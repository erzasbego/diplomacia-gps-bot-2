"""Telegram 'Account Management' menüsü (YALNIZ kurucunun özel sohbeti; Arayuz üzerinden yetki zaten denetlenmiştir).

GİZLİLİK
- JWT token ve proxy (adres/kullanıcı/parola) yalnız bu menüdeki "bekleme" akışında, kurucunun özel sohbetinde kabul edilir. Gönderilen mesaj
  HEMEN silinmeye çalışılır; silinemezse bu açıkça söylenir. Telegram Bot API sohbetleri uçtan uca şifreli DEĞİLDİR (mesaj Telegram
  sunucularından geçer): bu sınır kullanıcıya yazılır.
- Sırlar HİÇBİR yanıtta geri gösterilmez (hata metinleri dahil); yalnız VPS'te 0600 dosyalarda tutulur (bkz. hesaplar.HesapDeposu).
- Aday token/proxy ÖNCE sınanır; başarısızsa mevcut yapılandırma korunur. Proxy'li hesap proxy'ye ulaşamazsa doğrudan bağlantıya GEÇİLMEZ.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .. import config
from ..db import islem as db_islem
from ..hesaplar import DURUM_ETIKET, DURUM_HAZIR, SORUNLU_DURUMLAR, HesapHatasi
from ..maske import sir_kaydet
from ..zaman import SURE_GECERLI, SURE_SAAT_DEGISTI, damga_al, sure_durumu
from . import bicim
from .bicim import esc
from .modeller import Dugme, Olay, Yanit

SAYFA = 8
SIFRELEME_NOTU = (
    "Telegram bot chats are not end-to-end encrypted: the message passed through Telegram's servers"
)
SILINDI_NOTU = f"🗑 Your message was deleted. ({SIFRELEME_NOTU}.)"
SILINEMEDI_NOTU = (
    "⚠️ I could NOT delete your message (bots may not be allowed to delete it). Delete it yourself now. "
    f"{SIFRELEME_NOTU}, and until it is deleted anyone who can see this chat can read it; consider rotating the credential."
)


@dataclass
class GirdiBekleme:
    tur: str  # ad | proxy_yeni | token_yeni | token | proxy | ara
    hesap_id: int | None
    damga: object
    veri: dict = field(default_factory=dict, repr=False)  # SIR içerebilir (yalnız bellek, kısa ömürlü)


def _dg(etiket: str, veri: str) -> Dugme:
    return Dugme(etiket[:40], veri)


class HesapMenusu:
    """Arayuz'a karıştırılır. Kullandığı alanlar: hesaplar, conn, saat, _mono, islemler, kurucu_id, eski_token_yolu."""

    # ====================== durum yardımcıları ==============================
    def _hy(self):
        return self.hesaplar

    def _hesap_ad(self, h) -> str:
        return esc(h["ad"])

    def _durum_metni(self, h) -> str:
        e = DURUM_ETIKET.get(h["durum"], h["durum"])
        return e + (f" — {esc(h['durum_ayrinti'])}" if h["durum_ayrinti"] and h["durum"] != DURUM_HAZIR else "")

    def _sayi_metni(self, hesap_id: int, sayilar=None) -> str:
        n = (sayilar if sayilar is not None else self.hesaplar.oyuncu_sayilari()).get(hesap_id, 0)
        return f"{n}/{self.hesaplar.kapasite}"

    def _girdi_ayarla(self, o: Olay, tur: str, hesap_id: int | None, **veri) -> None:
        assert o.kullanici_id is not None
        self._girdi[o.kullanici_id] = GirdiBekleme(tur, hesap_id, damga_al(self.saat, self._mono), dict(veri))

    def _girdi_kaldir(self, kid: int | None) -> None:
        if kid is not None:
            g = self._girdi.pop(kid, None)
            if g is not None:
                g.veri.clear()

    def _geri(self, hesap_id: int | None = None) -> list[Dugme]:
        satir = []
        if hesap_id is not None:
            satir.append(_dg("⬅ Account", f"hd:{hesap_id}"))
        satir.append(_dg("🔐 Account Management", "hm"))
        return satir

    # ====================== ekranlar ========================================
    def _hesap_ana_ekran(self, o: Olay) -> Yanit:
        hy = self.hesaplar
        hy.dagit()
        hesaplar = hy.listele()
        oz = hy.kapasite_ozeti()
        satirlar = ["<b>Account Management</b> (owner only)"]
        if not hesaplar:
            satirlar.append("No Diplomacia account is configured yet. Tracking scans are paused until you add one.")
        else:
            sayilar = hy.oyuncu_sayilari()
            satirlar.append(f"Tracked players: {oz['toplam']} · assigned: {oz['toplam'] - oz['bekleyen']} · waiting for an account: {oz['bekleyen']}")
            for h in hesaplar:
                satirlar.append(f"• {self._hesap_ad(h)} — {self._sayi_metni(h['hesap_id'], sayilar)} players — {self._durum_metni(h)}")
        if oz["bekleyen"]:
            satirlar.append(f"⚠️ A new Diplomacia account is required: {oz['bekleyen']} players are waiting (nothing is lost; they are assigned automatically once you add an account).")
        satirlar.append("<i>Tokens and proxy credentials are never shown. Each account is responsible for at most "
                        f"{hy.kapasite} players (a fixed assignment capacity, not an API quota).</i>")
        klavye = [[_dg("👥 View Accounts", "hl"), _dg("➕ Add Account", "ha")]]
        if not hesaplar and self.eski_token_yolu is not None and self._eski_token_var():
            klavye.append([_dg("📥 Import existing token as Account 1", "hi")])
        klavye.append([Dugme("🏠 Main menu", "m")])
        return Yanit("\n".join(satirlar), klavye)

    def _eski_token_var(self) -> bool:
        try:
            from ..tg.token import TokenDeposu

            return TokenDeposu(self.eski_token_yolu).var_mi()
        except Exception:
            return False

    def _hesap_listesi(self, o: Olay) -> Yanit:
        hy = self.hesaplar
        hy.dagit()
        hesaplar = hy.listele()
        if not hesaplar:
            return Yanit("No accounts yet.", [[_dg("➕ Add Account", "ha")], self._geri()])
        sayilar = hy.oyuncu_sayilari()
        satirlar = ["<b>Accounts</b>"]
        klavye = []
        for h in hesaplar:
            satirlar.append(f"{self._hesap_ad(h)} — {self._sayi_metni(h['hesap_id'], sayilar)} players — {self._durum_metni(h)}")
            klavye.append([_dg(f"{h['ad']} — {self._sayi_metni(h['hesap_id'], sayilar)}", f"hd:{h['hesap_id']}")])
        if sayilar.get(None):
            satirlar.append(f"Waiting for an account: {sayilar[None]} players")
        klavye.append([_dg("➕ Add Account", "ha"), *self._geri()])
        return Yanit("\n".join(satirlar), klavye)

    def _hesap_detay(self, o: Olay, hesap_id: int) -> Yanit | None:
        hy = self.hesaplar
        h = hy.getir(hesap_id)
        if h is None:
            return None
        hy.dagit()
        st = hy.istatistik(hesap_id)
        t = hy.depo.token(hesap_id)
        satirlar = [
            f"<b>{self._hesap_ad(h)}</b> (#{hesap_id})",
            f"Status: {self._durum_metni(h)}",
            f"Players: {self._sayi_metni(hesap_id)} assigned",
            f"Token: {'stored' if t.var_mi() else 'none'}"
            + (f"; updated {bicim.tarih(h['token_guncelleme'])}" if h["token_guncelleme"] else ""),
            "Proxy: " + (f"configured ({esc(h['proxy_sema'])}) — {esc(self._proxy_durum_etiketi(h))}" if h["proxy_var"] else "not configured (direct connection)"),
            f"Last successful API request: {bicim.tarih(h['son_api_basari']) if h['son_api_basari'] else 'none yet'}",
            f"Last rate limit: {bicim.tarih(h['son_rate_limit']) if h['son_rate_limit'] else 'none'}; active cooldown: "
            + (f"{int(st['cooldown_kalan'] + 0.999)} s" if st["cooldown_kalan"] > 0 else "none"),
            "<i>Token and proxy credentials are never displayed.</i>",
        ]
        i = hesap_id
        klavye = [
            [_dg("🔑 Update JWT Token", f"ht:{i}"), _dg("🌐 Proxy Settings", f"hp:{i}")],
            [_dg("🔌 Test Connection", f"hc:{i}"), _dg("👤 Assigned Players", f"hy:{i}:0")],
            [_dg("📊 Account Statistics", f"hs:{i}")],
            [_dg("⬅ Accounts", "hl"), Dugme("🏠 Main menu", "m")],
        ]
        return Yanit("\n".join(satirlar), klavye)

    @staticmethod
    def _proxy_durum_etiketi(h) -> str:
        return {"calisiyor": "working", "hata": "UNREACHABLE (no direct connection is used)", "bilinmiyor": "unknown", "yok": "none"}.get(h["proxy_durum"], "unknown")

    def _hesap_istatistik(self, o: Olay, hesap_id: int) -> Yanit | None:
        hy = self.hesaplar
        h = hy.getir(hesap_id)
        if h is None:
            return None
        hy.dagit()
        st = hy.istatistik(hesap_id)
        h = hy.getir(hesap_id)
        satirlar = [
            f"<b>{self._hesap_ad(h)} — statistics</b>",
            f"Assigned players: {st['atanan']}/{st['kapasite']}",
            f"Successfully scanned (last 2 scan intervals): {st['basarili']}",
            f"Pending tasks: {st['bekleyen_gorev']}"
            + (" (this account is paused or failing; no other account takes them over)" if h["durum"] in SORUNLU_DURUMLAR and st["bekleyen_gorev"] else ""),
            f"Players whose last scan failed: {st['hatali']}",
            f"Last successful API request: {bicim.tarih(h['son_api_basari']) if h['son_api_basari'] else 'none yet'}",
            f"Last rate limit: {bicim.tarih(h['son_rate_limit']) if h['son_rate_limit'] else 'none'}",
            "Active cooldown: " + (f"{int(st['cooldown_kalan'] + 0.999)} s" if st["cooldown_kalan"] > 0 else "none"),
            "Proxy connection: " + (self._proxy_durum_etiketi(h) if h["proxy_var"] else "not configured (direct)"),
            f"Last scan duration: {h['son_tarama_sn']:.1f} s" + (f" (at {bicim.tarih(h['son_tarama_zamani'])})" if h["son_tarama_zamani"] else "")
            if h["son_tarama_sn"] is not None else "Last scan duration: none yet",
            f"Next scan: {bicim.tarih(h['sonraki_tarama']) if h['sonraki_tarama'] else 'waiting'} (interval {st['aralik_dk']} min)",
            f"Status: {self._durum_metni(h)}",
        ]
        bek = hy.kapasite_ozeti()["bekleyen"]
        if bek:
            satirlar.append(f"Players waiting for any account (no capacity): {bek}")
        return Yanit("\n".join(satirlar), [[_dg("🔄 Refresh", f"hs:{hesap_id}"), _dg("⬅ Account", f"hd:{hesap_id}")], self._geri()])

    def _oyuncu_takip_durumu(self, h, r) -> str:
        if h["durum"] in SORUNLU_DURUMLAR and h["durum"] not in ("rate_limit", "erisim_reddi") or self.hesaplar.cooldown_kalan(h["hesap_id"]) > 0:
            return "⏸ waiting (account paused)"
        if r["son_basari"] is None:
            return "⏳ waiting for first scan"
        sayac = self.conn.execute(
            "SELECT sayac FROM bulunamadi_sayaclari WHERE group_id=? AND tur='oyuncu' AND kimlik=?", (config.MERKEZ_ID, r["oyuncu_id"])
        ).fetchone()
        if sayac and sayac[0] >= config.NOT_FOUND_STREAK_THRESHOLD:
            return "❓ not found (404 streak)"
        return "✅ tracking"

    def _hesap_oyuncular(self, o: Olay, hesap_id: int, sayfa: int) -> Yanit | None:
        hy = self.hesaplar
        h = hy.getir(hesap_id)
        if h is None:
            return None
        hy.dagit()
        arama = self._hesap_arama.get((o.kullanici_id, hesap_id))
        toplam = hy.oyuncu_sayfasi(hesap_id, 0, 1, arama)[1]
        sayfa_sayisi = max(1, -(-toplam // SAYFA))
        sayfa = min(max(sayfa, 0), sayfa_sayisi - 1)
        satirlar_db, _ = hy.oyuncu_sayfasi(hesap_id, sayfa, SAYFA, arama)
        baslik = f"<b>{self._hesap_ad(h)} — assigned players</b> ({self._sayi_metni(hesap_id)}; page {sayfa + 1}/{sayfa_sayisi})"
        satirlar = [baslik]
        if arama:
            satirlar.append(f"Search: <code>{esc(arama)}</code> — {toplam} match(es)")
        if not satirlar_db:
            satirlar.append("No players." if not arama else "No matching players.")
        klavye = []
        for r in satirlar_db:
            ad = r["ad"] or "?"
            tarama = bicim.tarih(r["son_basari"]) if r["son_basari"] else "never"
            hata = f"⚠️ last scan failed: {esc(r['son_hata'])}" if r["son_hata"] else "no error"
            satirlar.append(f"• <a href=\"{esc(bicim.oyuncu_url(r['oyuncu_id']))}\">{esc(ad)}</a> (#{esc(r['oyuncu_id'])}) — last successful scan: {tarama} — {hata} — {self._oyuncu_takip_durumu(h, r)}")
            klavye.append([_dg(f"{r['oyuncu_id']} {ad}", f"od:{r['oyuncu_id']}")])
        gezinti = []
        if sayfa > 0:
            gezinti.append(_dg("‹ Previous", f"hy:{hesap_id}:{sayfa - 1}"))
        gezinti.append(_dg(f"{sayfa + 1}/{sayfa_sayisi}", "nop"))
        if sayfa < sayfa_sayisi - 1:
            gezinti.append(_dg("Next ›", f"hy:{hesap_id}:{sayfa + 1}"))
        klavye.append(gezinti)
        arama_satiri = [_dg("🔎 Search ID / name", f"hq:{hesap_id}")]
        if arama:
            arama_satiri.append(_dg("✖ Clear search", f"hx:{hesap_id}"))
        klavye.append(arama_satiri)
        klavye.append([_dg("⬅ Account", f"hd:{hesap_id}"), Dugme("🏠 Main menu", "m")])
        return Yanit("\n".join(satirlar), klavye)

    def _proxy_ekrani(self, o: Olay, hesap_id: int) -> Yanit | None:
        h = self.hesaplar.getir(hesap_id)
        if h is None:
            return None
        satirlar = [
            f"<b>{self._hesap_ad(h)} — proxy settings</b>",
            "Proxy: " + (f"configured ({esc(h['proxy_sema'])}) — {esc(self._proxy_durum_etiketi(h))}" if h["proxy_var"] else "not configured (direct connection)"),
            "A new proxy is tested with this account's stored token <b>before</b> it replaces the current one. If a configured proxy "
            "becomes unreachable the account waits — it never falls back to a direct connection on its own.",
            "<i>The proxy address, username and password are never displayed.</i>",
        ]
        klavye = [[_dg("✏️ Set / update proxy", f"hps:{hesap_id}")]]
        if h["proxy_var"]:
            klavye.append([_dg("🗑 Remove proxy", f"hpr:{hesap_id}")])
        klavye.append([_dg("🔌 Test Connection", f"hc:{hesap_id}"), _dg("⬅ Account", f"hd:{hesap_id}")])
        return Yanit("\n".join(satirlar), klavye)

    # ====================== sonuç metinleri =================================
    @staticmethod
    def _sonuc_metni(sonuc) -> str:
        return f"{esc(sonuc.aciklama)}"

    def _bekleme_istemi(self, tur: str, hesap_id: int | None = None) -> Yanit:
        iptal = [[_dg("Cancel", "hz")]]
        sn = config.HESAP_GIRDI_BEKLEME_SN // 60
        if tur == "ad":
            return Yanit(f"<b>Add Account — step 1/3</b>\nSend a name for the new account (e.g. <code>Account 2</code>). Valid within {sn} minutes.", iptal)
        if tur == "proxy_yeni":
            return Yanit(
                "<b>Add Account — step 2/3</b>\nSend the proxy as <code>http://user:password@host:port</code> in <b>one message</b>, or press "
                "“No proxy” for a direct connection. The message is deleted right after I read it; the proxy is verified together with "
                "the token in the next step.",
                [[_dg("No proxy (direct)", "hn")], [_dg("Cancel", "hz")]],
            )
        if tur == "token_yeni":
            return Yanit(
                "<b>Add Account — step 3/3</b>\nSend the Diplomacia JWT token in <b>one message</b>. It is tested (through the proxy, if any) "
                "<b>before</b> anything is saved; I try to delete your message and never echo the token.", iptal)
        if tur == "token":
            return Yanit(
                f"Send the new Diplomacia JWT token for account #{hesap_id} in <b>one message</b> (within {sn} minutes). It is tested through "
                "this account's proxy (if any) without touching the current token; if the test fails the current token is kept. I try to "
                "delete your message and never echo the token.", iptal)
        if tur == "proxy":
            return Yanit(
                f"Send the new proxy for account #{hesap_id} as <code>http://user:password@host:port</code> in <b>one message</b>. It is tested "
                "with this account's token first; if the test fails the current proxy is kept. I try to delete your message and never echo it.", iptal)
        if tur == "ara":
            return Yanit("Send a player ID (or the start of it) or part of a name to search among this account's players.", iptal)
        return Yanit("Unknown step.", iptal)

    async def _mesaji_sil_notu(self, o: Olay) -> str:
        silindi = False
        if o.sil is not None:
            try:
                silindi = bool(await o.sil())
            except Exception:
                silindi = False
        return SILINDI_NOTU if silindi else SILINEMEDI_NOTU

    # ====================== düğme yönlendirme ===============================
    async def _hesap_buton(self, o: Olay, a: str, p: list[str]) -> Yanit | None:
        """Account Management düğmeleri. Çağıran, kurucunun özel sohbeti olduğunu zaten denetlemiştir."""
        assert o.kullanici_id is not None
        arg = p[1] if len(p) > 1 else ""
        hid = int(arg) if arg.lstrip("-").isdigit() else None
        if a == "hm":
            self._girdi_kaldir(o.kullanici_id)
            return self._hesap_ana_ekran(o)
        if a == "hl":
            return self._hesap_listesi(o)
        if a == "hz":
            self._girdi_kaldir(o.kullanici_id)
            return Yanit("Cancelled. Nothing was changed.", [self._geri()])
        if a == "ha":
            if self.hesaplar._sayi() >= 12:
                return Yanit(uyari="The maximum number of accounts is reached.", uyari_goster=True, sessiz=True)
            self._girdi_ayarla(o, "ad", None)
            return self._bekleme_istemi("ad")
        if a == "hn":  # 'No proxy'
            g = self._girdi.get(o.kullanici_id)
            if g is None or g.tur != "proxy_yeni":
                return None
            g.veri["proxy"] = None
            g.tur = "token_yeni"
            g.damga = damga_al(self.saat, self._mono)
            return self._bekleme_istemi("token_yeni")
        if a == "hi":
            await self._ack(o)
            if self.eski_token_yolu is None:
                return None
            try:
                hesap_id, sonuc = await self.hesaplar.eski_token_ice_aktar(self.eski_token_yolu)
            except HesapHatasi as e:
                return Yanit(f"Could not import: {esc(str(e))}", [self._geri()])
            if hesap_id is None:
                return Yanit(f"The previous token could not be verified: {self._sonuc_metni(sonuc)}. Nothing was imported.", [self._geri()])
            return Yanit("✅ The previous token was verified and imported as <b>Account 1</b> (a copy; the old file is left in place).",
                         [[_dg("Open account", f"hd:{hesap_id}")], self._geri()])
        if hid is None:
            return None
        h = self.hesaplar.getir(hid)
        if h is None:
            return Yanit(uyari="That account no longer exists.", uyari_goster=True, sessiz=True)
        if a == "hd":
            return self._hesap_detay(o, hid)
        if a == "hs":
            return self._hesap_istatistik(o, hid)
        if a == "hy":
            sayfa = int(p[2]) if len(p) > 2 and p[2].isdigit() else 0
            return self._hesap_oyuncular(o, hid, sayfa)
        if a == "hq":
            self._girdi_ayarla(o, "ara", hid)
            return self._bekleme_istemi("ara", hid)
        if a == "hx":
            self._hesap_arama.pop((o.kullanici_id, hid), None)
            return self._hesap_oyuncular(o, hid, 0)
        if a == "hp":
            return self._proxy_ekrani(o, hid)
        if a == "ht":
            self._girdi_ayarla(o, "token", hid)
            return self._bekleme_istemi("token", hid)
        if a == "hps":
            self._girdi_ayarla(o, "proxy", hid)
            return self._bekleme_istemi("proxy", hid)
        if a == "hpr":
            return self._onay_yaniti(
                o, "proxy_sil", None, {"hesap": hid},
                f"<b>Remove the proxy of {self._hesap_ad(h)}?</b>\nAfter this the account connects <b>directly</b> to Diplomacia from the server's own IP address. "
                "This only happens because you confirm it; it is never done automatically.",
            )
        if a == "hc":
            await self._ack(o)
            try:
                sonuc = await self.hesaplar.baglanti_testi(hid)
            except HesapHatasi as e:
                return Yanit(esc(str(e)), [self._geri(hid)])
            simge = "✅" if sonuc.gecerli else "⚠️"
            return Yanit(f"{simge} <b>Connection test — {self._hesap_ad(h)}</b>\n{self._sonuc_metni(sonuc)}", [[_dg("⬅ Account", f"hd:{hid}"), _dg("🔌 Test again", f"hc:{hid}")]])
        return None

    # ====================== metin girdisi ===================================
    async def _hesap_girdi(self, o: Olay, g: GirdiBekleme) -> Yanit:
        """Bekleme açıkken gelen düz metin (özel sohbet, kurucu). Hassas girdiler (token/proxy) HEMEN silinmeye çalışılır."""
        assert o.kullanici_id is not None
        sure = sure_durumu(g.damga, timedelta(seconds=config.HESAP_GIRDI_BEKLEME_SN), self.saat, self._mono)
        metin = (o.metin or "").strip()
        hassas = g.tur in ("proxy_yeni", "token_yeni", "token", "proxy")
        if hassas:
            sir_kaydet(metin)
        if sure != SURE_GECERLI:
            self._girdi_kaldir(o.kullanici_id)
            not_ = await self._mesaji_sil_notu(o) if hassas else ""
            if sure == SURE_SAAT_DEGISTI:
                return Yanit(f"{not_}\nThe server clock changed; for safety the pending step was cancelled and your message was not processed. Nothing was changed.".strip(),
                             [self._geri()])
            return Yanit(f"{not_}\nThe waiting time expired; your message was not processed. Nothing was changed. Start again from Account Management.".strip(),
                         [self._geri()])
        # ---- hassas olmayan adımlar
        if g.tur == "ad":
            from ..hesaplar import ad_dogrula

            try:
                ad = ad_dogrula(metin)
            except HesapHatasi as e:
                return Yanit(esc(str(e)) + "\nSend another name or cancel.", [[_dg("Cancel", "hz")]])
            if self.conn.execute("SELECT 1 FROM hesaplar WHERE lower(ad)=lower(?)", (ad,)).fetchone():
                return Yanit("An account with that name already exists. Send another name or cancel.", [[_dg("Cancel", "hz")]])
            g.veri["ad"] = ad
            g.tur = "proxy_yeni"
            g.damga = damga_al(self.saat, self._mono)
            return self._bekleme_istemi("proxy_yeni")
        if g.tur == "ara":
            self._girdi_kaldir(o.kullanici_id)
            if not metin or len(metin) > 40:
                return Yanit("Send up to 40 characters.", [[_dg("⬅ Account", f"hd:{g.hesap_id}")]])
            self._hesap_arama[(o.kullanici_id, g.hesap_id)] = metin
            return self._hesap_oyuncular(o, g.hesap_id, 0) or Yanit("Unknown account.")
        # ---- hassas adımlar: ÖNCE sil
        sil = await self._mesaji_sil_notu(o)
        if g.tur == "proxy_yeni":
            from ..hesaplar import proxy_ayristir

            try:
                proxy_ayristir(metin)  # yalnız biçim denetimi; sınama token ile birlikte yapılır
            except HesapHatasi as e:
                return Yanit(f"{sil}\n{esc(str(e))}\nSend another proxy, press “No proxy”, or cancel.", [[_dg("No proxy (direct)", "hn")], [_dg("Cancel", "hz")]])
            g.veri["proxy"] = metin
            g.tur = "token_yeni"
            g.damga = damga_al(self.saat, self._mono)
            return Yanit(f"{sil}\n" + self._bekleme_istemi("token_yeni").metin, [[_dg("Cancel", "hz")]])
        if self._hesap_islem_suruyor:
            return Yanit(f"{sil}\nAnother verification is still running; wait for it to finish and retry from the menu. Nothing was changed.", [self._geri()])
        self._hesap_islem_suruyor = True
        try:
            self._girdi.pop(o.kullanici_id, None)  # tek kullanımlık; g.veri (ad/proxy) _hassas_isle'de alınıp temizlenir
            return await self._hassas_isle(o, g, metin, sil)
        finally:
            self._hesap_islem_suruyor = False

    async def _hassas_isle(self, o: Olay, g: GirdiBekleme, metin: str, sil: str) -> Yanit:
        proxy_metni = g.veri.get("proxy")
        ad = g.veri.get("ad")
        tur, hid = g.tur, g.hesap_id
        g.veri.clear()
        hy = self.hesaplar
        try:
            if tur == "token_yeni":
                hesap_id, sonuc = await hy.hesap_olustur(ad, metin, proxy_metni)
                if hesap_id is None:
                    return Yanit(f"{sil}\n⚠️ {self._sonuc_metni(sonuc)}. <b>No account was created.</b>", [self._geri()])
                self.hesaplar.dagit()
                return Yanit(
                    f"{sil}\n✅ Account <b>{esc(ad)}</b> was created after the token was verified"
                    + (" through its proxy" if proxy_metni else "") + ". Waiting players (if any) are assigned automatically.",
                    [[_dg("Open account", f"hd:{hesap_id}")], self._geri()])
            if tur == "token":
                sonuc = await hy.token_guncelle(hid, metin)
                if not sonuc.gecerli:
                    return Yanit(f"{sil}\n⚠️ {self._sonuc_metni(sonuc)}. The current token was <b>NOT changed</b>.", [self._geri(hid)])
                return Yanit(f"{sil}\n✅ The token was verified and saved (a previous token is kept as .prev for at most "
                             f"{config.TOKEN_PREV_OMRU_SANIYE // 3600} hours). This account's scans resume.", [self._geri(hid)])
            if tur == "proxy":
                sonuc = await hy.proxy_guncelle(hid, metin)
                if not sonuc.gecerli:
                    return Yanit(f"{sil}\n⚠️ {self._sonuc_metni(sonuc)}. The current proxy setting was <b>NOT changed</b>.", [self._geri(hid)])
                return Yanit(f"{sil}\n✅ The proxy was verified with this account's token and saved. This account's scans resume.", [self._geri(hid)])
        except HesapHatasi as e:
            return Yanit(f"{sil}\n{esc(str(e))} Nothing was changed.", [self._geri(hid)])
        except Exception as e:  # istisna metni sır içerebilir: yalnız sınıf adı
            return Yanit(f"{sil}\nUnexpected error ({type(e).__name__}); nothing was changed.", [self._geri(hid)])
        return Yanit(f"{sil}\nUnknown step.", [self._geri()])
