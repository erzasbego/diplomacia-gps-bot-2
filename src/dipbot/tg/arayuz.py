"""Telegram'dan BAĞIMSIZ komut/düğme mantığı: Olay -> Yanit.

Güvenlik kuralları (dp.txt bölüm 4 ve 12):
- HER komut ve HER düğme tıklaması, güncel DB durumuyla `yetki.karar_ver` ile YENİDEN denetlenir
  (sayısal kimlikler; eski/önbellek yetkisine güvenilmez).
- Özel sohbet yalnız kurucuya açık; grupta onaylı grup + yetkili kullanıcı gerekir.
- Yetkisiz deneme çekirdekteki `yonetim.yetkisiz_deneme_kaydet` ile kaydedilir (kurucuya özel,
  hız sınırlı bildirim).
- Etkili işlemler (silme, yetki/grup/parti kaldırma) onay düğmesiyle yapılır; onay işlemi sohbet +
  kullanıcıya bağlı, süreli ve tek kullanımlıktır.
- Gruba ait her sorgu `group_id` ile süzülür; başka gruba ait kimlik "bulunamadı" döner.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .. import config, outbox, yetki, yonetim
from ..db import islem as db_islem
from ..kaynak import EksikVeri, KaynakHatasi, ZamanlayiciKorumali, hata_aciklamasi
from ..takip import GrupOnaysiz, TakipServisi
from ..maske import sir_kaydet
from ..zaman import (
    SURE_GECERLI, SURE_SAAT_DEGISTI, Saat, ZamanDamgasi, damga_al, monotonik_al, simdi_utc, sure_durumu,
    yerel_goster, iso,
)
from . import bicim
from .bicim import esc
from .islemler import IslemDeposu
from .kaynak_yok import KaynakYapilandirilmadi, yapilandirildi_mi
from .modeller import Dugme, Klavye, Olay, Yanit
from .token import TokenDeposu, TokenDogrulayici, YapilandirilmamisDogrulayici

log = logging.getLogger("dipbot.tg.arayuz")

SAYFA_BOYUTU = 8
TOKEN_BEKLEME_OMRU = timedelta(minutes=5)

# Telegram komut adları yalnız [a-z0-9_] ve en çok 32 karakter. Açıklamalar Türkçe (<=256).
KOMUTLAR: dict[str, str] = {
    "start": "Botu başlat, ana menüyü aç",
    "yardim": "Komutlar ve kullanım yardımı",
    "oyuncu_ekle": "Oyuncuyu takibe ekle (profil linki veya kimlik)",
    "oyuncular": "Takip edilen oyuncuları listele",
    "oyuncu_sil": "Oyuncuyu takipten çıkar (kimlik)",
    "parti_ekle": "Partiyi takibe ekle (parti linki veya kimlik)",
    "partiler": "Takip edilen partileri listele",
    "parti_sil": "Parti takibini kaldır (kimlik)",
    "savaslar": "Aktif savaşları listele",
    "tarama": "Tarama aralığını ayarla: 5, 10, 15 veya 20 dakika",
    "durum": "Bot durumunu göster",
    "yetki_ver": "Kullanıcıya yetki ver (Telegram kullanıcı kimliği)",
    "yetki_al": "Kullanıcının yetkisini al (Telegram kullanıcı kimliği)",
    "grup_onayla": "Grubu onayla (Telegram grup kimliği)",
    "grup_kaldir": "Grup onayını kaldır (Telegram grup kimliği)",
    "grup_tasi": "Göç eden grubun verisini yeni kimliğe taşı (yalnız kurucu, özel sohbet)",
}

_KIMLIK = r"[1-9][0-9]{0,11}"  # kanonik pozitif numara; baştaki sıfır/harf/UUID reddedilir
_URL_DESENI = {
    "player": re.compile(rf"^(?:https?://)?(?:www\.)?diplomacia\.com\.tr/player/({_KIMLIK})/?(?:[?#].*)?$", re.I),
    "party": re.compile(rf"^(?:https?://)?(?:www\.)?diplomacia\.com\.tr/party/({_KIMLIK})/?(?:[?#].*)?$", re.I),
}
_HAM_KIMLIK = re.compile(rf"^{_KIMLIK}$")

_GRUP_KAPSAMLI = frozenset({"ol", "od", "os", "oe", "pl", "pd", "ps", "pe", "sl", "sd", "ta", "tv"})

KAPALI_OZEL = "Bu bot özel sohbette yalnızca kurucuya açıktır."
YETKISIZ = "Bu işlem için yetkiniz yok."
YALNIZ_KURUCU = "Bu işlemi yalnızca kurucu yapabilir."
GRUP_ONAYSIZ_YANIT = "Bu grup artık onaylı değil; işlem yapılmadı."
ESKI_DUGME = "Bu düğme artık geçerli değil. /start ile yeni menü açın."
ONAY_BITTI = "⌛ Bu onayın süresi dolmuş veya zaten kullanılmış. İşlemi yeniden başlatın."
SAAT_DEGISTI = ("⏱ Sunucu saati değişti; güvenlik için bekleyen işlem geçersiz sayıldı ve uygulanmadı. "
                "İşlemi yeniden başlatın.")


def kimlik_coz(arg: str, tur: str) -> str | None:
    """Profil/parti bağlantısından veya düz kimlikten kimliği çıkarır (tur: player|party)."""
    arg = (arg or "").strip()
    if not arg or any(c.isspace() for c in arg):
        return None
    m = _URL_DESENI[tur].match(arg)
    if m:
        return m.group(1)
    return arg if _HAM_KIMLIK.match(arg) else None


def _tam_sayi(arg: str) -> int | None:
    arg = (arg or "").strip()
    if not re.fullmatch(r"-?\d{1,20}", arg):
        return None
    n = int(arg)
    return n if abs(n) <= 2**63 - 1 else None  # SQLite INTEGER sınırı: taşma (OverflowError) önlenir


def _tasima_ozeti(s: dict) -> str:
    return (
        f"Oyuncu: {s['takip_oyuncular']}, parti: {s['takip_partiler']}, parti üyesi: {s['parti_uyeleri']}, "
        f"engel: {s['engeller']}, savaş kaydı: {s['savaslar']}."
    )


def _dg(etiket: str, veri: str) -> Dugme | None:
    """Etiketi kısaltır; callback_data 64 baytı aşarsa düğme yapılmaz (None)."""
    try:
        return Dugme(etiket[:40], veri)
    except ValueError:
        return None


_JWT_BICIMI = re.compile(r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}$")
_SAYFALAMA_NOTU = "<i>Not: Liste sayfalamasız varsayılmıştır (doğrulanmadı).</i>"
_GUC_NOTU = "<i>Not: 'Güç' değerinin hasar olduğu doğrulanmadı.</i>"


class Arayuz:
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
    ):
        self.conn = conn
        # DiplomaciaSource (SinirliKaynak ile sarılmış olması beklenir). Zamanlayıcı varsa istekler onun
        # duraklatma/istek-sınırı/403 geri çekilmesine uyar ve hataları ona iletir (O-d).
        self.kaynak = ZamanlayiciKorumali(kaynak, zamanlayici) if zamanlayici is not None else kaynak
        self.kurucu_id = kurucu_id
        self.saat = saat
        self.servis = TakipServisi(conn, saat)
        self.zamanlayici = zamanlayici
        self.dogrulayici = dogrulayici or YapilandirilmamisDogrulayici()
        self.token_deposu = token_deposu
        self._mono = monotonik_al(saat)
        self.islemler = IslemDeposu(conn, saat, self._mono)
        self._secili: dict[int, int] = {}  # özel sohbet: kullanıcı -> seçili grup (her kullanımda doğrulanır)
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
        return yonetim.durum_yukle(self.conn, self.kurucu_id)

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
        if not k and k.neden != "desteklenmeyen_sohbet":
            self._kaydet_yetkisiz(o, islem_adi)
        return k

    def _red(self, o: Olay, k: yetki.Karar) -> Yanit | None:
        if k.neden == "desteklenmeyen_sohbet":
            return None
        if o.dugme_mi:
            metin = {"ozel_sohbet_kapali": KAPALI_OZEL, "grup_onaysiz": "Bu grup onaylı değil."}.get(
                k.neden, YETKISIZ
            )
            return Yanit(uyari=metin, uyari_goster=True, sessiz=True)
        if k.neden == "ozel_sohbet_kapali":
            return Yanit(KAPALI_OZEL)
        if k.neden == "kullanici_yetkisiz":
            return Yanit(YETKISIZ)
        return None  # onaysız grup: sessiz (spam ve bilgi sızıntısı yok); kurucuya bildirim gitti

    def _kurucu_eylemi(self, o: Olay, eylem: str) -> Yanit | None:
        """Yalnız-kurucu eylemi (yetki.KURUCU_EYLEMLERI): komut ve düğme AYNI politikayı uygular.
        Kurucu değilse yetkisiz deneme kaydı (hız sınırlı bildirim) + anlaşılır yanıt; aksi halde None."""
        k = yetki.eylem_karar_ver(
            self._durum(), sohbet_id=o.sohbet_id, sohbet_turu=o.sohbet_turu,
            kullanici_id=o.kullanici_id, eylem=eylem,
        )
        if k:
            return None
        self._kaydet_yetkisiz(o, f"{'buton:' if o.dugme_mi else '/'}{eylem}")
        if o.dugme_mi:
            return Yanit(uyari=YALNIZ_KURUCU, uyari_goster=True, sessiz=True)
        return Yanit(YALNIZ_KURUCU)

    def _onayli(self, grup_id: int) -> bool:
        return grup_id in self._durum().onayli_gruplar

    def _grup(self, o: Olay) -> int | None:
        """İşlemin hedef grubu: grupta sohbetin kendisi; özel sohbette (kurucu) seçili grup."""
        if o.grup_mu:
            return o.sohbet_id
        onayli = self._durum().onayli_gruplar
        g = self._secili.get(o.kullanici_id) if o.kullanici_id is not None else None
        if g is not None and g in onayli:
            return g
        if o.kullanici_id is not None:
            self._secili.pop(o.kullanici_id, None)
            if len(onayli) == 1:
                (g,) = tuple(onayli)
                self._secili[o.kullanici_id] = g
                return g
        return None

    def _grup_gerekli(self, o: Olay) -> tuple[int | None, Yanit | None]:
        g = self._grup(o)
        if g is not None:
            return g, None
        return None, self._grup_secimi()

    def _grup_secimi(self) -> Yanit:
        onayli = sorted(self._durum().onayli_gruplar)
        if not onayli:
            return Yanit(
                "Henüz onaylı grup yok. Bir grubu onaylamak için: "
                "<code>/grup_onayla &lt;grup_kimligi&gt;</code>"
            )
        satirlar = [[d] for d in (_dg(f"Grup {g}", f"gs:{g}") for g in onayli[:20]) if d]
        satirlar.append([Dugme("Ana menü", "m")])
        return Yanit("İşlem yapmak istediğiniz grubu seçin:", satirlar)

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
        ad = "Diplomacia bağlantısı yapılandırılmadı" if isinstance(e, KaynakYapilandirilmadi) else hata_aciklamasi(e)
        return Yanit(f"{esc(baslik)}: {esc(ad)}. Hiçbir kayıt değiştirilmedi.", [[Dugme("Ana menü", "m")]])

    @staticmethod
    def _sayfa(toplam: int, sayfa: int) -> tuple[int, int, int]:
        sayfa_sayisi = max(1, -(-toplam // SAYFA_BOYUTU))
        sayfa = min(max(sayfa, 0), sayfa_sayisi - 1)
        return sayfa, sayfa_sayisi, sayfa * SAYFA_BOYUTU

    @staticmethod
    def _gezinti(onek: str, sayfa: int, sayfa_sayisi: int) -> list[Dugme]:
        satir = []
        if sayfa > 0:
            satir.append(Dugme("‹ Önceki", f"{onek}:{sayfa - 1}"))
        satir.append(Dugme(f"{sayfa + 1}/{sayfa_sayisi}", "nop"))
        if sayfa < sayfa_sayisi - 1:
            satir.append(Dugme("Sonraki ›", f"{onek}:{sayfa + 1}"))
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
            [[Dugme("✅ Onayla", f"c:e:{kid}"), Dugme("❌ Vazgeç", f"c:h:{kid}")]],
        )

    # ====================== giriş noktaları =================================
    async def komut(self, o: Olay) -> Yanit | None:
        ad = o.komut
        if ad not in KOMUTLAR:
            return None
        k = self._yetki(o, f"/{ad}")
        if not k:
            return self._red(o, k)
        try:
            return await getattr(self, f"_k_{ad}")(o)
        except GrupOnaysiz:  # servis katmanı onaysız grubu reddetti (ör. işlem sırasında onay kalktı)
            return Yanit(GRUP_ONAYSIZ_YANIT)
        except Exception:
            log.exception("komut işlenirken beklenmeyen hata: /%s", ad)
            return Yanit("Beklenmeyen bir hata oluştu; işlem tamamlanmadı. Ayrıntı kurucunun günlüğündedir.")

    async def buton(self, o: Olay) -> Yanit:
        veri = o.veri or ""
        k = self._yetki(o, f"buton:{veri[:30]}")
        if not k:
            return self._red(o, k) or Yanit(uyari=YETKISIZ, uyari_goster=True, sessiz=True)
        parca = veri.split(":", 2)
        try:
            y = await self._buton_yonlendir(o, parca)
        except GrupOnaysiz:
            y = Yanit(GRUP_ONAYSIZ_YANIT, uyari="Grup onaylı değil.", uyari_goster=True)
        except Exception:
            log.exception("düğme işlenirken beklenmeyen hata: %s", parca[0])
            y = Yanit(uyari="Beklenmeyen bir hata oluştu.", uyari_goster=True, sessiz=True)
        if y is None:
            y = Yanit(uyari=ESKI_DUGME, uyari_goster=True, sessiz=True)
        if y.metin and not y.sessiz:
            y.duzenle = True
        return y

    async def metin(self, o: Olay) -> Yanit | None:
        """Düz metin: yalnız kurucunun özel sohbetinde, token bekleniyorsa işlenir."""
        if o.sohbet_turu != "private" or o.kullanici_id is None:
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
                return Yanit("Önce menüden Token yenile'yi seçin; bu mesaj işlenmedi.")
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
                return Yanit("Sunucu saati değişti; güvenlik için token bekleme iptal edildi, mesajınız "
                             "işlenmedi (mevcut token DEĞİŞTİRİLMEDİ). Menüden yeniden başlatın.")
            return Yanit("Token bekleme süresi doldu. Menüden yeniden başlatın.")
        if not _JWT_BICIMI.match(metin):
            # JWT biçimli olmayan düz metin token sayılmaz; bekleme tek kullanımlıktır (tüketilir)
            self._bekleme_kaldir(o.kullanici_id)
            await self._mesaji_silmeyi_dene(o)
            return Yanit("Token biçimi geçersiz (JWT biçiminde tek bir token bekleniyor). Mesajınız silinmeye "
                         "çalışıldı; mevcut token DEĞİŞTİRİLMEDİ. Menüden yeniden başlatın.")
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
    async def _k_start(self, o: Olay) -> Yanit:
        return self._ana_menu(o)

    async def _k_yardim(self, o: Olay) -> Yanit:
        return self._yardim()

    async def _k_oyuncu_ekle(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        oid = kimlik_coz(o.arg, "player")
        if oid is None:
            return Yanit("Kullanım: <code>/oyuncu_ekle https://diplomacia.com.tr/player/72400</code>")
        try:
            s = await self.servis.oyuncu_ekle(g, oid, self.kaynak)
        except KaynakHatasi as e:
            return self._kaynak_hata("Oyuncu eklenemedi", e)
        if s.durum == "zaten_takipte":
            return Yanit(f"Bu oyuncu zaten takipte: {bicim.oyuncu_link(s.oyuncu_id)}", [[Dugme("Oyuncular", "ol:0")]])
        return Yanit(
            "\n".join(
                [
                    "✅ <b>Oyuncu takibe eklendi</b>",
                    bicim.oyuncu_link(oid, s.ad),
                    f"Bölge: {esc(s.bolge or 'bilinmiyor')}",
                    f"Vatandaşlık ülkesi: {esc(s.vatandaslik_ulkesi or 'bilinmiyor')}",
                    "<i>Bu bir başlangıç özetidir; değişiklik bildirimi değildir.</i>",
                ]
            ),
            [[Dugme("Oyuncular", "ol:0"), Dugme("Ana menü", "m")]],
        )

    async def _k_oyuncular(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        return r or self._oyuncu_liste(g, 0)

    async def _k_oyuncu_sil(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        oid = kimlik_coz(o.arg, "player")
        if oid is None:
            return Yanit("Kullanım: <code>/oyuncu_sil &lt;oyuncu_kimligi&gt;</code>")
        return self._oyuncu_sil_onayi(o, g, oid)

    async def _k_parti_ekle(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        pid = kimlik_coz(o.arg, "party")
        if pid is None:
            return Yanit("Kullanım: <code>/parti_ekle https://diplomacia.com.tr/party/5830</code>")
        try:
            s = await self.servis.parti_ekle(g, pid, self.kaynak)
        except KaynakHatasi as e:
            return self._kaynak_hata("Parti eklenemedi", e)
        if s.durum == "zaten_takipte":
            return Yanit(f"Bu parti zaten takipte: {bicim.parti_link(pid)}", [[Dugme("Partiler", "pl:0")]])
        return Yanit(
            "\n".join(
                [
                    "✅ <b>Parti takibe eklendi</b>",
                    bicim.parti_link(pid, s.ad),
                    f"Üye sayısı: {s.uye_sayisi}",
                    f"Takibe yeni alınan: {s.yeni_eklenen}; zaten takipte: {s.zaten_takipte}; "
                    f"engelli (otomatik eklenmedi): {s.engelli}",
                    "<i>Bu bir başlangıç durumudur; katılım bildirimi üretilmez.</i>",
                    "Üyelerin ayrıntıları ilk taramada okunacak.",
                ]
            ),
            [[Dugme("Partiler", "pl:0"), Dugme("Ana menü", "m")]],
        )

    async def _k_partiler(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        return r or self._parti_liste(g, 0)

    async def _k_parti_sil(self, o: Olay) -> Yanit:
        g, r = self._grup_gerekli(o)
        if r:
            return r
        pid = kimlik_coz(o.arg, "party")
        if pid is None:
            return Yanit("Kullanım: <code>/parti_sil &lt;parti_kimligi&gt;</code>")
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

    async def _k_yetki_ver(self, o: Olay) -> Yanit:
        hedef = _tam_sayi(o.arg)
        if hedef is None or hedef <= 0:
            return Yanit("Kullanım: <code>/yetki_ver &lt;telegram_kullanici_kimligi&gt;</code> (pozitif sayı)")
        try:
            yeni = yonetim.yetki_ver(self.conn, self.saat, self.kurucu_id, o.kullanici_id, hedef)
        except yonetim.YetkiHatasi:
            return Yanit(YETKISIZ)
        except yonetim.YonetimHatasi as e:
            return Yanit(esc(f"Yetki verilemedi: {e}"))
        if hedef == self.kurucu_id:
            return Yanit("Kurucu zaten her zaman yetkilidir.")
        return Yanit(
            f"Yetki verildi: <code>{hedef}</code>. (Özel sohbet yalnız kurucuya açıktır.)"
            if yeni
            else f"Kullanıcı zaten yetkili: <code>{hedef}</code>."
        )

    async def _k_yetki_al(self, o: Olay) -> Yanit:
        hedef = _tam_sayi(o.arg)
        if hedef is None or hedef <= 0:
            return Yanit("Kullanım: <code>/yetki_al &lt;telegram_kullanici_kimligi&gt;</code> (pozitif sayı)")
        return self._yetki_al_onayi(o, hedef)

    async def _k_grup_onayla(self, o: Olay) -> Yanit:
        gid = _tam_sayi(o.arg)
        if gid is None or gid >= 0:
            return Yanit("Kullanım: <code>/grup_onayla &lt;telegram_grup_kimligi&gt;</code> (negatif sayı)")
        try:
            yeni = yonetim.grup_onayla(self.conn, self.saat, self.kurucu_id, o.kullanici_id, gid)
        except yonetim.YetkiHatasi:
            return Yanit(YETKISIZ)
        return Yanit(
            f"Grup onaylandı: <code>{gid}</code>. Grupta oyuncu/parti ekleyebilirsiniz."
            if yeni
            else f"Grup zaten onaylı: <code>{gid}</code>."
        )

    async def _k_grup_kaldir(self, o: Olay) -> Yanit:
        gid = _tam_sayi(o.arg)
        if gid is None or gid >= 0:
            return Yanit("Kullanım: <code>/grup_kaldir &lt;telegram_grup_kimligi&gt;</code> (negatif sayı)")
        r = self._kurucu_eylemi(o, "grup_kaldir")
        if r is not None:
            return r
        return self._grup_kaldir_onayi(o, gid)

    async def _k_grup_tasi(self, o: Olay) -> Yanit:
        """Kayıtlı göç çiftinin verisini taşıma isteği: YALNIZ kurucunun özel sohbeti; onay düğmeyle."""
        r = self._kurucu_eylemi(o, "grup_tasi")
        if r is not None:
            return r
        if o.grup_mu:
            return Yanit("Grup verisi taşıma yalnızca kurucunun özel sohbetinde yapılabilir.")
        kullanim = (
            "Kullanım: <code>/grup_tasi &lt;eski_kimlik&gt; &lt;yeni_kimlik&gt;</code> "
            "(ikisi de negatif; Telegram göç olayıyla kayıtlı çift)"
        )
        parcalar = (o.arg or "").split()
        kimlikler = [_tam_sayi(x) for x in parcalar] if len(parcalar) == 2 else []
        if len(kimlikler) != 2 or None in kimlikler or kimlikler[0] >= 0 or kimlikler[1] >= 0 \
                or kimlikler[0] == kimlikler[1]:
            return Yanit(kullanim)
        eski, yeni = kimlikler
        try:
            sayilar = yonetim.tasima_degerlendir(self.conn, eski, yeni)
        except yonetim.TasimaHatasi as e:
            return Yanit(f"Taşıma yapılamadı: {esc(str(e))}. Hiçbir şey değiştirilmedi.")
        return self._onay_yaniti(
            o, "grup_tasi", None, {"eski": eski, "yeni": yeni},
            f"<b>Grup verisi taşınsın mı?</b>\nEski: <code>{eski}</code> → Yeni: <code>{yeni}</code>\n"
            f"{_tasima_ozeti(sayilar)}\n"
            "Tek işlemde taşınır; eski kimlikte veri kalmaz, eski kimliğin bekleyen bildirimleri iptal edilir.",
        )

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
        if a == "gl":
            return self._grup_secimi() if not o.grup_mu else None
        if a == "gs":
            return self._grup_sec(o, _tam_sayi(arg))
        if a == "c":
            return await self._b_onay(o, p)
        if a in ("tk", "tn", "tc"):
            return await self._b_token(o, a)
        if a == "yk":
            hedef = _tam_sayi(arg)
            return self._yetki_al_onayi(o, hedef) if hedef is not None else None
        if a == "gk":
            r = self._kurucu_eylemi(o, "grup_kaldir")
            if r is not None:
                return r
            if o.grup_mu:
                return None  # grup listesi yalnız kurucunun özel sohbetinde gösterilir
            gid = _tam_sayi(arg)
            return self._grup_kaldir_onayi(o, gid) if gid is not None else None

        # Buradan sonrası grup kapsamlı (bilinmeyen/eski düğme grup seçimine DÜŞMEZ)
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
                "Oyuncu eklemek için:\n<code>/oyuncu_ekle https://diplomacia.com.tr/player/72400</code>",
                [[Dugme("Oyuncular", "ol:0"), Dugme("Ana menü", "m")]],
            )
        if a == "pl":
            return self._parti_liste(g, sayfa)
        if a == "pd":
            return self._parti_detay(g, arg)
        if a == "ps":
            return await self._parti_sil_baslat(o, g, arg)
        if a == "pe":
            return Yanit(
                "Parti eklemek için:\n<code>/parti_ekle https://diplomacia.com.tr/party/5830</code>",
                [[Dugme("Partiler", "pl:0"), Dugme("Ana menü", "m")]],
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
        satirlar = ["<b>Komutlar</b>"]
        ornek = {
            "oyuncu_ekle": "&lt;profil_linki&gt;",
            "oyuncu_sil": "&lt;oyuncu_kimligi&gt;",
            "parti_ekle": "&lt;parti_linki&gt;",
            "parti_sil": "&lt;parti_kimligi&gt;",
            "tarama": "&lt;5|10|15|20&gt;",
            "yetki_ver": "&lt;kullanici_kimligi&gt;",
            "yetki_al": "&lt;kullanici_kimligi&gt;",
            "grup_onayla": "&lt;grup_kimligi&gt;",
            "grup_kaldir": "&lt;grup_kimligi&gt;",
            "grup_tasi": "&lt;eski_kimlik&gt; &lt;yeni_kimlik&gt;",
        }
        for ad, aciklama in KOMUTLAR.items():
            if ad in ("start",):
                continue
            satirlar.append(f"/{ad} {ornek.get(ad, '')}".rstrip() + f" — {esc(aciklama)}")
        satirlar.append("")
        satirlar.append(
            "<i>Kontroller aralıklıdır; bildirimdeki zaman tespit zamanıdır, kesin değişiklik zamanı değildir. "
            "Özel sohbet yalnız kurucuya açıktır.</i>"
        )
        return Yanit("\n".join(satirlar), [[Dugme("Ana menü", "m")]])

    def _ana_menu(self, o: Olay) -> Yanit:
        satirlar = ["<b>Diplomacia GPS Botu</b>", "Bir bölüm seçin."]
        if not o.grup_mu:
            g = self._grup(o)
            satirlar.append(f"Seçili grup: <code>{g}</code>" if g is not None else "Seçili grup yok.")
        klavye: Klavye = [
            [Dugme("Oyuncular", "ol:0"), Dugme("Partiler", "pl:0")],
            [Dugme("Savaşlar", "sl:0"), Dugme("Tarama Ayarları", "ta")],
            [Dugme("Bot Durumu", "du"), Dugme("Yönetim", "yo")],
            [Dugme("Yardım", "yd")],
        ]
        if not o.grup_mu:
            klavye.append([Dugme("Grup Seç", "gl")])
        return Yanit("\n".join(satirlar), klavye)

    def _grup_sec(self, o: Olay, gid: int | None) -> Yanit | None:
        if o.grup_mu or gid is None or o.kullanici_id is None:
            return None
        if not self._onayli(gid):
            return Yanit(uyari="Bu grup artık onaylı değil.", uyari_goster=True, sessiz=True)
        self._secili[o.kullanici_id] = gid
        return self._ana_menu(o)

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
                "Bu grupta takip edilen oyuncu yok.\nEklemek için: "
                "<code>/oyuncu_ekle &lt;profil_linki&gt;</code>",
                [[Dugme("Oyuncu nasıl eklenir?", "oe")], [Dugme("Ana menü", "m")]],
            )
        sayfa, sayfa_sayisi, bas = self._sayfa(toplam, sayfa)
        dilim = satirlar_db[bas : bas + SAYFA_BOYUTU]
        metin = [f"<b>Takip edilen oyuncular</b> (toplam {toplam})"]
        klavye: Klavye = []
        for i, r in enumerate(dilim, start=bas + 1):
            metin.append(f"{i}. {bicim.oyuncu_link(r['oyuncu_id'], r['ad'])} (#{esc(r['oyuncu_id'])})")
            d = _dg(f"{i}. {r['ad'] or r['oyuncu_id']} #{r['oyuncu_id']}", f"od:{r['oyuncu_id']}")
            if d:
                klavye.append([d])
        klavye.append(self._gezinti("ol", sayfa, sayfa_sayisi))
        klavye.append([Dugme("Ana menü", "m")])
        return Yanit("\n".join(metin), klavye)

    def _oyuncu_detay(self, g: int, oid: str) -> Yanit:
        r = self.conn.execute(
            "SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)
        ).fetchone()
        if r is None:
            return Yanit(uyari="Bu oyuncu bu grupta takipte değil (silinmiş olabilir).", uyari_goster=True, sessiz=True)
        metin = [
            f"<b>Oyuncu</b>: {bicim.oyuncu_link(oid, r['ad'])} (#{esc(oid)})",
            f"Bölge: {esc(r['bolge'] or 'bilinmiyor')}",
            f"Vatandaşlık ülkesi: {esc(r['vatandaslik_ulkesi'] or 'bilinmiyor')}",
            f"Son gözlem: {bicim.tarih(r['son_gozlem'])}",
            "Ekleme: " + ("elle" if r["elle_eklendi"] else "parti üyeliğiyle"),
        ]
        if oid in self.servis.bulunamayan_oyuncular(g):
            metin.append(
                "⚠ Bulunamıyor: art arda birkaç taramada kaynakta bulunamadı (kayıt silinmedi; "
                "gerekirse elle takipten çıkarın)."
            )
        klavye: Klavye = []
        sil = _dg("🗑 Takipten çıkar", f"os:{oid}")
        if sil:
            klavye.append([sil])
        klavye.append([Dugme("‹ Liste", "ol:0"), Dugme("Ana menü", "m")])
        return Yanit("\n".join(metin), klavye)

    def _oyuncu_sil_onayi(self, o: Olay, g: int, oid: str) -> Yanit:
        r = self.conn.execute(
            "SELECT ad FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)
        ).fetchone()
        if r is None:
            return self._bilgi(o, "Bu oyuncu bu grupta takipte değil.")
        return self._onay_yaniti(
            o,
            "oyuncu_sil",
            g,
            {"id": oid},
            f"<b>Oyuncu takipten çıkarılsın mı?</b>\n{bicim.oyuncu_link(oid, r['ad'])} (#{esc(oid)})\n"
            "Yalnız bu grubun takibinden çıkar. Takip edilen bir partinin üyesiyse otomatik yeniden eklenmez.",
        )

    # ---- partiler
    def _parti_liste(self, g: int, sayfa: int) -> Yanit:
        satirlar_db = self.conn.execute(
            "SELECT t.parti_id, t.ad, (SELECT COUNT(*) FROM parti_uyeleri u WHERE u.group_id=t.group_id"
            " AND u.parti_id=t.parti_id) AS uye FROM takip_partiler t WHERE t.group_id=?"
            " ORDER BY lower(COALESCE(t.ad, '')), t.parti_id",
            (g,),
        ).fetchall()
        toplam = len(satirlar_db)
        if toplam == 0:
            return Yanit(
                "Bu grupta takip edilen parti yok.\nEklemek için: <code>/parti_ekle &lt;parti_linki&gt;</code>",
                [[Dugme("Parti nasıl eklenir?", "pe")], [Dugme("Ana menü", "m")]],
            )
        sayfa, sayfa_sayisi, bas = self._sayfa(toplam, sayfa)
        metin = [f"<b>Takip edilen partiler</b> (toplam {toplam})"]
        klavye: Klavye = []
        for i, r in enumerate(satirlar_db[bas : bas + SAYFA_BOYUTU], start=bas + 1):
            metin.append(
                f"{i}. {bicim.parti_link(r['parti_id'], r['ad'])} (#{esc(r['parti_id'])}) — {r['uye']} üye"
            )
            d = _dg(f"{i}. {r['ad'] or r['parti_id']} #{r['parti_id']}", f"pd:{r['parti_id']}")
            if d:
                klavye.append([d])
        klavye.append(self._gezinti("pl", sayfa, sayfa_sayisi))
        klavye.append([Dugme("Ana menü", "m")])
        return Yanit("\n".join(metin), klavye)

    def _parti_detay(self, g: int, pid: str) -> Yanit:
        r = self.conn.execute(
            "SELECT * FROM takip_partiler WHERE group_id=? AND parti_id=?", (g, pid)
        ).fetchone()
        if r is None:
            return Yanit(uyari="Bu parti bu grupta takipte değil (kaldırılmış olabilir).", uyari_goster=True, sessiz=True)
        uye = self.conn.execute(
            "SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (g, pid)
        ).fetchone()[0]
        metin = [
            f"<b>Parti</b>: {bicim.parti_link(pid, r['ad'])} (#{esc(pid)})",
            f"Bilinen üye sayısı: {uye}",
            f"Son doğrulama: {bicim.tarih(r['son_dogrulama'])}",
        ]
        klavye: Klavye = []
        sil = _dg("🗑 Parti takibini kaldır", f"ps:{pid}")
        if sil:
            klavye.append([sil])
        klavye.append([Dugme("‹ Liste", "pl:0"), Dugme("Ana menü", "m")])
        return Yanit("\n".join(metin), klavye)

    async def _parti_sil_baslat(self, o: Olay, g: int, pid: str) -> Yanit:
        await self._ack(o)
        on = await self.servis.parti_kaldirma_hazirla(g, pid, self.kaynak)
        if not on.basarili:
            return Yanit(
                f"Parti kaldırılamadı: {esc(on.neden)}", [[Dugme("Partiler", "pl:0"), Dugme("Ana menü", "m")]]
            )
        return self._parti_onay_ekrani(o, g, on, tekrar=False)

    def _parti_onay_ekrani(self, o: Olay, g: int, on, *, tekrar: bool) -> Yanit:
        adlar = {}
        for oid in on.etkilenen[:15]:
            r = self.conn.execute(
                "SELECT ad FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)
            ).fetchone()
            adlar[oid] = r["ad"] if r else None
        satirlar = ["<b>Parti takibi kaldırılsın mı?</b>"]
        if tekrar:
            satirlar.append("⚠️ Üyelik onay sırasında değişti; güncel özeti yeniden onaylayın.")
        satirlar += [
            f"Parti: {bicim.parti_link(on.parti_id, on.parti_ad)} (#{esc(on.parti_id)})",
            f"Güncel üye sayısı: {on.uye_sayisi}",
            f"Bu grubun takibinden çıkarılacak oyuncu: <b>{len(on.etkilenen)}</b>",
        ]
        for oid, ad in adlar.items():
            satirlar.append(f"• {esc(ad or oid)} (#{esc(oid)})")
        if len(on.etkilenen) > len(adlar):
            satirlar.append(f"… ve {len(on.etkilenen) - len(adlar)} oyuncu daha")
        satirlar.append(
            "Partiden önceden ayrılmış oyuncuların takibi sürer; diğer gruplar etkilenmez. "
            "Elle eklenmiş olsalar da şu anki üyeler çıkarılır."
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
            return self._savas_liste_yedek(g, EksikVeri('savaş listesi tamamlanmadı'))
        savaslar = liste.savaslar
        toplam = len(savaslar)
        simdi = self.saat()
        ust = [f"<b>Aktif savaşlar</b> (toplam {toplam})", f"Güncellik: {yerel_goster(simdi)} (az önce alındı)"]
        if toplam == 0:
            return Yanit("\n".join(ust + ["Şu an aktif savaş görünmüyor.", _SAYFALAMA_NOTU]), [[Dugme("Yenile", "sl:0"), Dugme("Ana menü", "m")]])
        sayfa, sayfa_sayisi, bas = self._sayfa(toplam, sayfa)
        klavye: Klavye = []
        for i, s in enumerate(savaslar[bas : bas + SAYFA_BOYUTU], start=bas + 1):
            baslik = bicim.savas_baglantili(s.ad, s.war_number, s.attacker_name, s.defender_name, s.savas_id)
            if bicim.savas_baglantisi(s.war_number) is None:
                baslik += f" (#{esc(s.savas_id)})"  # numara yok: kimlik gösterilir
            ek = [bicim.durum_etiketi(s.durum)] if s.durum else []
            if s.tur:
                ek.append(bicim.tur_etiketi(s.tur))
            ust.append(f"{i}. {baslik}" + (f" — {', '.join(ek)}" if ek else ""))
            d = _dg(f"{i}. {s.ad or s.war_number or s.savas_id}", f"sd:{s.savas_id}")
            if d:
                klavye.append([d])
        ust.append(f'<a href="{esc(bicim.SITE)}/wars">Savaşlar sayfası</a>')
        ust.append(_SAYFALAMA_NOTU)
        klavye.append(self._gezinti("sl", sayfa, sayfa_sayisi))
        klavye.append([Dugme("Yenile", f"sl:{sayfa}"), Dugme("Ana menü", "m")])
        return Yanit("\n".join(ust), klavye)

    def _savas_liste_yedek(self, g: int, e: BaseException) -> Yanit:
        neden = "Diplomacia bağlantısı yapılandırılmadı" if isinstance(e, KaynakYapilandirilmadi) else hata_aciklamasi(e)
        satirlar = ["<b>Savaşlar</b>", f"⚠️ Güncel veri alınamadı ({esc(neden)}): veriye erişilemedi."]
        r = self.conn.execute(
            "SELECT son_basari FROM tarama_isleri WHERE group_id=? AND is_tipi='savaslar'", (g,)
        ).fetchone()
        satirlar.append(f"Son başarılı savaş taraması: {bicim.tarih(r['son_basari']) if r and r['son_basari'] else 'yok'}")
        bilinen = self.conn.execute(
            "SELECT savas_id, ad, ilk_gorulme FROM savaslar WHERE group_id=? ORDER BY ilk_gorulme DESC LIMIT ?",
            (g, SAYFA_BOYUTU),
        ).fetchall()
        if bilinen:
            satirlar.append("Grup kaydındaki bilinen savaşlar (eski kayıt; bitmiş olabilir):")
            for b in bilinen:
                satirlar.append(f"• {esc(b['ad'] or b['savas_id'])} — ilk görülme {bicim.tarih(b['ilk_gorulme'])}")
        return Yanit("\n".join(satirlar), [[Dugme("Yenile", "sl:0"), Dugme("Ana menü", "m")]])

    async def _savas_detay(self, o: Olay, sid: str) -> Yanit:
        if not sid:
            return None  # type: ignore[return-value]
        await self._ack(o)
        gv = await self.servis.savas_detayi(sid, self.kaynak)
        klavye = [[d for d in (_dg("Yenile", f"sd:{sid}"),) if d] + [Dugme("‹ Liste", "sl:0")], [Dugme("Ana menü", "m")]]
        neden = ""
        if gv.hata:
            neden = "Diplomacia bağlantısı yapılandırılmadı" if "yapılandırılmadı" in gv.hata else gv.hata
        if gv.detay is None:
            return Yanit(
                f"<b>Savaş</b> #{esc(sid)}\n⚠️ Veriye erişilemedi ({esc(neden)}). Daha önce alınmış veri yok.",
                klavye,
            )
        d = gv.detay
        sad = d.taraflar[0].ad if len(d.taraflar) > 1 else None
        sav = d.taraflar[1].ad if len(d.taraflar) > 1 else None
        baslik = bicim.savas_baglantili(d.ad, d.war_number, sad, sav, sid)
        if bicim.savas_baglantisi(d.war_number) is None:
            baslik += f" (#{esc(sid)})"
        satirlar = [f"<b>Savaş</b>: {baslik}", f"Durum: {bicim.durum_etiketi(d.durum)}"]
        if d.tur:
            satirlar.append(f"Tür: {bicim.tur_etiketi(d.tur)}")
        if d.taraflar:
            satirlar.append("Taraflar:")
            for t in d.taraflar:
                guc = "veri yok" if t.guc is None else f"{t.guc:,}".replace(",", ".")
                satirlar.append(f"• {esc(t.ad or 'bilinmiyor')} — Güç: {guc}")
            satirlar.append(_GUC_NOTU)
        else:
            satirlar.append("Taraf bilgisi: veri yok")
        satirlar.append(f'<a href="{esc(bicim.SITE)}/wars">Savaşlar sayfası</a>')
        zaman = yerel_goster(gv.zaman) if gv.zaman else "bilinmiyor"
        if gv.guncel:
            satirlar.append(f"Güncellik: {zaman} (az önce alındı)")
        else:
            satirlar.append(f"⚠️ Güncel veri alınamadı ({esc(neden)}). Gösterilen veri {zaman} tarihlidir.")
        satirlar.append("<i>Güç değişimleri ve durum geçişleri için otomatik bildirim gönderilmez.</i>")
        return Yanit("\n".join(satirlar), klavye)

    # ---- tarama ayarı
    def _tarama_ekrani(self, g: int, not_: str = "") -> Yanit:
        mevcut = yonetim.tarama_araligi(self.conn, g)
        satirlar = [f"<b>Tarama ayarları</b>", f"Mevcut aralık: {mevcut} dakika"]
        if not_:
            satirlar.append(not_)
        satirlar.append("Seçilen aralık oyuncu, parti ve yeni savaş kontrollerinde geçerlidir.")
        satir = [
            Dugme(("✓ " if dk == mevcut else "") + f"{dk} dk", f"tv:{dk}")
            for dk in config.ALLOWED_SCAN_INTERVALS_MINUTES
        ]
        return Yanit("\n".join(satirlar), [satir, [Dugme("Ana menü", "m")]])

    def _tarama_ayarla(self, g: int, dk: int | None) -> Yanit:
        izinli = config.ALLOWED_SCAN_INTERVALS_MINUTES
        if dk is None or dk not in izinli:
            return Yanit(
                f"Geçersiz aralık. İzinli değerler: {', '.join(map(str, izinli))} (dakika).",
                uyari="Geçersiz aralık", uyari_goster=True, sessiz=True,
            )
        try:
            yonetim.tarama_araligi_ayarla(self.conn, g, dk)
        except (ValueError, yonetim.YonetimHatasi) as e:
            return Yanit(uyari=f"Ayarlanamadı: {e}"[:200], uyari_goster=True, sessiz=True)
        return self._tarama_ekrani(g, f"✅ Aralık {dk} dakika olarak ayarlandı (bir sonraki taramadan itibaren).")

    # ---- durum
    def _token_duraklatildi(self) -> bool:
        if self.zamanlayici is not None:
            return bool(self.zamanlayici.token_duraklatildi)
        r = self.conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='token_duraklatildi'").fetchone()
        return bool(r and r[0] == "1")

    def _baglanti_metni(self, g: int | None, isler: dict) -> str:
        if not yapilandirildi_mi(self.kaynak):
            return "yapılandırılmadı"
        ic = getattr(self.kaynak, "_ic", self.kaynak)
        if getattr(ic, "token_hazir", True) is False:
            return "token dosyası yok (Diplomacia isteği atılmıyor; Yönetim > Token menüsünden token verin)"
        if getattr(ic, "canli_dogrulanmadi", False):
            return ("yapılandırıldı (kimlik biçimi kullanıcı DevTools'ta doğruladı, canlı bağlantı test edilmedi); "
                    + self._tarama_metni(g, isler))
        return self._tarama_metni(g, isler)

    def _tarama_metni(self, g: int | None, isler: dict) -> str:
        if self._token_duraklatildi():
            return "site token'ı reddetti (401); taramalar duraklatıldı"
        if g is None:
            return "yapılandırıldı (grup seçilmediği için tarama durumu gösterilemiyor)"
        hatali = [t for t, r in isler.items() if r["son_hata"]]
        if hatali:
            ilk = isler[hatali[0]]["son_hata"]
            return esc(f"son tarama hatalı ({', '.join(hatali)}): {ilk[:80]}")
        if any(r["son_basari"] for r in isler.values()):
            return "son taramalar başarılı"
        return "henüz tarama yapılmadı"

    def _durum_ekrani(self, o: Olay) -> Yanit:
        g = self._grup(o)
        d = self._durum()
        satirlar = ["<b>Bot durumu</b>"]
        isler: dict = {}
        if g is not None:
            isler = {
                r["is_tipi"]: r
                for r in self.conn.execute("SELECT * FROM tarama_isleri WHERE group_id=?", (g,))
            }
            n_oyuncu = self.conn.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=?", (g,)).fetchone()[0]
            n_parti = self.conn.execute("SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", (g,)).fetchone()[0]
            ayar = self.conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (g,)).fetchone()
            satirlar.append(f"Grup: <code>{g}</code>")
            satirlar.append(f"Oyuncu sayısı: {n_oyuncu}")
            satirlar.append(f"Parti sayısı: {n_parti}")
            satirlar.append(f"Tarama aralığı: {yonetim.tarama_araligi(self.conn, g)} dakika")
            son = []
            for tip, etiket in (("oyuncular", "oyuncular"), ("partiler", "partiler"), ("savaslar", "savaşlar")):
                r = isler.get(tip)
                son.append(f"{etiket}: {bicim.tarih(r['son_basari']) if r and r['son_basari'] else 'henüz yok'}")
            satirlar.append("Son başarılı kontrol: " + "; ".join(son))
            if self._token_duraklatildi():
                sonraki = "duraklatıldı (token)"
            elif ayar is not None and ayar["sonraki_tarama"]:
                sonraki = bicim.tarih(ayar["sonraki_tarama"])
            else:
                sonraki = "ilk tarama bekleniyor"
            satirlar.append(f"Sonraki planlanan kontrol: {esc(sonraki)}")
            calisiyor = bool(self.zamanlayici and self.zamanlayici.calisiyor_mu(g))
            satirlar.append(f"Devam eden tarama: {'evet' if calisiyor else 'hayır'}")
            bekleyen = self.conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", (g,)
            ).fetchone()[0]
            satirlar.append(f"Bekleyen bildirim: {bekleyen}")
            n_bulunamayan = len(self.servis.bulunamayan_oyuncular(g))
            if n_bulunamayan:
                satirlar.append(f"Bulunamıyor (art arda 404): {n_bulunamayan} oyuncu")
            tanimayan = self.conn.execute(
                "SELECT deger FROM sistem_durumu WHERE anahtar=?", (f"savas_taninmayan:{g}",)
            ).fetchone()
            if tanimayan and int(tanimayan[0]) > 0:
                satirlar.append(f"Durumu tanınmayan savaş: {int(tanimayan[0])} (kaydedildi, bildirilmedi)")
            satirlar.append(_SAYFALAMA_NOTU)
        else:
            satirlar.append("Grup seçilmedi (özel sohbet): genel durum.")
        if not o.grup_mu:
            satirlar.append(f"Onaylı grup: {len(d.onayli_gruplar)}; yetkili kullanıcı: {len(d.yetkililer)}")
        satirlar.append(f"Diplomacia bağlantısı: {self._baglanti_metni(g, isler)}")
        return Yanit("\n".join(satirlar), [[Dugme("Yenile", "du"), Dugme("Ana menü", "m")]])

    # ---- yönetim
    def _yonetim_ekrani(self, o: Olay) -> Yanit:
        d = self._durum()
        ozel = not o.grup_mu
        satirlar = ["<b>Yönetim</b>"]
        klavye: Klavye = []
        yetkililer = sorted(d.yetkililer)
        satirlar.append(f"Yetkili kullanıcılar ({len(yetkililer)}):" if yetkililer else "Ek yetkili kullanıcı yok.")
        for uid in yetkililer[:10]:
            satirlar.append(f"• <code>{uid}</code>")
            b = _dg(f"Yetkiyi kaldır #{uid}", f"yk:{uid}")
            if b:
                klavye.append([b])
        if len(yetkililer) > 10:
            satirlar.append("… (tamamı için /yetki_al komutunu kullanın)")
        if ozel:
            gruplar = sorted(d.onayli_gruplar)
            satirlar.append(f"Onaylı gruplar ({len(gruplar)}):" if gruplar else "Onaylı grup yok.")
            for gid in gruplar[:10]:
                satirlar.append(f"• <code>{gid}</code>")
                b = _dg(f"Onayı kaldır {gid}", f"gk:{gid}")
                if b:
                    klavye.append([b])
        satirlar.append(
            "Yetki vermek: <code>/yetki_ver &lt;kullanici_kimligi&gt;</code>\n"
            "Grup onaylamak: <code>/grup_onayla &lt;grup_kimligi&gt;</code>"
        )
        if ozel:
            klavye.append([Dugme("Token Yenile", "tk"), Dugme("Grup Seç", "gl")])
        klavye.append([Dugme("Ana menü", "m")])
        return Yanit("\n".join(satirlar), klavye)

    def _yetki_al_onayi(self, o: Olay, hedef: int) -> Yanit:
        k = yetki.yetki_alinabilir_mi(self._durum(), hedef)
        if not k:
            return self._bilgi(
                o, "Kurucunun yetkisi alınamaz." if k.neden == "kurucu_yetkisi_alinamaz" else YETKISIZ
            )
        if hedef not in self._durum().yetkililer:
            return self._bilgi(o, f"Bu kullanıcı yetkili listesinde değil: {hedef}.")
        return self._onay_yaniti(
            o, "yetki_al", None, {"hedef": hedef},
            f"<b>Yetki kaldırılsın mı?</b>\nKullanıcı: <code>{hedef}</code>\n"
            "Eski düğmeleriyle de işlem yapamaz.",
        )

    def _grup_kaldir_onayi(self, o: Olay, gid: int) -> Yanit:
        if not self._onayli(gid):
            return self._bilgi(o, f"Bu grup zaten onaylı değil: {gid}.")
        return self._onay_yaniti(
            o, "grup_kaldir", None, {"grup": gid},
            f"<b>Grup onayı kaldırılsın mı?</b>\nGrup: <code>{gid}</code>\n"
            "Taramalar ve bekleyen bildirimler durur; takip verisi silinmez (yeniden onayda korunur).",
        )

    # ====================== onay işleme =====================================
    async def _b_onay(self, o: Olay, p: list[str]) -> Yanit | None:
        if len(p) < 3 or p[1] not in ("e", "h"):
            return None
        eylem, kid = p[1], p[2]
        op, neden = self.islemler.getir_ayrintili(kid)
        if op is None and neden == SURE_SAAT_DEGISTI:
            return Yanit(SAAT_DEGISTI, uyari="Sunucu saati değişti; işlem geçersiz.", uyari_goster=True)
        if op is None:
            return Yanit(ONAY_BITTI, uyari="Onayın süresi dolmuş veya kullanılmış.", uyari_goster=True)
        # Kapsam: aynı sohbet + aynı kullanıcı; gruba bağlı işlem kendi grubundan başka yerde kabul edilmez.
        if op.sohbet_id != o.sohbet_id:
            self._kaydet_yetkisiz(o, "buton:baska_sohbet_islemi")
            return Yanit(uyari="Bu işlem başka bir sohbete/gruba ait.", uyari_goster=True, sessiz=True)
        if op.kullanici_id != o.kullanici_id:
            return Yanit(uyari="Bu onayı yalnız işlemi başlatan kullanıcı verebilir.", uyari_goster=True, sessiz=True)
        if op.grup_id is not None:
            if o.grup_mu and op.grup_id != o.sohbet_id:
                self._kaydet_yetkisiz(o, "buton:baska_grup_islemi")
                return Yanit(uyari="Bu işlem başka bir gruba ait.", uyari_goster=True, sessiz=True)
            if not self._onayli(op.grup_id):
                self.islemler.sil(kid)
                return Yanit("Grup artık onaylı değil; işlem iptal edildi.", uyari="Grup onaylı değil.", uyari_goster=True)
        if eylem == "h":
            self.islemler.sil(kid)
            return Yanit("İşlem iptal edildi.", [[Dugme("Ana menü", "m")]])
        if not self.islemler.sil(kid):  # tek kullanımlık: çift tıklama ikinci kez çalıştırmaz
            return Yanit(ONAY_BITTI, uyari="Bu onay zaten kullanılmış.", uyari_goster=True)
        return await self._onay_uygula(o, op)

    async def _onay_uygula(self, o: Olay, op) -> Yanit:
        geri = [[Dugme("Ana menü", "m")]]
        v = op.veri
        if op.tur == "oyuncu_sil":
            ok = self.servis.oyuncu_sil(op.grup_id, v["id"])
            return Yanit(
                f"Oyuncu takipten çıkarıldı: #{esc(v['id'])}" if ok else "Oyuncu zaten takipte değildi.",
                [[Dugme("Oyuncular", "ol:0"), Dugme("Ana menü", "m")]],
            )
        if op.tur == "parti_sil":
            await self._ack(o)
            s = await self.servis.parti_kaldirma_onayla(op.grup_id, v["parti_id"], self.kaynak, v["ozet"],
                                                       islem_yapan=o.kullanici_id, kurucu_id=self.kurucu_id)
            if s.durum == "silindi":
                satirlar = [f"Parti takibi kaldırıldı. Takipten çıkarılan oyuncu: {len(s.silinen)}."]
                satirlar += [f"• {esc(x.ad or x.oyuncu_id)} (#{esc(x.oyuncu_id)})" for x in s.silinen_oyuncular[:15]]
                if len(s.silinen_oyuncular) > 15:
                    satirlar.append(f"… ve {len(s.silinen_oyuncular) - 15} oyuncu daha")
                if s.engellenen:
                    satirlar.append(
                        "Başka takipli partide göründükleri için yeniden eklenmeyecek (engel konuldu; "
                        "elle eklerseniz kalkar):"
                    )
                    satirlar += [f"⛔ {esc(x.ad or x.oyuncu_id)} (#{esc(x.oyuncu_id)})" for x in s.engellenen[:15]]
                    if len(s.engellenen) > 15:
                        satirlar.append(f"… ve {len(s.engellenen) - 15} oyuncu daha")
                return Yanit("\n".join(satirlar), [[Dugme("Partiler", "pl:0"), Dugme("Ana menü", "m")]])
            if s.durum == "tekrar_onay" and s.onizleme is not None:
                return self._parti_onay_ekrani(o, op.grup_id, s.onizleme, tekrar=True)
            return Yanit(f"Parti kaldırılamadı: {esc(s.neden or s.durum)}. Hiçbir oyuncu silinmedi.", geri)
        if op.tur == "yetki_al":
            try:
                ok = yonetim.yetki_al(self.conn, self.saat, self.kurucu_id, o.kullanici_id, v["hedef"])
            except yonetim.YetkiHatasi as e:
                return Yanit(f"Yetki kaldırılamadı: {esc(str(e))}", geri)
            return Yanit(
                f"Yetki kaldırıldı: <code>{v['hedef']}</code>." if ok else "Kullanıcı zaten yetkili değildi.", geri
            )
        if op.tur == "grup_kaldir":
            try:
                n = yonetim.grup_kaldir(self.conn, self.saat, self.kurucu_id, o.kullanici_id, v["grup"])
            except yonetim.YetkiHatasi:
                return Yanit(YETKISIZ, geri)
            return Yanit(f"Grup onayı kaldırıldı: <code>{v['grup']}</code>. İptal edilen bekleyen bildirim: {n}.", geri)
        if op.tur == "grup_tasi":
            # Onay anında koşullar YENİDEN doğrulanır (servis katmanı transaction içinde); buton
            # yalnız kurucunun özel sohbetinde geçerlidir (sohbet+kullanıcı bağlı işlem + burada ek denetim).
            if o.grup_mu or o.kullanici_id != self.kurucu_id or o.sohbet_id != self.kurucu_id:
                self._kaydet_yetkisiz(o, "buton:grup_tasi")
                return Yanit("Grup verisi taşıma yalnızca kurucunun özel sohbetinde yapılabilir.", geri)
            try:
                sayilar = yonetim.grup_tasi(self.conn, self.saat, self.kurucu_id, o.kullanici_id, v["eski"], v["yeni"])
            except yonetim.YetkiHatasi:
                return Yanit(YETKISIZ, geri)
            except yonetim.TasimaHatasi as e:
                return Yanit(f"Taşıma yapılamadı: {esc(str(e))}. Hiçbir şey değiştirilmedi.", geri)
            except Exception:
                log.exception("grup taşıma başarısız (tüm değişiklikler geri alındı)")
                return Yanit("Taşıma sırasında hata oluştu; tüm değişiklikler geri alındı, hiçbir şey taşınmadı.", geri)
            return Yanit(
                f"Taşıma tamamlandı: <code>{v['eski']}</code> → <code>{v['yeni']}</code>\n"
                f"{_tasima_ozeti(sayilar)}\nEski kimlikte veri kalmadı.",
                geri,
            )
        return Yanit("Bilinmeyen işlem türü.", geri)

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
            return Yanit(uyari="Token yönetimi yalnız kurucunun özel sohbetinde yapılabilir.", uyari_goster=True, sessiz=True)
        assert o.kullanici_id is not None
        if a == "tc":
            self._bekleme_kaldir(o.kullanici_id)
            return Yanit("Token yenileme iptal edildi.", [[Dugme("Yönetim", "yo"), Dugme("Ana menü", "m")]])
        if a == "tn":
            self._bekleme_ayarla(o.kullanici_id)
            return Yanit(
                "Yeni Diplomacia token'ını <b>tek bir mesajla</b> gönderin (5 dakika içinde).\n"
                "Mesajı silmeye çalışacağım; token loga veya veritabanına yazılmaz. "
                "Aday token, mevcut dosyaya dokunmadan bir oyuncu isteğiyle sınanır; "
                "sınama başarısızsa mevcut token değiştirilmez.",
                [[Dugme("İptal", "tc")]],
            )
        # tk: menü (token'ın kendisi asla gösterilmez)
        var = bool(self.token_deposu and self.token_deposu.var_mi())
        mt = self.token_deposu.son_degisiklik() if self.token_deposu else None
        son = yerel_goster(datetime.fromtimestamp(mt, timezone.utc)) if mt else "bilinmiyor"
        dogrulayici = "yapılandırıldı" if getattr(self.dogrulayici, "yapilandirildi", True) else "yapılandırılmadı"
        return Yanit(
            "\n".join(
                [
                    "<b>Token yönetimi</b> (yalnız kurucu)",
                    f"Token dosyası: {'mevcut' if var else 'yok'}; son değişiklik: {esc(son)}",
                    f"Taramalar: {'token nedeniyle duraklatıldı' if self._token_duraklatildi() else 'duraklatılmadı'}",
                    f"Token doğrulayıcı: {dogrulayici}",
                ]
            ),
            [[Dugme("Yeni token gönder", "tn")], [Dugme("Yönetim", "yo"), Dugme("Ana menü", "m")]],
        )

    async def _token_isle(self, o: Olay) -> Yanit:
        if self._token_islem_suruyor:
            await self._mesaji_silmeyi_dene(o)
            return Yanit("Devam eden doğrulama var; bitmesini bekleyin. Bu mesaj işlenmedi.")
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
        sil_notu = "Mesajınız silindi." if silindi else "⚠️ Mesajınız silinemedi; lütfen elle silin."
        if not token or any(c.isspace() for c in token) or len(token) > 4096:
            return Yanit(f"{sil_notu}\nGeçersiz biçim (boşluk içermeyen tek bir token gönderin). Token DEĞİŞTİRİLMEDİ.")
        if self.token_deposu is None:
            return Yanit(f"{sil_notu}\nToken deposu yapılandırılmadı. Token DEĞİŞTİRİLMEDİ.")
        try:
            sonuc = await self.dogrulayici.dogrula(token)
        except Exception as e:  # hata metni token içerebilir: yalnız sınıf adı
            return Yanit(f"{sil_notu}\nDoğrulama sırasında hata ({type(e).__name__}). Token DEĞİŞTİRİLMEDİ.")
        if not sonuc.gecerli:
            nedenler = {
                "yapilandirilmadi": "Token doğrulayıcı yapılandırılmadı",
                "gecersiz": "Token reddedildi (401)",
                "hata": "Doğrulanamadı",
            }
            return Yanit(
                f"{sil_notu}\n{esc(nedenler.get(sonuc.durum, 'Doğrulanamadı'))}: {esc(sonuc.aciklama)}. "
                "Mevcut token DEĞİŞTİRİLMEDİ."
            )
        try:
            self.token_deposu.yaz(token)
        except Exception as e:
            return Yanit(f"{sil_notu}\nToken dosyası yazılamadı ({type(e).__name__}). Mevcut token korunuyor.")
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
            devam = " Taramalar kaldığı yerden devam ediyor."
        return Yanit(
            f"{sil_notu}\n✅ Aday token ile oyuncu isteği başarılı; kaydedildi "
            f"(önceki token varsa .prev olarak yedeklendi; yedek yeni token ilk başarılı taramayla teyit edilince "
            f"veya {config.TOKEN_PREV_OMRU_SANIYE // 3600} saat sonra otomatik silinir). "
            f"Canlı davranış ayrıca doğrulanmadı.{devam}",
            [[Dugme("Ana menü", "m")]],
        )

    # ====================== sohbet üyelik olayları ==========================
    def bot_uyelik_degisti(
        self, sohbet_id: int, sohbet_turu: str, eski_durum: str | None, yeni_durum: str | None, ekleyen_id: int | None
    ) -> str:
        """my_chat_member: botun onaysız gruba eklenmesini tespit eder ve kurucuya (hız sınırlı) bildirir.

        Dönüş: 'onaysiz_ekleme' | 'onayli_ekleme' | 'diger'.
        """
        if sohbet_turu not in ("group", "supergroup"):
            return "diger"
        eklendi = (eski_durum in (None, "left", "kicked")) and yeni_durum in ("member", "administrator")
        if not eklendi:
            return "diger"
        if sohbet_id in self._durum().onayli_gruplar:
            return "onayli_ekleme"
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
            log.exception("onaysız ekleme kaydı başarısız")
        return "onaysiz_ekleme"

    def grup_goc(self, eski_id: int, yeni_id: int) -> bool:
        """Grup süper gruba yükseldi (migrate). ONAYLI KAYIT OTOMATİK TAŞINMAZ: çekirdekte güvenli
        'grup_tasi' işlevi yok. Yeni kimlik onaysız kalır; kurucuya yeniden yetkilendirme bildirilir.
        Dönüş: bildirim kuyruğa alındı mı."""
        onayliydi = eski_id in self._durum().onayli_gruplar
        if not onayliydi and not yonetim.grup_verisi_var_mi(self.conn, eski_id):
            return False  # tanınmayan/verisiz grubun göçü: kayıt ve bildirim yok
        simdi = self.saat()
        # D9: olay YALNIZ kayıt üretir (bekleyen göç); yeni kimlik OTOMATİK onaylanmaz/yetkilenmez, veri taşınmaz.
        yonetim.goc_kaydet(self.conn, self.saat, eski_id, yeni_id)
        # Eski (ölü) sohbetin onayı düşer: tarama/gönderim durur, bekleyen outbox iptal; veri silinmez.
        if onayliydi:
            yonetim.grup_kaldir_sistem(self.conn, self.saat, self.kurucu_id, eski_id, neden="grup_goc")
        with db_islem(self.conn):
            return outbox.kuyruga_ekle(
                self.conn,
                hedef_sohbet=self.kurucu_id,
                grup_id=None,
                tur="grup_goc",
                idem_anahtari=f"grup_goc:{eski_id}:{yeni_id}",
                veri={"eski": eski_id, "yeni": yeni_id, "zaman": iso(simdi)},
                simdi=simdi,
            )
