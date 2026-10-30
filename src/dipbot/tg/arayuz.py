"""Telegram'dan BAĞIMSIZ komut/düğme mantığı: Olay -> Yanit.

Güvenlik kuralları (dp.txt bölüm 4 ve 12):
- HER komut ve HER düğme tıklaması, güncel DB durumuyla `yetki.karar_ver` ile YENİDEN denetlenir
  (sayısal kimlikler; eski/önbellek yetkisine güvenilmez).
- Özel sohbet yalnız kurucuya açık; GRUPLARDA hiçbir komut/düğme işlenmez (grup onayı/yetkili listesi kullanılmaz).
- Yetkisiz deneme çekirdekteki `yonetim.yetkisiz_deneme_kaydet` ile kaydedilir (kurucuya özel,
  hız sınırlı bildirim).
- Etkili işlemler (oyuncu/parti silme) onay düğmesiyle yapılır; onay işlemi sohbet +
  kullanıcıya bağlı, süreli ve tek kullanımlıktır.
- Tüm takip sorguları merkezi alan kimliğiyle (`group_id` sütunu = config.MERKEZ_ID) süzülür.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .. import config, outbox, yetki, yonetim
from ..db import alan_aktif, islem as db_islem
from ..hesaplar import HesapHatasi
from ..kaynak import EksikVeri, KaynakHatasi, ZamanlayiciKorumali, hata_aciklamasi
from ..takip import GrupOnaysiz, TakipServisi
from ..maske import sir_kaydet
from ..zaman import (
    SURE_GECERLI, SURE_SAAT_DEGISTI, Saat, ZamanDamgasi, damga_al, monotonik_al, simdi_utc, sure_durumu,
    yerel_goster, iso,
)
from . import bicim
from .baglanti import BaglantiHatasi, kimlik_coz_ayrintili
from .bicim import esc
from .hesap_menu import GirdiBekleme, HesapMenusu, sir_gibi_mi
from .islemler import IslemDeposu
from .kaynak_yok import KaynakYapilandirilmadi, yapilandirildi_mi
from .modeller import Dugme, Klavye, Olay, Yanit
from .token import TokenDeposu, TokenDogrulayici, YapilandirilmamisDogrulayici

log = logging.getLogger("dipbot.tg.arayuz")

SAYFA_BOYUTU = 8
TOKEN_BEKLEME_OMRU = timedelta(minutes=5)

# Telegram command names: [a-z0-9_] only, at most 32 characters. Descriptions are English (<=256).
# The English names below are the ones shown in the Telegram command menu, in /help and in on-screen hints.
# Internal handlers (`_k_<name>`), audit names and the permission policy keep the original (Turkish) command
# identifiers; every English name is normalised to that identifier before dispatch (see EN_KOMUTLAR).
KOMUTLAR: dict[str, str] = {
    "start": "Start the bot and open the main menu",
    "help": "Show commands and usage help",
    "addplayer": "Track a player (ID or link)",
    "players": "List tracked players",
    "removeplayer": "Stop tracking a player (ID or link)",
    "addparty": "Track a party (ID or link)",
    "parties": "List tracked parties",
    "removeparty": "Stop tracking a party (ID or link)",
    "wars": "List active wars",
    "scan": "Set the scan interval: 5, 10, 15 or 20 minutes",
    "status": "Show bot status",
    "accounts": "Account Management: Diplomacia accounts, tokens, proxies, assigned players",
    "channel": "Verify the notification channel (exists and the bot can post)",
    "lists": "Show the central tracking list summary",
}

# English command name -> internal command identifier.
EN_KOMUTLAR: dict[str, str] = {
    "start": "start",
    "help": "yardim",
    "addplayer": "oyuncuekle",
    "players": "oyuncular",
    "removeplayer": "oyuncusil",
    "addparty": "partiekle",
    "parties": "partiler",
    "removeparty": "partisil",
    "wars": "savaslar",
    "scan": "tarama",
    "status": "durum",
    "accounts": "hesaplar",
    "channel": "kanal",
    "lists": "listeler",
}
# Internal identifier -> English command name (for messages shown to users).
KOMUT_GOSTER: dict[str, str] = {v: k for k, v in EN_KOMUTLAR.items()}

# Backward compatibility: legacy Turkish command names keep working (same handler, same authorization and
# confirmation path) but are not listed in the Telegram menu or in /help.
ESKI_KOMUTLAR: dict[str, str] = {
    "oyuncu_ekle": "oyuncuekle",
    "oyuncu_sil": "oyuncusil",
    "parti_ekle": "partiekle",
    "parti_sil": "partisil",
}
# The short Turkish names (/oyuncuekle, /durum, ...) also keep working.
TR_KOMUTLAR: tuple[str, ...] = tuple(v for v in EN_KOMUTLAR.values() if v != "start")
TUM_KOMUT_ADLARI = tuple(KOMUTLAR) + tuple(ESKI_KOMUTLAR) + TR_KOMUTLAR


def komut_ic_adi(ad: str | None) -> str | None:
    """English or legacy command name -> internal identifier; None if unknown."""
    if ad in EN_KOMUTLAR:
        return EN_KOMUTLAR[ad]
    if ad in ESKI_KOMUTLAR:
        return ESKI_KOMUTLAR[ad]
    return ad if ad in TR_KOMUTLAR else None


def komut_goster(ic: str) -> str:
    """Internal identifier (or legacy name) -> English command name shown to users."""
    ic = ESKI_KOMUTLAR.get(ic, ic)
    return KOMUT_GOSTER.get(ic, ic)


_GRUP_KAPSAMLI = frozenset({"ol", "od", "os", "oe", "pl", "pd", "ps", "pe", "sl", "sd", "ta", "tv"})

KAPALI_OZEL = "In private chat, this bot is available to the owner only."
YETKISIZ = "You are not authorized to perform this action."
YALNIZ_KURUCU = "Only the owner can perform this action."
GRUP_ONAYSIZ_YANIT = "This tracking area is not active; no action was taken."
ESKI_DUGME = "This button is no longer valid. Open a new menu with /start."
ONAY_BITTI = "⌛ This confirmation has expired or has already been used. Please start the action again."
SAAT_DEGISTI = ("⏱ The server clock changed; for safety, the pending action was invalidated and not applied. "
                "Please start the action again.")


def kimlik_coz(arg: str, tur: str) -> str | None:
    """Kimlik veya Diplomacia bağlantısından kimliği çıkarır (tur: player|party); geçersizse None.
    Ayrıntılı hata için baglanti.kimlik_coz_ayrintili."""
    try:
        return kimlik_coz_ayrintili(arg, tur)
    except BaglantiHatasi:
        return None


def _tam_sayi(arg: str) -> int | None:
    arg = (arg or "").strip()
    if not re.fullmatch(r"-?\d{1,20}", arg):
        return None
    n = int(arg)
    return n if abs(n) <= 2**63 - 1 else None  # SQLite INTEGER sınırı: taşma (OverflowError) önlenir


def _dg(etiket: str, veri: str) -> Dugme | None:
    """Etiketi kısaltır; callback_data 64 baytı aşarsa düğme yapılmaz (None)."""
    try:
        return Dugme(etiket[:40], veri)
    except ValueError:
        return None


_HESAP_DUGMELERI = frozenset({"hm", "hl", "ha", "hz", "hn", "hi", "hd", "hs", "hy", "hq", "hx", "hp", "ht", "hps", "hpr", "hpt", "hc"})
_JWT_BICIMI = re.compile(r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}$")
_SAYFALAMA_NOTU = "<i>Note: the list is assumed to be unpaginated (unverified).</i>"
_GUC_NOTU = "<i>Note: it has not been verified that the 'Power' value represents damage.</i>"


class Arayuz(HesapMenusu):
    def __init__(
        self,
        conn,
        kaynak,
        kurucu_id: int,
        *,
        saat: Saat = simdi_utc,
        zamanlayici=None,
        dogrulayici: TokenDogrulayici | None = None,
        token_deposu: TokenDeposu | None = None,
        kanal=None,
        kapsam: int = config.MERKEZ_ID,
        hesaplar=None,
        eski_token_yolu=None,
    ):
        # hesaplar: HesapYoneticisi (üretim: ÇOK HESAPLI mod; token/proxy yönetimi 'Account Management' menüsündedir). None: eski tek-token modu
        # (yalnız testler/geri uyumluluk).
        self.hesaplar = hesaplar
        self.eski_token_yolu = eski_token_yolu
        self._girdi: dict[int, GirdiBekleme] = {}
        self._hesap_arama: dict[tuple, str] = {}
        self._hesap_islem_suruyor = False
        # kapsam: takip verisinin TEK alan kimliği (merkezi alan; Telegram kimliği DEĞİL). Üretimde hep config.MERKEZ_ID;
        # yalnız alan-yalıtımı testleri başka değer verir. Grup seçimi/grup onayı YOKTUR.
        self.kapsam = kapsam
        self.kanal = kanal  # KanalDogrulayici (veya None): durum ekranı ve /channel için
        self.conn = conn
        # DiplomaciaSource (SinirliKaynak ile sarılmış olması beklenir). Zamanlayıcı varsa istekler onun
        # duraklatma/istek-sınırı/403 geri çekilmesine uyar ve hataları ona iletir (O-d).
        self.kaynak = ZamanlayiciKorumali(kaynak, zamanlayici) if zamanlayici is not None else kaynak
        self.kurucu_id = kurucu_id
        self.saat = saat
        self.servis = TakipServisi(conn, saat, kanal_id=kanal.kanal_id if kanal is not None else None)
        self.zamanlayici = zamanlayici
        self.dogrulayici = dogrulayici or YapilandirilmamisDogrulayici()
        self.token_deposu = token_deposu
        self._mono = monotonik_al(saat)
        self.islemler = IslemDeposu(conn, saat, self._mono)
        # kullanıcı -> bekleme başlangıcı (duvar + monotonic). YALNIZ BELLEKTE: yeniden başlatmada geçersizdir (D7).
        self._token_bekleyen: dict[int, ZamanDamgasi] = {}
        self._token_islem_suruyor = False  # tek uçuş: eşzamanlı ikinci token gönderimi reddedilir
        self._bekleme_yukle()

    _BEKLEME_ANAHTARI = "token_bekleme"

    def _bekleme_yukle(self) -> None:
        """Yeniden başlatmada token-bekleme DEVAM ETMEZ (D7: monotonic başlangıç kurulamaz, geçerlilik
        belirsiz). Önceki sürümlerden kalan kalıcı kayıt varsa silinir."""
        with db_islem(self.conn):
            self.conn.execute("DELETE FROM sistem_durumu WHERE anahtar=?", (self._BEKLEME_ANAHTARI,))

    def _bekleme_ayarla(self, kullanici_id: int) -> None:
        self._token_bekleyen[kullanici_id] = damga_al(self.saat, self._mono)

    def _bekleme_kaldir(self, kullanici_id: int | None) -> None:
        if kullanici_id is not None:
            self._token_bekleyen.pop(kullanici_id, None)

    # ====================== yetki ===========================================
    def _durum(self):
        """Yetki durumu: YALNIZ kurucu. Grup onayı / yetkili kullanıcı listesi işleyişte KULLANILMAZ (veritabanından okunmaz)."""
        return yetki.YetkiDurumu(self.kurucu_id, frozenset(), frozenset())

    def _kaydet_yetkisiz(self, o: Olay, islem_adi: str) -> None:
        try:
            yonetim.yetkisiz_deneme_kaydet(
                self.conn,
                self.saat,
                self.kurucu_id,
                kullanici_id=o.kullanici_id,
                grup_id=o.sohbet_id if o.grup_mu else None,
                islem_adi=islem_adi,
            )
        except Exception:
            log.exception("yetkisiz deneme kaydı başarısız")

    def _yetki(self, o: Olay, islem_adi: str) -> yetki.Karar:
        k = yetki.karar_ver(
            self._durum(), sohbet_id=o.sohbet_id, sohbet_turu=o.sohbet_turu, kullanici_id=o.kullanici_id
        )
        if not k and k.neden not in ("desteklenmeyen_sohbet", "grup_komutlari_kapali"):
            self._kaydet_yetkisiz(o, islem_adi)
        return k

    def _red(self, o: Olay, k: yetki.Karar) -> Yanit | None:
        if k.neden == "desteklenmeyen_sohbet":
            return None
        if k.neden == "grup_komutlari_kapali":
            # Gruplar komut/düğme İŞLEMEZ ve hiçbir yanıt/uyarı/kayıt üretmez (bildirimler yalnız log kanalına gider).
            return Yanit(sessiz=True) if o.dugme_mi else None
        if o.dugme_mi:
            metin = {"ozel_sohbet_kapali": KAPALI_OZEL, "grup_onaysiz": "This group is not approved."}.get(
                k.neden, YETKISIZ
            )
            return Yanit(uyari=metin, uyari_goster=True, sessiz=True)
        if k.neden == "ozel_sohbet_kapali":
            return Yanit(KAPALI_OZEL)
        if k.neden == "kullanici_yetkisiz":
            return Yanit(YETKISIZ)
        return None  # onaysız grup: sessiz (spam ve bilgi sızıntısı yok); kurucuya bildirim gitti

    def _onayli(self, grup_id: int) -> bool:
        return alan_aktif(self.conn, grup_id)

    def _grup(self, o: Olay) -> int:
        """İşlemin takip alanı: HER ZAMAN merkezi alan (grup/liste seçimi yoktur)."""
        return self.kapsam

    def _grup_gerekli(self, o: Olay) -> tuple[int, None]:
        return self.kapsam, None

    # ====================== ortak yardımcılar ===============================
    async def _ack(self, o: Olay) -> None:
        """Ağ çağrısından önce callback'i hemen yanıtla (spinner/süre aşımı olmasın)."""
        if o.dugme_mi and o.ack is not None and not o.acked:
            o.acked = True
            try:
                await o.ack()
            except Exception:
                log.info("erken callback yanıtı başarısız")

    @staticmethod
    def _kaynak_hata(baslik: str, e: BaseException) -> Yanit:
        ad = "Diplomacia connection is not configured" if isinstance(e, KaynakYapilandirilmadi) else hata_aciklamasi(e)
        return Yanit(f"{esc(baslik)}: {esc(ad)}. No record was changed.", [[Dugme("🏠 Main menu", "m")]])

    @staticmethod
    def _sayfa(toplam: int, sayfa: int) -> tuple[int, int, int]:
        sayfa_sayisi = max(1, -(-toplam // SAYFA_BOYUTU))
        sayfa = min(max(sayfa, 0), sayfa_sayisi - 1)
        return sayfa, sayfa_sayisi, sayfa * SAYFA_BOYUTU

    @staticmethod
    def _gezinti(onek: str, sayfa: int, sayfa_sayisi: int) -> list[Dugme]:
        satir = []
        if sayfa > 0:
            satir.append(Dugme("‹ Previous", f"{onek}:{sayfa - 1}"))
        satir.append(Dugme(f"{sayfa + 1}/{sayfa_sayisi}", "nop"))
        if sayfa < sayfa_sayisi - 1:
            satir.append(Dugme("Next ›", f"{onek}:{sayfa + 1}"))
        return satir

    @staticmethod
    def _bilgi(o: Olay, metin: str) -> Yanit:
        """Düğmede açılır uyarı (menü bozulmaz); komutta normal mesaj."""
        if o.dugme_mi:
            return Yanit(uyari=metin[:200], uyari_goster=True, sessiz=True)
        return Yanit(esc(metin))

    def _onay_yaniti(self, o: Olay, tur: str, grup_id: int | None, veri: dict, metin: str) -> Yanit:
        assert o.kullanici_id is not None
        kid = self.islemler.olustur(tur, o.sohbet_id, o.kullanici_id, grup_id, veri)
        return Yanit(
            metin,
            [[Dugme("✅ Confirm", f"c:e:{kid}"), Dugme("❌ Cancel", f"c:h:{kid}")]],
        )

    # ====================== giriş noktaları =================================
    async def komut(self, o: Olay) -> Yanit | None:
        ad = komut_ic_adi(o.komut)
        if ad is None:
            return None
        k = self._yetki(o, f"/{ad}")
        if not k:
            return self._red(o, k)
        try:
            sonuc = await getattr(self, f"_k_{ad}")(o)
            self._dagit()
            return sonuc
        except GrupOnaysiz:  # servis katmanı onaysız grubu reddetti (ör. işlem sırasında onay kalktı)
            return Yanit(GRUP_ONAYSIZ_YANIT)
        except Exception:
            log.exception("komut işlenirken beklenmeyen hata: /%s", ad)
            return Yanit("An unexpected error occurred; the action was not completed. Details are in the owner's log.")

    async def buton(self, o: Olay) -> Yanit:
        veri = o.veri or ""
        k = self._yetki(o, f"buton:{veri[:30]}")
        if not k:
            return self._red(o, k) or Yanit(uyari=YETKISIZ, uyari_goster=True, sessiz=True)
        parca = veri.split(":", 2)
        try:
            y = await self._buton_yonlendir(o, parca)
            self._dagit()
        except GrupOnaysiz:
            y = Yanit(GRUP_ONAYSIZ_YANIT, uyari="Tracking area is not active.", uyari_goster=True)
        except Exception:
            log.exception("düğme işlenirken beklenmeyen hata: %s", parca[0])
            y = Yanit(uyari="An unexpected error occurred.", uyari_goster=True, sessiz=True)
        if y is None:
            y = Yanit(uyari=ESKI_DUGME, uyari_goster=True, sessiz=True)
        if y.metin and not y.sessiz:
            y.duzenle = True
        return y

    async def metin(self, o: Olay) -> Yanit | None:
        """Düz metin: yalnız kurucunun özel sohbetinde, token bekleniyorsa işlenir."""
        if o.sohbet_turu != "private" or o.kullanici_id is None:
            return None
        if self.hesaplar is not None:
            g = self._girdi.get(o.kullanici_id)
            k = yetki.token_yonetimi_izinli_mi(
                self._durum(), sohbet_id=o.sohbet_id, sohbet_turu=o.sohbet_turu, kullanici_id=o.kullanici_id
            )
            if g is not None:
                if not k:
                    self._girdi_kaldir(o.kullanici_id)
                    self._kaydet_yetkisiz(o, "hesap_girdisi")
                    return Yanit(KAPALI_OZEL)
                return await self._hesap_girdi(o, g)
            if k and (_JWT_BICIMI.match(o.metin or "") or sir_gibi_mi(o.metin or "")):  # bekleme yokken gelen JWT/proxy/uzun dizi: kalmasın, işlenmesin
                sir_kaydet(o.metin)
                return Yanit(f"{await self._mesaji_sil_notu(o)}\nTo set a token, open Account Management → an account → Update JWT Token first; "
                             "this message was not processed.")
            return None
        damga = self._token_bekleyen.get(o.kullanici_id)
        if damga is None:
            if _JWT_BICIMI.match(o.metin or "") and yetki.token_yonetimi_izinli_mi(
                self._durum(), sohbet_id=o.sohbet_id, sohbet_turu=o.sohbet_turu, kullanici_id=o.kullanici_id
            ):
                if o.sil is not None:
                    try:
                        await o.sil()
                    except Exception:
                        pass
                return Yanit("First choose Renew Token from the menu; this message was not processed.")
            return None
        k = yetki.token_yonetimi_izinli_mi(
            self._durum(), sohbet_id=o.sohbet_id, sohbet_turu=o.sohbet_turu, kullanici_id=o.kullanici_id
        )
        if not k:
            self._bekleme_kaldir(o.kullanici_id)
            self._kaydet_yetkisiz(o, "token_mesaji")
            return Yanit(KAPALI_OZEL)
        metin = (o.metin or "").strip()
        sure = sure_durumu(damga, TOKEN_BEKLEME_OMRU, self.saat, self._mono)
        if sure != SURE_GECERLI:
            self._bekleme_kaldir(o.kullanici_id)
            if _JWT_BICIMI.match(metin) or (len(metin) >= 40 and not any(c.isspace() for c in metin)):
                await self._mesaji_silmeyi_dene(o)  # token olabilir: sohbette kalmasın (değer yansıtılmaz)
            if sure == SURE_SAAT_DEGISTI:
                return Yanit("The server clock changed; for safety, the token wait was cancelled and your message "
                             "was not processed (the current token was NOT CHANGED). Restart from the menu.")
            return Yanit("The token wait time has expired. Restart from the menu.")
        if not _JWT_BICIMI.match(metin):
            # JWT biçimli olmayan düz metin token sayılmaz; bekleme tek kullanımlıktır (tüketilir)
            self._bekleme_kaldir(o.kullanici_id)
            await self._mesaji_silmeyi_dene(o)
            return Yanit("Invalid token format (a single token in JWT format is expected). An attempt was made to "
                         "delete your message; the current token was NOT CHANGED. Restart from the menu.")
        self._bekleme_kaldir(o.kullanici_id)  # tek kullanımlık bekleme
        return await self._token_isle(o)

    @staticmethod
    async def _mesaji_silmeyi_dene(o: Olay) -> bool:
        if o.sil is None:
            return False
        try:
            return bool(await o.sil())
        except Exception:
            return False

    # ====================== komutlar ========================================
    async def _k_hesaplar(self, o: Olay) -> Yanit:
        if self.hesaplar is None:
            return Yanit("Account Management is not enabled in this process.")
        return self._hesap_ana_ekran(o)

    def _dagit(self) -> None:
        """Oyuncu eklendi/silindi: görev atamasını hemen güncelle (zamanlayıcı da her turda yapar)."""
        if self.hesaplar is not None:
            try:
                self.hesaplar.dagit()
            except Exception:
                log.exception("görev dağıtımı başarısız")

    async def _k_kanal(self, o: Olay) -> Yanit:
        """Log kanalını Telegram API ile yeniden doğrular (kanal var mı, bot mesaj gönderebilir mi); mesaj göndermez."""
        if self.kanal is None:
            return Yanit(f"Notification channel: <code>{config.LOG_KANAL_ID}</code>\nVerification is not available in this process.")
        d = await self.kanal.dogrula()
        satirlar = [f"<b>Notification channel</b> <code>{self.kanal.kanal_id}</code>"]
        if d.baslik:
            satirlar.append(f"Title: {esc(d.baslik)}")
        if d.hazir:
            satirlar.append("✅ The channel exists and the bot can post messages there.")
        else:
            satirlar.append(f"⚠️ Not ready: {esc(d.neden)}")
            satirlar.append("Tracking scans stay paused and no tracking notifications are sent until this is fixed "
                            "(add the bot as an administrator with the 'Post messages' permission).")
        return Yanit("\n".join(satirlar), [[Dugme("🏠 Main menu", "m")]])

    async def _k_listeler(self, o: Olay) -> Yanit:
        """MERKEZİ takip listesinin özeti (eski gruplar değil). Arşivlenmiş eski grup kayıtları yalnız bilgi satırıdır."""
        from .. import rapor

        m = rapor.merkez_ozeti(self.conn, self.kapsam)
        satirlar = [
            "<b>Central tracking list</b>",
            f"Players: {m['oyuncu']} (manually added: {m['oyuncu_elle']})",
            f"Parties: {m['parti']} (members stored: {m['parti_uyesi']})",
            f"Wars known: {m['savas']}",
            f"Scan interval: {m['aralik_dk']} minutes",
            f"Last successful scan: {bicim.tarih(m['son_tarama']) if m['son_tarama'] else 'none yet'}",
            f"Pending notifications: {m['bekleyen_bildirim']}",
        ]
        if m["arsiv_alanlari"]:
            satirlar.append(
                f"Archived old-group records: {m['arsiv_alanlari']} (kept only as history/migration source; not scanned, no "
                f"notifications). Migration: {'applied' if m['migrasyon_uygulandi'] else 'NOT applied yet'}."
            )
        return Yanit("\n".join(satirlar), [[Dugme("🏠 Main menu", "m")]])

    async def _k_start(self, o: Olay) -> Yanit:
        return self._ana_menu(o)

    async def _k_yardim(self, o: Olay) -> Yanit:
        return self._yardim()

    @staticmethod
    def _kimlik_al(o: Olay, tur: str, komut: str) -> tuple[str | None, Yanit | None]:
        """Komut argümanından (ID veya Diplomacia bağlantısı) kimliği çıkarır; geçersizse kullanım yanıtı döner."""
        try:
            return kimlik_coz_ayrintili(o.arg, tur), None
        except BaglantiHatasi as e:
            ornek = "72400" if tur == "player" else "1244"
            return None, Yanit(f"⚠️ {esc(str(e))}\nUsage: <code>/{komut_goster(komut)} {ornek}</code>")

    async def _k_oyuncuekle(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        oid, hata = self._kimlik_al(o, "player", "oyuncuekle")
        if hata:
            return hata
        try:
            s = await self.servis.oyuncu_ekle(g, oid, self.kaynak)
        except KaynakHatasi as e:
            return self._kaynak_hata("Could not add player", e)
        if s.durum == "zaten_takipte":
            return Yanit(f"This player is already tracked: {bicim.oyuncu_link(s.oyuncu_id)}", [[Dugme("👤 Players", "ol:0")]])
        return Yanit(
            "\n".join(
                [
                    "✅ <b>Player added to tracking</b>",
                    bicim.oyuncu_link(oid, s.ad),
                    f"Region: {esc(s.bolge or 'unknown')}",
                    f"Citizenship: {esc(s.vatandaslik_ulkesi or 'unknown')}",
                    "<i>This is an initial summary; it is not a change notification.</i>",
                ]
            ),
            [[Dugme("👤 Players", "ol:0"), Dugme("🏠 Main menu", "m")]],
        )

    async def _k_oyuncular(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        return r or self._oyuncu_liste(g, 0)

    async def _k_oyuncusil(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        oid, hata = self._kimlik_al(o, "player", "oyuncusil")
        if hata:
            return hata
        return self._oyuncu_sil_onayi(o, g, oid)

    async def _k_partiekle(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        pid, hata = self._kimlik_al(o, "party", "partiekle")
        if hata:
            return hata
        try:
            s = await self.servis.parti_ekle(g, pid, self.kaynak)
        except KaynakHatasi as e:
            return self._kaynak_hata("Could not add party", e)
        if s.durum == "zaten_takipte":
            return Yanit(f"This party is already tracked: {bicim.parti_link(pid)}", [[Dugme("👥 Parties", "pl:0")]])
        esik = f"{config.PARTI_XP_ESIGI:,}"
        satirlar = [
            "✅ <b>Party added to tracking</b>",
            bicim.parti_link(pid, s.ad),
            f"Total members: {s.toplam_uye or s.uye_sayisi}",
            f"Reviewed from the API: {s.incelenen or s.uye_sayisi}",
            f"Tracked with {esik}+ XP: {s.takip_edilen}",
            f"Below the XP threshold: {s.xp_alti}",
        ]
        if s.xp_bilinmeyen:
            satirlar.append(f"XP missing/invalid (not tracked): {s.xp_bilinmeyen}")
        if s.engelli:
            satirlar.append(f"Not added because they were removed manually (blocked): {s.engelli}")
        satirlar.append(f"First observation received: {s.ilk_veri_alinan}")
        satirlar.append(f"Awaiting first scan: {s.ilk_veri_bekleyen}" + (f" ({esc(s.ilk_veri_notu)})" if s.ilk_veri_bekleyen and s.ilk_veri_notu else ""))
        satirlar += self._kismi_not(s)
        satirlar.append(
            "<i>This is an initial summary (a single message): no notifications are sent for member joins, "
            "region or citizenship; no separate messages are sent for players. From now on, only real changes "
            "are reported.</i>"
        )
        return Yanit("\n".join(satirlar), [[Dugme("👥 Parties", "pl:0"), Dugme("🏠 Main menu", "m")]])

    @staticmethod
    def _kismi_not(s) -> list[str]:
        """KISMİ okuma (API tek istekte 100 üye döndürdü): liste EKSİKTİR, tam liste sayılmaz. Okunmayan üyelerin XP'si
        bilinmez. Gerçek sayfalama yöntemi doğrulanmadığı için ek sayfa istenmez."""
        if not s.kismi:
            return []
        kalan = max((s.toplam_uye or 0) - (s.incelenen or 0), 0)
        satirlar = [
            f"⚠️ <b>Member list read INCOMPLETE: {s.incelenen}/{s.toplam_uye}.</b> The API returned {s.incelenen} members in a single request; "
            f"the other {kalan} members could not be read (their XP is unknown, so they could not be tracked). The pagination method is unverified; "
            "no additional pages were requested.",
        ]
        if s.min_xp is not None and s.min_xp >= config.PARTI_XP_ESIGI:
            satirlar.append("All members read are above the threshold: there may also be eligible players among the members that could not be read.")
        else:
            satirlar.append(
                "The lowest XP read is below the threshold; however, since the ordering is not trusted, it cannot be "
                "verified that the unread members are also below the threshold."
            )
        xp = {True: "appears to be in descending order", False: "is NOT in descending order", None: "order could not be verified"}[s.xp_sirali]
        satirlar.append(f"Note: XP among the members read: {xp} (the ordering is not trusted; each member's XP was checked individually).")
        return satirlar

    async def _k_partiler(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        return r or self._parti_liste(g, 0)

    async def _k_partisil(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        pid, hata = self._kimlik_al(o, "party", "partisil")
        if hata:
            return hata
        return await self._parti_sil_baslat(o, g, pid)

    async def _k_savaslar(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        return r or await self._savas_liste(o, g, 0)

    async def _k_tarama(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        if not o.arg.strip():
            return self._tarama_ekrani(g)
        dk = _tam_sayi(o.arg.split()[0])
        return self._tarama_ayarla(g, dk)

    async def _k_durum(self, o: Olay) -> Yanit:
        return self._durum_ekrani(o)

    # ====================== düğmeler ========================================
    async def _buton_yonlendir(self, o: Olay, p: list[str]) -> Yanit | None:
        a = p[0]
        arg = p[1] if len(p) > 1 else ""
        sayfa = _tam_sayi(arg) or 0 if a in ("ol", "pl", "sl") else 0

        if a == "nop":
            return Yanit(sessiz=True)
        if a == "m":
            return self._ana_menu(o)
        if a == "yd":
            return self._yardim()
        if a == "du":
            return self._durum_ekrani(o)
        if a == "yo":
            return self._yonetim_ekrani(o)
        if a == "c":
            return await self._b_onay(o, p)
        if self.hesaplar is not None and a in _HESAP_DUGMELERI:
            return await self._hesap_buton(o, a, p)
        if a in ("tk", "tn", "tc"):
            if self.hesaplar is not None:  # çok hesaplı mod: eski tek-token akışı yerine hesap menüsü
                return self._hesap_ana_ekran(o)
            return await self._b_token(o, a)

        # Buradan sonrası takip ekranları (merkezi alan; bilinmeyen/eski düğmeler yok sayılır)
        if a not in _GRUP_KAPSAMLI:
            return None
        g, r = self._grup_gerekli(o)
        if r:
            return r
        if a == "ol":
            return self._oyuncu_liste(g, sayfa)
        if a == "od":
            return self._oyuncu_detay(g, arg)
        if a == "os":
            return self._oyuncu_sil_onayi(o, g, arg)
        if a == "oe":
            return Yanit(
                "To add a player:\n<code>/addplayer 72400</code>\nor with a link:\n<code>/addplayer https://diplomacia.com.tr/player/72400</code>",
                [[Dugme("👤 Players", "ol:0"), Dugme("🏠 Main menu", "m")]],
            )
        if a == "pl":
            return self._parti_liste(g, sayfa)
        if a == "pd":
            return self._parti_detay(g, arg)
        if a == "ps":
            return await self._parti_sil_baslat(o, g, arg)
        if a == "pe":
            return Yanit(
                "To add a party:\n<code>/addparty 1244</code>\nor with a link:\n<code>/addparty https://diplomacia.com.tr/party/1244</code>",
                [[Dugme("👥 Parties", "pl:0"), Dugme("🏠 Main menu", "m")]],
            )
        if a == "sl":
            return await self._savas_liste(o, g, sayfa)
        if a == "sd":
            return await self._savas_detay(o, arg)
        if a == "ta":
            return self._tarama_ekrani(g)
        if a == "tv":
            return self._tarama_ayarla(g, _tam_sayi(arg))
        return None

    # ====================== ekranlar ========================================
    def _yardim(self) -> Yanit:
        satirlar = ["<b>Commands</b>"]
        ornek = {
            "addplayer": "&lt;ID or link&gt;",
            "removeplayer": "&lt;ID or link&gt;",
            "addparty": "&lt;ID or link&gt;",
            "removeparty": "&lt;ID or link&gt;",
            "scan": "&lt;5|10|15|20&gt;",
        }
        for ad, aciklama in KOMUTLAR.items():
            if ad in ("start",):
                continue
            satirlar.append(f"/{ad} {ornek.get(ad, '')}".rstrip() + f" — {esc(aciklama)}")
        satirlar.append("")
        satirlar.append(
            "<i>Checks run at intervals; the time in a notification is the detection time, not the exact time of "
            "the change. Commands work only in the owner's private chat (groups are ignored). Tracking notifications "
            "are posted to the log channel only. All times are Europe/Istanbul (24-hour clock).</i>"
        )
        return Yanit("\n".join(satirlar), [[Dugme("🏠 Main menu", "m")]])

    def _ana_menu(self, o: Olay) -> Yanit:
        satirlar = ["<b>Diplomacia GPS Bot</b>", "Choose a section."]
        klavye: Klavye = [
            [Dugme("👤 Players", "ol:0"), Dugme("👥 Parties", "pl:0")],
            [Dugme("⚔️ Wars", "sl:0"), Dugme("⏱️ Scan Settings", "ta")],
            [Dugme("📊 Bot Status", "du"), Dugme("⚙️ Administration", "yo")],
            [Dugme("❓ Help", "yd")],
        ]
        if self.hesaplar is not None:
            klavye.insert(3, [Dugme("🔐 Account Management", "hm")])
        return Yanit("\n".join(satirlar), klavye)

    # ---- oyuncular
    def _oyuncu_liste(self, g: int, sayfa: int) -> Yanit:
        satirlar_db = self.conn.execute(
            "SELECT oyuncu_id, ad FROM takip_oyuncular WHERE group_id=?"
            " ORDER BY lower(COALESCE(ad, '')), oyuncu_id",
            (g,),
        ).fetchall()
        toplam = len(satirlar_db)
        if toplam == 0:
            return Yanit(
                "No players are tracked yet.\nTo add one: "
                "<code>/addplayer &lt;ID or link&gt;</code>",
                [[Dugme("How to add a player?", "oe")], [Dugme("🏠 Main menu", "m")]],
            )
        sayfa, sayfa_sayisi, bas = self._sayfa(toplam, sayfa)
        dilim = satirlar_db[bas : bas + SAYFA_BOYUTU]
        metin = [f"<b>Tracked players</b> (total {toplam})"]
        klavye: Klavye = []
        for i, r in enumerate(dilim, start=bas + 1):
            metin.append(f"{i}. {bicim.oyuncu_link(r['oyuncu_id'], r['ad'])} (#{esc(r['oyuncu_id'])})")
            d = _dg(f"{i}. {r['ad'] or r['oyuncu_id']} #{r['oyuncu_id']}", f"od:{r['oyuncu_id']}")
            if d:
                klavye.append([d])
        klavye.append(self._gezinti("ol", sayfa, sayfa_sayisi))
        klavye.append([Dugme("🏠 Main menu", "m")])
        return Yanit("\n".join(metin), klavye)

    def _oyuncu_detay(self, g: int, oid: str) -> Yanit:
        r = self.conn.execute(
            "SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)
        ).fetchone()
        if r is None:
            return Yanit(uyari="This player is not tracked (may have been removed).", uyari_goster=True, sessiz=True)
        metin = [
            f"<b>Player</b>: {bicim.oyuncu_link(oid, r['ad'])} (#{esc(oid)})",
            f"Region: {esc(r['bolge'] or 'unknown')}",
            f"Citizenship: {esc(r['vatandaslik_ulkesi'] or 'unknown')}",
            f"Last observation: {bicim.tarih(r['son_gozlem'])}",
            "Added: " + ("manually" if r["elle_eklendi"] else "via party membership"),
        ]
        if oid in self.servis.bulunamayan_oyuncular(g):
            metin.append(
                "⚠ Not found: the player was not found at the source in several consecutive scans (the record "
                "was not deleted; stop tracking manually if needed)."
            )
        klavye: Klavye = []
        sil = _dg("🗑 Stop tracking", f"os:{oid}")
        if sil:
            klavye.append([sil])
        klavye.append([Dugme("📋 List", "ol:0"), Dugme("🏠 Main menu", "m")])
        return Yanit("\n".join(metin), klavye)

    def _oyuncu_sil_onayi(self, o: Olay, g: int, oid: str) -> Yanit:
        r = self.conn.execute(
            "SELECT ad FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)
        ).fetchone()
        if r is None:
            return self._bilgi(o, "This player is not tracked.")
        return self._onay_yaniti(
            o,
            "oyuncu_sil",
            g,
            {"id": oid},
            f"<b>Stop tracking this player?</b>\n{bicim.oyuncu_link(oid, r['ad'])} (#{esc(oid)})\n"
            "Only removes the player from tracking. If the player is a member of a tracked party, they will not be re-added automatically.",
        )

    # ---- partiler
    def _parti_liste(self, g: int, sayfa: int) -> Yanit:
        satirlar_db = self.conn.execute(
            "SELECT t.parti_id, t.ad, t.kismi, t.toplam_uye, (SELECT COUNT(*) FROM parti_uyeleri u"
            " WHERE u.group_id=t.group_id AND u.parti_id=t.parti_id) AS uye FROM takip_partiler t WHERE t.group_id=?"
            " ORDER BY lower(COALESCE(t.ad, '')), t.parti_id",
            (g,),
        ).fetchall()
        toplam = len(satirlar_db)
        if toplam == 0:
            return Yanit(
                "No parties are tracked yet.\nTo add one: <code>/addparty &lt;ID or link&gt;</code>",
                [[Dugme("How to add a party?", "pe")], [Dugme("🏠 Main menu", "m")]],
            )
        sayfa, sayfa_sayisi, bas = self._sayfa(toplam, sayfa)
        metin = [f"<b>Tracked parties</b> (total {toplam})"]
        klavye: Klavye = []
        for i, r in enumerate(satirlar_db[bas : bas + SAYFA_BOYUTU], start=bas + 1):
            metin.append(
                f"{i}. {bicim.parti_link(r['parti_id'], r['ad'])} (#{esc(r['parti_id'])}) — "
                + (f"{r['uye']}/{r['toplam_uye']} members" if r["kismi"] and r["toplam_uye"] else f"{r['uye']} members")
            )
            d = _dg(f"{i}. {r['ad'] or r['parti_id']} #{r['parti_id']}", f"pd:{r['parti_id']}")
            if d:
                klavye.append([d])
        klavye.append(self._gezinti("pl", sayfa, sayfa_sayisi))
        klavye.append([Dugme("🏠 Main menu", "m")])
        return Yanit("\n".join(metin), klavye)

    def _parti_detay(self, g: int, pid: str) -> Yanit:
        r = self.conn.execute(
            "SELECT * FROM takip_partiler WHERE group_id=? AND parti_id=?", (g, pid)
        ).fetchone()
        if r is None:
            return Yanit(uyari="This party is not tracked (may have been removed).", uyari_goster=True, sessiz=True)
        uye = self.conn.execute(
            "SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (g, pid)
        ).fetchone()[0]
        metin = [
            f"<b>Party</b>: {bicim.parti_link(pid, r['ad'])} (#{esc(pid)})",
            f"Known member count: {uye}",
            f"Last verification: {bicim.tarih(r['son_dogrulama'])}",
        ]
        if r["kismi"]:
            metin.insert(2, f"Total members: {r['toplam_uye'] or '?'} — only {uye} members COULD BE READ in the last scan "
                            "(list incomplete; unread members cannot be monitored, and not appearing does not count as a membership change).")
        metin.insert(2, f"Automatic tracking: {config.PARTI_XP_ESIGI:,} XP and above")
        klavye: Klavye = []
        sil = _dg("🗑 Remove party tracking", f"ps:{pid}")
        if sil:
            klavye.append([sil])
        klavye.append([Dugme("📋 List", "pl:0"), Dugme("🏠 Main menu", "m")])
        return Yanit("\n".join(metin), klavye)

    async def _parti_sil_baslat(self, o: Olay, g: int, pid: str) -> Yanit:
        await self._ack(o)
        on = await self.servis.parti_kaldirma_hazirla(g, pid, self.kaynak)
        if not on.basarili:
            return Yanit(
                f"Could not remove party: {esc(on.neden)}", [[Dugme("👥 Parties", "pl:0"), Dugme("🏠 Main menu", "m")]]
            )
        return self._parti_onay_ekrani(o, g, on, tekrar=False)

    def _parti_onay_ekrani(self, o: Olay, g: int, on, *, tekrar: bool) -> Yanit:
        adlar = {}
        for oid in on.etkilenen[:15]:
            r = self.conn.execute(
                "SELECT ad FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)
            ).fetchone()
            adlar[oid] = r["ad"] if r else None
        satirlar = ["<b>Remove party tracking?</b>"]
        if tekrar:
            satirlar.append("⚠️ Membership changed during confirmation; please confirm the updated summary again.")
        satirlar += [
            f"Party: {bicim.parti_link(on.parti_id, on.parti_ad)} (#{esc(on.parti_id)})",
            f"Current member count: {on.uye_sayisi}" if not on.kismi else
            f"Total members: {on.toplam_uye} (list INCOMPLETE: only {on.uye_sayisi} members could be read)",
            f"Players to be removed from tracking: <b>{len(on.etkilenen)}</b>",
        ]
        for oid, ad in adlar.items():
            satirlar.append(f"• {esc(ad or oid)} (#{esc(oid)})")
        if len(on.etkilenen) > len(adlar):
            satirlar.append(f"… and {len(on.etkilenen) - len(adlar)} more players")
        satirlar.append(
            "Players who left the party earlier remain tracked. "
            "Current members are removed even if they were added manually."
        )
        if on.kismi:
            satirlar.append(
                "⚠️ Large party: the member list was read incompletely; members who could not be read (even if "
                "added manually) are not removed from tracking by this action."
            )
        return self._onay_yaniti(
            o, "parti_sil", g, {"parti_id": on.parti_id, "ozet": on.ozet_anahtari}, "\n".join(satirlar)
        )

    # ---- savaşlar
    async def _savas_liste(self, o: Olay, g: int, sayfa: int) -> Yanit:
        await self._ack(o)
        try:
            liste = await self.kaynak.list_active_wars()
        except KaynakHatasi as e:
            return self._savas_liste_yedek(g, e)
        if not liste.tamam:  # eksik liste 'savaş yok' gibi gösterilmez
            return self._savas_liste_yedek(g, EksikVeri('war list was not completed'))
        savaslar = liste.savaslar
        toplam = len(savaslar)
        simdi = self.saat()
        ust = [f"<b>Active wars</b> (total {toplam})", f"Data as of: {yerel_goster(simdi)} (fetched just now)"]
        if toplam == 0:
            return Yanit("\n".join(ust + ["No active wars are currently shown.", _SAYFALAMA_NOTU]), [[Dugme("🔄 Refresh", "sl:0"), Dugme("🏠 Main menu", "m")]])
        sayfa, sayfa_sayisi, bas = self._sayfa(toplam, sayfa)
        klavye: Klavye = []
        for i, s in enumerate(savaslar[bas : bas + SAYFA_BOYUTU], start=bas + 1):
            ust.append(bicim.savas_liste_satiri(i, s))
            kisa = bicim.savas_kisa_ad(s.attacker_name, s.defender_name, s.ad, s.war_number, s.savas_id)
            d = _dg(f"🔎 {i}. {kisa}", f"sd:{s.savas_id}")
            if d:
                klavye.append([d])
        ust.append(bicim.SAVAS_EMOJI_LEJANDI)
        ust.append(f'<a href="{esc(bicim.SITE)}/wars">Wars page</a>')
        ust.append(_SAYFALAMA_NOTU)
        klavye.append(self._gezinti("sl", sayfa, sayfa_sayisi))
        klavye.append([Dugme("🔄 Refresh", f"sl:{sayfa}"), Dugme("🏠 Main menu", "m")])
        return Yanit("\n".join(ust), klavye)

    def _savas_liste_yedek(self, g: int, e: BaseException) -> Yanit:
        neden = "Diplomacia connection is not configured" if isinstance(e, KaynakYapilandirilmadi) else hata_aciklamasi(e)
        satirlar = ["<b>Wars</b>", f"⚠️ Current data could not be fetched ({esc(neden)}): the data was unreachable."]
        r = self.conn.execute(
            "SELECT son_basari FROM tarama_isleri WHERE group_id=? AND is_tipi='savaslar'", (g,)
        ).fetchone()
        satirlar.append(f"Last successful war scan: {bicim.tarih(r['son_basari']) if r and r['son_basari'] else 'none'}")
        bilinen = self.conn.execute(
            "SELECT savas_id, ad, ilk_gorulme FROM savaslar WHERE group_id=? ORDER BY ilk_gorulme DESC LIMIT ?",
            (g, SAYFA_BOYUTU),
        ).fetchall()
        if bilinen:
            satirlar.append("Known wars in the record (old record; may have ended):")
            for b in bilinen:
                satirlar.append(f"• {esc(b['ad'] or b['savas_id'])} — first seen {bicim.tarih(b['ilk_gorulme'])}")
        return Yanit("\n".join(satirlar), [[Dugme("🔄 Refresh", "sl:0"), Dugme("🏠 Main menu", "m")]])

    async def _savas_detay(self, o: Olay, sid: str) -> Yanit:
        if not sid:
            return None  # type: ignore[return-value]
        await self._ack(o)
        gv = await self.servis.savas_detayi(sid, self.kaynak)
        klavye = [[d for d in (_dg("🔄 Refresh", f"sd:{sid}"),) if d] + [Dugme("📋 List", "sl:0")], [Dugme("🏠 Main menu", "m")]]
        neden = ""
        if gv.hata:
            neden = ("Diplomacia connection is not configured"
                     if "not configured" in gv.hata.lower() else gv.hata)
        if gv.detay is None:
            return Yanit(
                f"<b>War</b> #{esc(sid)}\n⚠️ Data unreachable ({esc(neden)}). No previously fetched data.",
                klavye,
            )
        d = gv.detay
        sad = d.taraflar[0].ad if len(d.taraflar) > 1 else None
        sav = d.taraflar[1].ad if len(d.taraflar) > 1 else None
        baslik = bicim.savas_baglantili(d.ad, d.war_number, sad, sav, sid)
        if bicim.savas_baglantisi(d.war_number) is None:
            baslik += f" (#{esc(sid)})"
        satirlar = [
            f"{bicim.tur_emoji(d.tur)} <b>War</b>: {baslik}",
            f"Status: {bicim.durum_emoji(d.durum)} {bicim.durum_etiketi(d.durum)}",
        ]
        if d.tur:
            satirlar.append(f"Type: {bicim.tur_etiketi(d.tur)}")
        if d.taraflar:
            satirlar.append("Sides:")
            rozet = ("⚔️ Attacker: ", "🛡️ Defender: ") if len(d.taraflar) == 2 else ("", "")
            for k, t in enumerate(d.taraflar):
                guc = "no data" if t.guc is None else f"{t.guc:,}"
                satirlar.append(f"• {rozet[k] if len(d.taraflar) == 2 else ''}{esc(t.ad or 'unknown')} — Power: {guc}")
            satirlar.append(_GUC_NOTU)
        else:
            satirlar.append("Side information: no data")
        satirlar.append(f'<a href="{esc(bicim.SITE)}/wars">Wars page</a>')
        zaman = yerel_goster(gv.zaman) if gv.zaman else "unknown"
        if gv.guncel:
            satirlar.append(f"Data as of: {zaman} (fetched just now)")
        else:
            satirlar.append(f"⚠️ Current data could not be fetched ({esc(neden)}). The data shown is from {zaman}.")
        satirlar.append("<i>No automatic notifications are sent for power changes or status transitions.</i>")
        return Yanit("\n".join(satirlar), klavye)

    # ---- tarama ayarı
    def _tarama_ekrani(self, g: int, not_: str = "") -> Yanit:
        mevcut = yonetim.tarama_araligi(self.conn, g)
        satirlar = [f"<b>Scan Settings</b>", f"Current interval: {mevcut} minutes"]
        if not_:
            satirlar.append(not_)
        satirlar.append("The selected interval applies to player, party and new-war checks.")
        satir = [
            Dugme(("✓ " if dk == mevcut else "") + f"{dk} min", f"tv:{dk}")
            for dk in config.ALLOWED_SCAN_INTERVALS_MINUTES
        ]
        return Yanit("\n".join(satirlar), [satir, [Dugme("🏠 Main menu", "m")]])

    def _tarama_ayarla(self, g: int, dk: int | None) -> Yanit:
        izinli = config.ALLOWED_SCAN_INTERVALS_MINUTES
        if dk is None or dk not in izinli:
            return Yanit(
                f"Invalid interval. Allowed values: {', '.join(map(str, izinli))} (minutes).",
                uyari="Invalid interval", uyari_goster=True, sessiz=True,
            )
        try:
            yonetim.tarama_araligi_ayarla(self.conn, g, dk)
        except (ValueError, yonetim.YonetimHatasi) as e:
            return Yanit(uyari=f"Could not be set: {e}"[:200], uyari_goster=True, sessiz=True)
        return self._tarama_ekrani(g, f"✅ Interval set to {dk} minutes (effective from the next scan).")

    # ---- durum
    def _token_duraklatildi(self) -> bool:
        if self.zamanlayici is not None:
            return bool(self.zamanlayici.token_duraklatildi)
        r = self.conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='token_duraklatildi'").fetchone()
        return bool(r and r[0] == "1")

    def _baglanti_metni(self, g: int | None, isler: dict) -> str | None:
        """Problem description for the status screen, or None when there is nothing to report.
        No fixed "configured / not verified" boilerplate is shown: only real, current conditions."""
        if not yapilandirildi_mi(self.kaynak):
            return "not configured"
        ic = getattr(self.kaynak, "_ic", self.kaynak)
        if getattr(ic, "token_hazir", True) is False:
            if self.hesaplar is not None:
                return "no usable Diplomacia account token (no requests are sent; see Account Management)"
            return "no token file (no Diplomacia requests are sent; provide a token via Administration > Token)"
        return self._tarama_metni(g, isler)

    def _tarama_metni(self, g: int | None, isler: dict) -> str | None:
        if self._token_duraklatildi():
            if self.hesaplar is not None:
                return "the primary account's token was rejected (401); party/war scans are paused (see Account Management)"
            return "the site rejected the token (401); scans are paused"
        hatali = [t for t, r in isler.items() if r["son_hata"]]
        if hatali:
            ilk = isler[hatali[0]]["son_hata"]
            return esc(f"last scan failed ({', '.join(hatali)}): {ilk[:80]}")
        return None

    def _durum_ekrani(self, o: Olay) -> Yanit:
        g = self._grup(o)
        satirlar = ["<b>Bot Status</b>"]
        isler = {
            r["is_tipi"]: r
            for r in self.conn.execute("SELECT * FROM tarama_isleri WHERE group_id=?", (g,))
        }
        n_oyuncu = self.conn.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=?", (g,)).fetchone()[0]
        n_parti = self.conn.execute("SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", (g,)).fetchone()[0]
        ayar = self.conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (g,)).fetchone()
        satirlar.append(f"Tracked Players: {n_oyuncu}")
        satirlar.append(f"Tracked Parties: {n_parti}")
        satirlar.append(f"Scan Interval: {yonetim.tarama_araligi(self.conn, g)} minutes")
        satirlar.append("Last Successful Scan:")
        for tip, etiket in (("oyuncular", "Players"), ("partiler", "Parties"), ("savaslar", "Wars")):
            r = isler.get(tip)
            satirlar.append(f"• {etiket}: {bicim.tarih(r['son_basari']) if r and r['son_basari'] else 'none yet'}")
        if self._token_duraklatildi():
            sonraki = "paused (token)"
        elif ayar is not None and ayar["sonraki_tarama"]:
            sonraki = bicim.tarih(ayar["sonraki_tarama"])
        else:
            sonraki = "waiting for the first scan"
        satirlar.append(f"Next Scheduled Scan: {esc(sonraki)}")
        calisiyor = bool(self.zamanlayici and self.zamanlayici.calisiyor_mu(g))
        satirlar.append(f"Scan in Progress: {'Yes' if calisiyor else 'No'}")
        bekleyen = self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", (g,)
        ).fetchone()[0]
        satirlar.append(f"Pending Notifications: {bekleyen}")
        if self.hesaplar is not None:
            hs = self.hesaplar.listele()
            oz = self.hesaplar.kapasite_ozeti()
            hazir = sum(1 for h in hs if h["durum"] == "hazir")
            satirlar.append(f"Diplomacia Accounts: {len(hs)} ({hazir} ready)" + (f"; players waiting for an account: {oz['bekleyen']}" if oz["bekleyen"] else ""))
        n_bulunamayan = len(self.servis.bulunamayan_oyuncular(g))
        if n_bulunamayan:
            satirlar.append(f"Not found (consecutive 404s): {n_bulunamayan} players")
        tanimayan = self.conn.execute(
            "SELECT deger FROM sistem_durumu WHERE anahtar=?", (f"savas_taninmayan:{g}",)
        ).fetchone()
        if tanimayan and int(tanimayan[0]) > 0:
            satirlar.append(f"Wars with an unrecognized status: {int(tanimayan[0])} (recorded, not announced)")
        kanal_id = self.kanal.kanal_id if self.kanal is not None else config.LOG_KANAL_ID
        if self.kanal is None or self.kanal.durum is None:
            satirlar.append(f"Notification channel: <code>{kanal_id}</code> (not verified yet)")
        elif self.kanal.durum.hazir:
            satirlar.append(f"Notification channel: <code>{kanal_id}</code> — ready")
        else:
            satirlar.append(f"Notification channel: <code>{kanal_id}</code> — NOT READY: {esc(self.kanal.durum.neden)}")
        baglanti = self._baglanti_metni(g, isler)
        if baglanti:
            satirlar.append(f"Diplomacia connection: {baglanti}")
        return Yanit("\n".join(satirlar), [[Dugme("🔄 Refresh", "du"), Dugme("🏠 Main menu", "m")]])

    # ---- yönetim
    def _yonetim_ekrani(self, o: Olay) -> Yanit:
        satirlar = [
            "<b>Administration</b>",
            "Owner-only panel (this private chat). Tracking is central and independent of Telegram groups; "
            "all tracking notifications are posted to the log channel.",
            "Use /channel to verify the log channel and /lists for the central tracking summary.",
        ]
        if self.hesaplar is not None:
            klavye: Klavye = [[Dugme("🔐 Account Management", "hm")], [Dugme("🏠 Main menu", "m")]]
        else:
            klavye = [[Dugme("🔑 Renew Token", "tk")], [Dugme("🏠 Main menu", "m")]]
        return Yanit("\n".join(satirlar), klavye)

    # ====================== onay işleme =====================================
    async def _b_onay(self, o: Olay, p: list[str]) -> Yanit | None:
        if len(p) < 3 or p[1] not in ("e", "h"):
            return None
        eylem, kid = p[1], p[2]
        op, neden = self.islemler.getir_ayrintili(kid)
        if op is None and neden == SURE_SAAT_DEGISTI:
            return Yanit(SAAT_DEGISTI, uyari="The server clock changed; the action is invalid.", uyari_goster=True)
        if op is None:
            return Yanit(ONAY_BITTI, uyari="The confirmation has expired or has been used.", uyari_goster=True)
        # Kapsam: aynı sohbet + aynı kullanıcı; gruba bağlı işlem kendi grubundan başka yerde kabul edilmez.
        if op.sohbet_id != o.sohbet_id:
            self._kaydet_yetkisiz(o, "buton:baska_sohbet_islemi")
            return Yanit(uyari="This action belongs to another chat/group.", uyari_goster=True, sessiz=True)
        if op.kullanici_id != o.kullanici_id:
            return Yanit(uyari="Only the user who started the action can give this confirmation.", uyari_goster=True, sessiz=True)
        if op.grup_id is not None and not self._onayli(op.grup_id):
            self.islemler.sil(kid)
            return Yanit("The tracking area is no longer active; the action was cancelled.", uyari="Tracking area is not active.", uyari_goster=True)
        if eylem == "h":
            self.islemler.sil(kid)
            return Yanit("Action cancelled.", [[Dugme("🏠 Main menu", "m")]])
        if not self.islemler.sil(kid):  # tek kullanımlık: çift tıklama ikinci kez çalıştırmaz
            return Yanit(ONAY_BITTI, uyari="This confirmation has already been used.", uyari_goster=True)
        return await self._onay_uygula(o, op)

    async def _onay_uygula(self, o: Olay, op) -> Yanit:
        geri = [[Dugme("🏠 Main menu", "m")]]
        v = op.veri
        if op.tur == "oyuncu_sil":
            ok = self.servis.oyuncu_sil(op.grup_id, v["id"])
            return Yanit(
                f"Player removed from tracking: #{esc(v['id'])}" if ok else "The player was already not tracked.",
                [[Dugme("👤 Players", "ol:0"), Dugme("🏠 Main menu", "m")]],
            )
        if op.tur == "parti_sil":
            await self._ack(o)
            s = await self.servis.parti_kaldirma_onayla(op.grup_id, v["parti_id"], self.kaynak, v["ozet"],
                                                       islem_yapan=o.kullanici_id, kurucu_id=self.kurucu_id)
            if s.durum == "silindi":
                satirlar = [f"Party tracking removed. Players removed from tracking: {len(s.silinen)}."]
                satirlar += [f"• {esc(x.ad or x.oyuncu_id)} (#{esc(x.oyuncu_id)})" for x in s.silinen_oyuncular[:15]]
                if len(s.silinen_oyuncular) > 15:
                    satirlar.append(f"… and {len(s.silinen_oyuncular) - 15} more players")
                if s.engellenen:
                    satirlar.append(
                        "They will not be re-added because they appear in another tracked party (a block was set; "
                        "it is lifted if you add them manually):"
                    )
                    satirlar += [f"⛔ {esc(x.ad or x.oyuncu_id)} (#{esc(x.oyuncu_id)})" for x in s.engellenen[:15]]
                    if len(s.engellenen) > 15:
                        satirlar.append(f"… and {len(s.engellenen) - 15} more players")
                return Yanit("\n".join(satirlar), [[Dugme("👥 Parties", "pl:0"), Dugme("🏠 Main menu", "m")]])
            if s.durum == "tekrar_onay" and s.onizleme is not None:
                return self._parti_onay_ekrani(o, op.grup_id, s.onizleme, tekrar=True)
            return Yanit(f"Could not remove party: {esc(s.neden or s.durum)}. No players were removed.", geri)
        if op.tur == "proxy_sil":
            if self.hesaplar is None:
                return Yanit("Unknown action type.", geri)
            try:
                await self.hesaplar.proxy_kaldir(v["hesap"])
            except HesapHatasi as e:
                return Yanit(esc(str(e)), geri)
            return Yanit("The proxy was removed; this account now connects directly (as you confirmed).",
                         [[Dugme("⬅ Account", f"hd:{v['hesap']}"), Dugme("🏠 Main menu", "m")]])
        return Yanit("Unknown action type.", geri)

    # ====================== token yenileme (yalnız kurucu, özel sohbet) =====
    def _token_izni(self, o: Olay, islem_adi: str) -> bool:
        k = yetki.token_yonetimi_izinli_mi(
            self._durum(), sohbet_id=o.sohbet_id, sohbet_turu=o.sohbet_turu, kullanici_id=o.kullanici_id
        )
        if not k:
            self._kaydet_yetkisiz(o, islem_adi)
        return bool(k)

    async def _b_token(self, o: Olay, a: str) -> Yanit | None:
        if not self._token_izni(o, f"token:{a}"):
            return Yanit(uyari="Token management can only be done in the owner's private chat.", uyari_goster=True, sessiz=True)
        assert o.kullanici_id is not None
        if a == "tc":
            self._bekleme_kaldir(o.kullanici_id)
            return Yanit("Token renewal cancelled.", [[Dugme("⚙️ Administration", "yo"), Dugme("🏠 Main menu", "m")]])
        if a == "tn":
            self._bekleme_ayarla(o.kullanici_id)
            return Yanit(
                "Send the new Diplomacia token in <b>a single message</b> (within 5 minutes).\n"
                "I will try to delete the message; the token is not written to the log or the database. "
                "The candidate token is tested with a player request without touching the existing file; "
                "if the test fails, the current token is not replaced.",
                [[Dugme("Cancel", "tc")]],
            )
        # tk: menü (token'ın kendisi asla gösterilmez)
        var = bool(self.token_deposu and self.token_deposu.var_mi())
        mt = self.token_deposu.son_degisiklik() if self.token_deposu else None
        son = yerel_goster(datetime.fromtimestamp(mt, timezone.utc)) if mt else "unknown"
        dogrulayici = "configured" if getattr(self.dogrulayici, "yapilandirildi", True) else "not configured"
        return Yanit(
            "\n".join(
                [
                    "<b>Token management</b> (owner only)",
                    f"Token file: {'present' if var else 'none'}; last modified: {esc(son)}",
                    f"Scans: {'paused due to token' if self._token_duraklatildi() else 'not paused'}",
                    f"Token validator: {dogrulayici}",
                ]
            ),
            [[Dugme("🔑 Send new token", "tn")], [Dugme("⚙️ Administration", "yo"), Dugme("🏠 Main menu", "m")]],
        )

    async def _token_isle(self, o: Olay) -> Yanit:
        if self._token_islem_suruyor:
            await self._mesaji_silmeyi_dene(o)
            return Yanit("A verification is already in progress; please wait for it to finish. This message was not processed.")
        self._token_islem_suruyor = True
        try:
            return await self._token_isle_ic(o)
        finally:
            self._token_islem_suruyor = False

    async def _token_isle_ic(self, o: Olay) -> Yanit:
        token = (o.metin or "").strip()
        sir_kaydet(token)  # aday token doğrulama sırasında da log/hata metinlerinde maskelensin
        # Önce mesajı silmeyi dene (token sohbette kalmasın); sonucu raporla.
        silindi = False
        if o.sil is not None:
            try:
                silindi = bool(await o.sil())
            except Exception:
                silindi = False
        sil_notu = "Your message was deleted." if silindi else "⚠️ Your message could not be deleted; please delete it manually."
        if not token or any(c.isspace() for c in token) or len(token) > 4096:
            return Yanit(f"{sil_notu}\nInvalid format (send a single token without whitespace). Token was NOT CHANGED.")
        if self.token_deposu is None:
            return Yanit(f"{sil_notu}\nToken store is not configured. Token was NOT CHANGED.")
        try:
            sonuc = await self.dogrulayici.dogrula(token)
        except Exception as e:  # hata metni token içerebilir: yalnız sınıf adı
            return Yanit(f"{sil_notu}\nError during verification ({type(e).__name__}). Token was NOT CHANGED.")
        if not sonuc.gecerli:
            nedenler = {
                "yapilandirilmadi": "Token verifier is not configured",
                "gecersiz": "Token rejected",
                "hata": "Could not be verified",
            }
            return Yanit(
                f"{sil_notu}\n{esc(nedenler.get(sonuc.durum, 'Could not be verified'))}: {esc(sonuc.aciklama)}. "
                "The current token was NOT CHANGED."
            )
        try:
            self.token_deposu.yaz(token)
        except Exception as e:
            return Yanit(f"{sil_notu}\nCould not write the token file ({type(e).__name__}). The current token is kept.")
        try:
            with db_islem(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO sistem_durumu(anahtar, deger) VALUES('token_son_guncelleme', ?)",
                    (iso(self.saat()),),
                )
        except Exception:
            log.warning("token güncelleme zamanı kaydedilemedi")
        devam = ""
        if self.zamanlayici is not None:
            self.zamanlayici.token_devam_ettir()
            devam = " Scans are resuming where they left off."
        return Yanit(
            f"{sil_notu}\n✅ The player request with the candidate token succeeded; saved "
            f"(if there was a previous token, it was backed up as .prev; the backup is deleted automatically once the "
            f"new token is confirmed by the first successful scan or after {config.TOKEN_PREV_OMRU_SANIYE // 3600} hours). "
            f"Live behavior has not been verified separately.{devam}",
            [[Dugme("🏠 Main menu", "m")]],
        )

    # ====================== sohbet üyelik olayları ==========================
    def bot_uyelik_degisti(
        self, sohbet_id: int, sohbet_turu: str, eski_durum: str | None, yeni_durum: str | None, ekleyen_id: int | None
    ) -> str:
        """my_chat_member: botun bir GRUBA eklenmesini yalnız kurucuya (özel sohbet, hız sınırlı) bildirir. Hiçbir şey
        oluşturmaz/onaylamaz: gruplar komut işlemez, bildirim almaz ve yeni bir takip alanı yaratmaz; botun yönetici
        olması da hiçbir yetki vermez. Dönüş: 'onaysiz_ekleme' | 'diger'."""
        if sohbet_turu not in ("group", "supergroup"):
            return "diger"
        eklendi = (eski_durum in (None, "left", "kicked")) and yeni_durum in ("member", "administrator")
        if not eklendi:
            return "diger"
        try:
            yonetim.yetkisiz_deneme_kaydet(
                self.conn,
                self.saat,
                self.kurucu_id,
                kullanici_id=ekleyen_id,
                grup_id=sohbet_id,
                islem_adi="onaysiz_gruba_eklendi",
            )
        except Exception:
            log.exception("gruba ekleme kaydı başarısız")
        return "onaysiz_ekleme"
