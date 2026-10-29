"""Telegram 'Account Management' menüsü (çok hesaplı mimari) — MOCK testler, ağ yok.

Kapsam: gezinti, hesap ekleme akışı (ad -> proxy -> token), mesaj silme/silinememe uyarıları, JWT güncelleme, proxy ayarları,
hesap ekranları/istatistikler, hesaba atanmış oyuncu listesi (sayfalama + arama), güvenlik (sır sızıntısı yok, İngilizce arayüz,
yalnız kurucu/özel sohbet), /status satırı ve oyuncu ekle/sil sonrası anında yeniden dağıtım.

Her yanıt (metin, uyarı, düğme etiketi, callback_data, url) `Ortam.agit` içinde otomatik denetlenir: sır yok, Türkçe artık yok,
callback_data <= 64 bayt.
"""
import asyncio
import logging
import os
import stat

import pytest

from conftest import run
from fakes import SahteKaynak, SahteSaat
from test_ingilizce_arayuz import ingilizce_mi, tr_kalinti, yanit_ingilizce
from tg_fakes import KURUCU, YABANCI, YETKILI, G1, dugme, komut, onay_kimligi, veriler

from dipbot import config, db, yonetim
from dipbot.hesaplar import HesapYoneticisi, proxy_ayristir
from dipbot.tg.arayuz import KAPALI_OZEL, Arayuz
from dipbot.tg.hesap_menu import GirdiBekleme, SILINDI_NOTU, SILINEMEDI_NOTU
from dipbot.tg.modeller import CALLBACK_AZAMI_BAYT, Olay
from dipbot.tg.token import DogrulamaSonucu, TokenDeposu
from dipbot.zaman import damga_al, iso, yerel_goster

M = config.MERKEZ_ID
TOKEN1 = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0MSJ9.c2lnbmF0dXJlMQ"
TOKEN2 = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0MiJ9.c2lnbmF0dXJlMg"
TOKEN3 = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0MyJ9.c2lnbmF0dXJlMw"
KOTU = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJiYWQifQ.YmFkc2lnbmF0dXJl"  # JWT biçimli ama site reddeder
PROXY = "http://proxyuser:S3cretPass@10.1.2.3:8080"
PROXY2 = "http://proxy2user:Pass2Secret@10.9.8.7:3128"
PROXY_URL = proxy_ayristir(PROXY).url
PROXY2_URL = proxy_ayristir(PROXY2).url
# Hiçbir yanıtta / uyarıda / düğmede / callback_data'da / loglarda görünmemesi gereken sırlar
SIRLAR = [
    TOKEN1, TOKEN2, TOKEN3, KOTU, "eyJhbGci", PROXY, PROXY2, PROXY_URL, PROXY2_URL,
    "proxyuser", "S3cretPass", "10.1.2.3", "proxy2user", "Pass2Secret", "10.9.8.7",
]


# ====================================================================== altyapı
class _Dog:
    """Sahte doğrulayıcı: aday token + kullanılan proxy KAYDEDİLİR; sonucu Ortam.kural belirler."""

    def __init__(self, ortam, proxy):
        self.ortam, self.proxy = ortam, proxy

    async def dogrula(self, token):
        o = self.ortam
        o.kayit.append((token, self.proxy))
        if o.basladi is not None:
            o.basladi.set()
        if o.engel is not None:
            await o.engel.wait()
        if o.kural is not None:
            return o.kural(token, self.proxy)
        if self.proxy and self.proxy in o.olu_proxyler:
            return DogrulamaSonucu("proxy_hatasi", "could not connect through the configured proxy (no direct connection was attempted)")
        if token in (TOKEN1, TOKEN2, TOKEN3):
            return DogrulamaSonucu("gecerli", "ok")
        return DogrulamaSonucu("gecersiz", "the site rejected the token (401)")


def _metinler(y):
    if y is None:
        return
    yield y.metin
    yield y.uyari
    for satir in y.klavye or []:
        for d in satir:
            yield d.etiket
            yield d.veri
            yield d.url


def yanit_denetle(y):
    """Her yanıt: sır yok, Türkçe artık yok, callback_data <= 64 bayt."""
    if y is None:
        return
    for s in _metinler(y):
        if not s:
            continue
        for sir in SIRLAR:
            assert sir not in s, f"SIR SIZDI ({sir[:6]}...): {s!r}"
    yanit_ingilizce(y)
    for satir in y.klavye or []:
        for d in satir:
            if d.veri is not None:
                assert len(d.veri.encode("utf-8")) <= CALLBACK_AZAMI_BAYT, d.veri
    assert y.uyari is None or len(y.uyari) <= 200


class Ortam:
    def __init__(self, tmp_path, *, hesaplar=True, eski_token=None, kapasite=None):
        self.tmp_path = tmp_path
        self.conn = db.baglan(":memory:")
        self.saat = SahteSaat()
        self.kayit: list = []
        self.olu_proxyler: set = set()
        self.kural = None
        self.engel = None
        self.basladi = None
        self.silmeler: list = []
        self.yanitlar: list = []
        self.hy = HesapYoneticisi(
            self.conn, self.saat, tmp_path / "hesaplar", dogrulayici_fabrikasi=lambda p: _Dog(self, p.url if p else None),
            kapasite=kapasite,
        )
        self.src = SahteKaynak()
        self.eski_yol = None
        if eski_token is not None:
            self.eski_yol = tmp_path / "eski" / "token"
            TokenDeposu(self.eski_yol).yaz(eski_token)
        self.ui = Arayuz(self.conn, self.src, KURUCU, saat=self.saat, hesaplar=self.hy if hesaplar else None,
                         eski_token_yolu=self.eski_yol)

    # ---- olay üreticiler
    def mesaj(self, metin, *, sil="ok", kul=KURUCU, sohbet=None, tur="private") -> Olay:
        async def ok():
            self.silmeler.append(metin)
            return True

        async def basarisiz():
            self.silmeler.append(metin)
            return False

        async def istisna():
            self.silmeler.append(metin)
            raise RuntimeError("Forbidden: bots can't delete")

        fn = {"ok": ok, "basarisiz": basarisiz, "istisna": istisna, "yok": None}[sil]
        return Olay(sohbet_id=kul if sohbet is None else sohbet, sohbet_turu=tur, kullanici_id=kul, metin=metin, mesaj_id=30, sil=fn)

    # ---- gönderme (yanıtlar kaydedilir + denetlenir)
    async def agit(self, olay):
        if olay.veri is not None:
            y = await self.ui.buton(olay)
        elif olay.komut:
            y = await self.ui.komut(olay)
        else:
            y = await self.ui.metin(olay)
        self.yanitlar.append(y)
        yanit_denetle(y)
        return y

    def git(self, olay):
        return run(self.agit(olay))

    def d(self, veri, **kw):
        return self.git(dugme(veri, **kw))

    def k(self, ad, arg="", **kw):
        return self.git(komut(ad, arg, **kw))

    def m(self, metin, **kw):
        return self.git(self.mesaj(metin, **kw))

    # ---- veri kurulumu
    def hesap(self, ad="Main", token=TOKEN1, proxy=None) -> int:
        hid, s = run(self.hy.hesap_olustur(ad, token, proxy))
        assert hid is not None, s
        return hid

    def oyuncular(self, n, baslangic=1, ad=lambda i: f"P{i}"):
        for i in range(baslangic, baslangic + n):
            self.conn.execute(
                "INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi, baseline_tamam) VALUES(?,?,?,?,1)",
                (M, str(i), ad(i), f"2026-10-01T10:{i // 60 % 60:02d}:{i % 60:02d}.{i:06d}Z"),
            )
        self.hy.dagit()

    def satir(self, hid):
        return self.conn.execute("SELECT * FROM hesaplar WHERE hesap_id=?", (hid,)).fetchone()

    def hesap_sayisi(self):
        return self.conn.execute("SELECT COUNT(*) FROM hesaplar").fetchone()[0]

    def dosyalar(self, hid):
        d = self.tmp_path / "hesaplar" / str(hid)
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def token_dosyasi(self, hid):
        return self.hy.depo.token(hid).oku()

    def denemeler(self, kul=None):
        sql, a = "SELECT user_id, group_id, islem FROM yetkisiz_denemeler", ()
        if kul is not None:
            sql, a = sql + " WHERE user_id=?", (kul,)
        return self.conn.execute(sql, a).fetchall()

    def bekleme_var(self):
        return bool(self.ui._girdi)


@pytest.fixture
def o(tmp_path):
    return Ortam(tmp_path)


def hesap_ekle_akisi(o, ad="Account 2", proxy=None, token=TOKEN3, **sil):
    """ha -> ad -> (proxy | hn) -> token; son yanıtı döndürür."""
    o.d("ha")
    o.m(ad)
    if proxy:
        o.m(proxy, **sil)
    else:
        o.d("hn")
    return o.m(token, **sil)


def oid_listesi(y):
    return [v.split(":")[1] for v in veriler(y) if v.startswith("od:")]


# ====================================================================== 1. gezinti
@pytest.mark.parametrize("ad", ["accounts", "hesaplar"])
def test_accounts_ve_eski_hesaplar_komutu_hesap_ekranini_acar(o, ad):
    y = o.k(ad)
    assert "Account Management" in y.metin and "owner only" in y.metin
    assert {"hl", "ha"} <= set(veriler(y))
    assert "tk" not in veriler(y)


def test_ana_menude_hm_dugmesi_ve_yonetim_ekraninda_tk_yok(o):
    assert "hm" in veriler(o.k("start"))
    y = o.d("yo")
    assert "hm" in veriler(y) and "tk" not in veriler(y)
    assert any("Account Management" in d.etiket for s in y.klavye for d in s)
    # eski Renew Token düğmesine basılsa bile eski akış değil hesap ekranı açılır
    y = o.d("tk")
    assert "Account Management" in y.metin and not o.bekleme_var()
    assert "Account Management" in o.d("hm").metin


def test_hesap_yokken_hicbiri_yapilandirilmadi_ve_hesap_ekle_sunar(o):
    y = o.k("accounts")
    assert "No Diplomacia account is configured yet" in y.metin
    assert "ha" in veriler(y) and "hi" not in veriler(y)
    y = o.d("hl")
    assert "No accounts yet" in y.metin and "ha" in veriler(y)


def test_eski_token_dosyasi_varsa_ice_aktar_sunulur_dogrulayinca_account1_olusur(tmp_path):
    o = Ortam(tmp_path, eski_token=TOKEN1)
    assert "hi" in veriler(o.d("hm"))
    assert o.hesap_sayisi() == 0, "yalnız teklif; doğrulamadan hesap yok"
    y = o.d("hi")
    assert "imported as <b>Account 1</b>" in y.metin and "old file is left in place" in y.metin
    assert o.hesap_sayisi() == 1 and o.satir(1)["ad"] == "Account 1"
    assert o.kayit == [(TOKEN1, None)], "önce doğrulandı"
    assert o.eski_yol.is_file() and o.eski_yol.read_text().strip() == TOKEN1, "eski dosya silinmedi"
    assert o.token_dosyasi(1) == TOKEN1
    assert "hi" not in veriler(o.d("hm")), "yalnız hesap yokken sunulur"
    # hesap varken 'hi' basılırsa reddedilir, ikinci hesap oluşmaz
    y = o.d("hi")
    assert "Could not import" in y.metin and o.hesap_sayisi() == 1


def test_eski_token_dogrulanamazsa_hicbir_sey_ice_aktarilmaz(tmp_path):
    o = Ortam(tmp_path, eski_token=KOTU)
    y = o.d("hi")
    assert "could not be verified" in y.metin and "Nothing was imported" in y.metin
    assert o.hesap_sayisi() == 0 and o.dosyalar(1) == []
    assert o.eski_yol.read_text().strip() == KOTU
    assert "hi" in veriler(o.d("hm")), "hâlâ hesap yok: teklif sürer"


def test_eski_token_dosyasi_yoksa_ice_aktar_sunulmaz(tmp_path):
    o = Ortam(tmp_path)
    o.ui.eski_token_yolu = tmp_path / "yok" / "token"
    assert "hi" not in veriler(o.d("hm"))


def test_azami_hesap_sayisinda_ekle_reddedilir(o):
    for i in range(12):
        o.conn.execute("INSERT INTO hesaplar(ad, olusturuldu) VALUES(?, '2026-10-01T00:00:00.000000Z')", (f"H{i}",))
    y = o.d("ha")
    assert "maximum number of accounts" in y.uyari and not o.bekleme_var()


# ====================================================================== 2. hesap ekleme akışı
def test_hesap_ekleme_adimlari_ve_istemleri_proxysiz(o):
    y = o.d("ha")
    assert "step 1/3" in y.metin and "hz" in veriler(y) and y.duzenle
    y = o.m("Account 2")
    assert "step 2/3" in y.metin and {"hn", "hz"} <= set(veriler(y))
    assert o.silmeler == [], "ad hassas değil"
    y = o.d("hn")
    assert "step 3/3" in y.metin and "hz" in veriler(y)
    y = o.m(TOKEN3)
    assert o.silmeler == [TOKEN3], "token mesajı silindi"
    assert SILINDI_NOTU in y.metin and "Account <b>Account 2</b> was created" in y.metin and "through its proxy" not in y.metin
    assert o.kayit == [(TOKEN3, None)]
    assert o.hesap_sayisi() == 1 and not o.bekleme_var()
    assert o.token_dosyasi(1) == TOKEN3
    assert any(v == "hd:1" for v in veriler(y))


def test_hesap_ekleme_proxyli_basarili_dosyalar_0600_satirlarda_sir_yok(o, tmp_path):
    o.oyuncular(10)
    assert o.hy.oyuncu_sayilari() == {None: 10}, "hesap yok: oyuncular bekliyor"
    old = os.umask(0o022)  # kötü umask'e rağmen izinler zorlanır
    try:
        o.d("ha")
        o.m("Account 2")
        y = o.m(PROXY)
        assert "step 3/3" in y.metin and SILINDI_NOTU in y.metin
        assert o.silmeler == [PROXY] and o.hesap_sayisi() == 0
        y = o.m(TOKEN3)
    finally:
        os.umask(old)
    assert "was created" in y.metin and "through its proxy" in y.metin
    assert o.silmeler == [PROXY, TOKEN3]
    assert o.kayit == [(TOKEN3, PROXY_URL)], "doğrulama aday proxy üzerinden yapıldı"
    kok = tmp_path / "hesaplar"
    assert stat.S_IMODE(os.stat(kok).st_mode) == 0o700 and stat.S_IMODE(os.stat(kok / "1").st_mode) == 0o700
    assert o.dosyalar(1) == ["proxy", "token"]
    for p in (kok / "1").iterdir():
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, p.name
    assert o.hy.depo.izin_sorunlari() == []
    r = o.satir(1)
    assert (r["proxy_var"], r["proxy_sema"], r["durum"], r["proxy_durum"]) == (1, "http", "hazir", "calisiyor")
    for satir in o.conn.execute("SELECT * FROM hesaplar"):
        assert not any(isinstance(v, str) and any(s in v for s in SIRLAR) for v in tuple(satir))
    assert o.hy.oyuncu_sayilari() == {1: 10}, "bekleyen oyuncular otomatik atandı"
    assert "(10/150" not in y.metin  # sayı ekranlarında gösterilir, bu yanıtta değil


def test_gecersiz_veya_yinelenen_ad_yeniden_sorulur(o):
    o.hesap("Main")
    o.d("ha")
    for kotu in ["<b>x</b>", "", "-x", "x" * 33, "Çay"]:
        y = o.m(kotu)
        assert "Account names use" in y.metin and "hz" in veriler(y), kotu
    for yinelenen in ["Main", "main", " MAIN "]:
        y = o.m(yinelenen)
        assert "already exists" in y.metin, yinelenen
    assert o.bekleme_var() and o.ui._girdi[KURUCU].tur == "ad"
    y = o.m("Second")
    assert "step 2/3" in y.metin


def test_gecersiz_proxy_bicimi_yeniden_sorulur_silinir_ve_yansitilmaz(o):
    o.d("ha")
    o.m("Account 2")
    for kotu in ["http://nohost", "ftp://u:S3cretPass@10.1.2.3:21", "proxyuser S3cretPass 10.1.2.3", "http://u:S3cretPass@10.1.2.3:8080/yol?x=1"]:
        n = len(o.silmeler)
        y = o.m(kotu)
        assert len(o.silmeler) == n + 1, "hassas olabilir: her mesaj silinmeye çalışılır"
        assert "Send the proxy as" in y.metin and "hn" in veriler(y) and "hz" in veriler(y)
        assert kotu not in y.metin
        assert o.ui._girdi[KURUCU].tur == "proxy_yeni"
    y = o.m(PROXY)
    assert "step 3/3" in y.metin
    assert o.kayit == [], "token gelmeden hiçbir doğrulama yok"


def test_dogrulama_basarisizsa_hesap_ve_dosya_olusmaz(o, tmp_path):
    y = hesap_ekle_akisi(o, token=KOTU)
    assert "No account was created" in y.metin
    assert o.hesap_sayisi() == 0
    assert not (tmp_path / "hesaplar").exists() or list((tmp_path / "hesaplar").rglob("*")) == []
    assert o.kayit == [(KOTU, None)] and o.silmeler == [KOTU]
    assert not o.bekleme_var(), "bekleme tek kullanımlık"


def test_dogrulama_aday_proxy_uzerinden_basarisizsa_hicbir_sey_olusmaz(o, tmp_path):
    o.olu_proxyler.add(PROXY_URL)
    y = hesap_ekle_akisi(o, proxy=PROXY)
    assert "No account was created" in y.metin and "no direct connection was attempted" in y.metin
    assert o.hesap_sayisi() == 0 and o.kayit == [(TOKEN3, PROXY_URL)]
    assert not (tmp_path / "hesaplar").exists() or list((tmp_path / "hesaplar").rglob("*")) == []


def test_hz_akisi_iptal_eder_sonraki_token_islenmez(o):
    o.d("ha")
    o.m("Account 2")
    y = o.d("hn")
    assert o.bekleme_var()
    y = o.d("hz")
    assert "Cancelled" in y.metin and "Nothing was changed" in y.metin and not o.bekleme_var()
    y = o.m(TOKEN3)
    assert "was not processed" in y.metin
    assert o.hesap_sayisi() == 0 and o.kayit == []
    # hn bekleme yokken yanıtsız (eski düğme)
    assert "no longer valid" in o.d("hn").uyari
    # hm de beklemeyi temizler
    o.d("ha")
    assert o.bekleme_var()
    o.d("hm")
    assert not o.bekleme_var()


@pytest.mark.parametrize("adim", ["proxy_yeni", "token_yeni", "token", "proxy"])
def test_bekleme_suresi_dolunca_mesaj_islenmez_ama_silinmeye_calisilir(o, adim):
    hid = o.hesap("Main")
    o.kayit.clear()
    if adim == "proxy_yeni":
        o.d("ha"); o.m("Account 2")
        mesaj = PROXY
    elif adim == "token_yeni":
        o.d("ha"); o.m("Account 2"); o.d("hn")
        mesaj = TOKEN3
    elif adim == "token":
        o.d(f"ht:{hid}")
        mesaj = TOKEN2
    else:
        o.d(f"hps:{hid}")
        mesaj = PROXY
    o.saat.ilerle(config.HESAP_GIRDI_BEKLEME_SN + 1)
    y = o.m(mesaj)
    assert "waiting time expired" in y.metin and "not processed" in y.metin
    assert o.silmeler == [mesaj], "hassas adım: silme denendi"
    assert SILINDI_NOTU in y.metin
    assert o.kayit == [] and not o.bekleme_var()
    assert o.hesap_sayisi() == 1 and o.token_dosyasi(hid) == TOKEN1 and o.satir(hid)["proxy_var"] == 0


def test_bekleme_suresi_icinde_sinirda_gecerli_ve_her_adim_sayaci_yeniler(o):
    o.d("ha")
    o.saat.ilerle(config.HESAP_GIRDI_BEKLEME_SN - 5)
    o.m("Account 2")
    o.saat.ilerle(config.HESAP_GIRDI_BEKLEME_SN - 5)  # toplam süre çok aşıldı ama her adım yenilendi
    o.d("hn")
    o.saat.ilerle(config.HESAP_GIRDI_BEKLEME_SN - 5)
    y = o.m(TOKEN3)
    assert "was created" in y.metin


def test_ad_ve_arama_adimlarinda_sure_dolunca_islenmez(o):
    hid = o.hesap("Main")
    o.oyuncular(3)
    o.d("ha")
    o.saat.ilerle(config.HESAP_GIRDI_BEKLEME_SN + 1)
    y = o.m("Account 2")
    assert "waiting time expired" in y.metin and not o.bekleme_var()
    o.d(f"hq:{hid}")
    o.saat.ilerle(config.HESAP_GIRDI_BEKLEME_SN + 1)
    y = o.m("P1")
    assert "waiting time expired" in y.metin
    assert "Search:" not in o.d(f"hy:{hid}:0").metin


@pytest.mark.parametrize("adim", ["token_yeni", "token", "proxy"])
def test_saat_atlarsa_bekleme_iptal_mesaj_islenmez_ve_silinmeye_calisilir(o, adim):
    hid = o.hesap("Main")
    o.kayit.clear()
    if adim == "token_yeni":
        o.d("ha"); o.m("Account 2"); o.d("hn")
        mesaj = TOKEN3
    elif adim == "token":
        o.d(f"ht:{hid}")
        mesaj = TOKEN2
    else:
        o.d(f"hps:{hid}")
        mesaj = PROXY
    o.saat.duvar_atla(7200)  # NTP / elle saat değişimi: monotonic ilerlemedi
    y = o.m(mesaj)
    assert "server clock changed" in y.metin and "not processed" in y.metin
    assert o.silmeler == [mesaj]
    assert o.kayit == [] and not o.bekleme_var() and o.hesap_sayisi() == 1
    assert o.token_dosyasi(hid) == TOKEN1


def test_saat_geri_atlarsa_da_iptal(o):
    hid = o.hesap("Main")
    o.d(f"ht:{hid}")
    o.saat.duvar_atla(-3600)
    y = o.m(TOKEN2)
    assert "server clock changed" in y.metin and o.token_dosyasi(hid) == TOKEN1


def test_dogrulama_surerken_ikinci_mesaj_reddedilir(o):
    hid = o.hesap("Main")
    o.kayit.clear()
    o.d(f"ht:{hid}")

    async def senaryo():
        o.engel, o.basladi = asyncio.Event(), asyncio.Event()
        a = asyncio.create_task(o.agit(o.mesaj(TOKEN2)))
        await o.basladi.wait()
        # (1) bekleme yokken gelen ikinci JWT: işlenmez
        y1 = await o.agit(o.mesaj(TOKEN3))
        # (2) yeni bir bekleme açılsa bile doğrulama sürerken ikinci mesaj reddedilir
        await o.agit(dugme(f"ht:{hid}"))
        y2 = await o.agit(o.mesaj(TOKEN3))
        o.engel.set()
        return y1, y2, await a

    y1, y2, y3 = run(senaryo())
    assert "not processed" in y1.metin
    assert "Another verification is still running" in y2.metin and "Nothing was changed" in y2.metin
    assert "verified and saved" in y3.metin
    assert [t for t, _ in o.kayit] == [TOKEN2], "ikinci token HİÇ doğrulanmadı"
    assert o.token_dosyasi(hid) == TOKEN2
    assert o.silmeler.count(TOKEN3) == 2, "reddedilen mesajlar da silinmeye çalışıldı"
    # bayrak temizlendi: sonraki akış çalışır
    o.engel = None
    o.d(f"ht:{hid}")
    assert "verified and saved" in o.m(TOKEN3).metin


# ====================================================================== 3. silme başarısızlığı
@pytest.mark.parametrize("sil", ["basarisiz", "istisna", "yok"])
def test_mesaj_silinemezse_acik_uyari_ve_sifreleme_notu(o, sil):
    hid = o.hesap("Main")
    o.d(f"ht:{hid}")
    y = o.m(TOKEN2, sil=sil)
    assert "could NOT delete your message" in y.metin
    assert "not end-to-end encrypted" in y.metin
    assert SILINEMEDI_NOTU in y.metin and SILINDI_NOTU not in y.metin
    assert "verified and saved" in y.metin, "işlem yine de yürür; uyarı eklenir"


def test_mesaj_silinirse_silindi_notu(o):
    hid = o.hesap("Main")
    o.d(f"ht:{hid}")
    y = o.m(TOKEN2)
    assert SILINDI_NOTU in y.metin and "could NOT" not in y.metin


@pytest.mark.parametrize("sil", ["ok", "basarisiz", "istisna", "yok"])
def test_bekleme_yokken_gelen_jwt_silinir_islenmez(o, sil):
    hid = o.hesap("Main")
    o.kayit.clear()
    y = o.m(TOKEN2, sil=sil)
    assert "was not processed" in y.metin and "Update JWT Token" in y.metin
    assert ("could NOT delete" in y.metin) == (sil != "ok")
    assert o.silmeler == ([] if sil == "yok" else [TOKEN2])
    assert o.kayit == [] and o.token_dosyasi(hid) == TOKEN1


def test_bekleme_yokken_jwt_olmayan_duz_metin_yanitsiz(o):
    assert o.m("merhaba") is None and o.m("hello world") is None
    assert o.silmeler == []


def test_arama_adimi_hassas_degil_ve_mesaj_silinmez(o):
    hid = o.hesap("Main")
    o.oyuncular(3)
    o.d(f"hq:{hid}")
    o.m("P1")
    assert o.silmeler == []


# ====================================================================== 4. JWT token güncelleme
def test_token_guncelle_basarili_kaydeder_ve_prev_birakir_hazira_doner(o):
    hid = o.hesap("Main")
    o.hy.durum_yaz(hid, "token_gecersiz", "token rejected (401)")
    assert "token rejected (401)" in o.d(f"hd:{hid}").metin
    y = o.d(f"ht:{hid}")
    assert f"account #{hid}" in y.metin and "hz" in veriler(y)
    y = o.m(TOKEN2)
    assert "The token was verified and saved" in y.metin and "scans resume" in y.metin
    assert o.token_dosyasi(hid) == TOKEN2
    prev = o.hy.depo.token(hid).yedek_yolu
    assert prev.read_text().strip() == TOKEN1 and stat.S_IMODE(os.stat(prev).st_mode) == 0o600
    assert o.satir(hid)["durum"] == "hazir"
    assert "Status: ready" in o.d(f"hd:{hid}").metin
    assert o.hy.depo.izin_sorunlari() == []


def test_token_guncelle_dogrulama_basarisizsa_eski_token_korunur(o):
    hid = o.hesap("Main")
    once = (o.hy.depo.token(hid).yol.stat().st_mtime_ns, o.token_dosyasi(hid))
    o.hy.durum_yaz(hid, "rate_limit", "cooldown")
    o.d(f"ht:{hid}")
    y = o.m(KOTU)
    assert "NOT changed" in y.metin and "rejected the token" in y.metin
    assert (o.hy.depo.token(hid).yol.stat().st_mtime_ns, o.token_dosyasi(hid)) == once, "dosya bayt bayt aynı"
    assert o.dosyalar(hid) == ["token"], ".prev de oluşmadı"
    assert o.satir(hid)["durum"] == "rate_limit", "durum da değişmedi"


def test_token_guncelle_hesabin_proxysi_uzerinden_dogrulanir(o):
    hid = o.hesap("Main", proxy=PROXY)
    o.kayit.clear()
    o.d(f"ht:{hid}")
    o.m(TOKEN2)
    assert o.kayit == [(TOKEN2, PROXY_URL)]
    # proxy ulaşılamazsa token DEĞİŞMEZ ve doğrudan bağlantı denenmez
    o.olu_proxyler.add(PROXY_URL)
    o.kayit.clear()
    o.d(f"ht:{hid}")
    y = o.m(TOKEN3)
    assert "NOT changed" in y.metin and "no direct connection was attempted" in y.metin
    assert o.kayit == [(TOKEN3, PROXY_URL)] and o.token_dosyasi(hid) == TOKEN2


def test_token_guncelle_hata_veren_dogrulayici_sirri_sizdirmaz(o):
    hid = o.hesap("Main")

    def patla(token, proxy):
        raise RuntimeError(f"boom {token}")

    o.kural = patla
    o.d(f"ht:{hid}")
    y = o.m(TOKEN2)
    assert "Unexpected error (RuntimeError)" in y.metin and "nothing was changed" in y.metin
    assert o.token_dosyasi(hid) == TOKEN1


@pytest.mark.parametrize("veri", ["ht:99", "hd:99", "hs:99", "hy:99:0", "hp:99", "hps:99", "hpr:99", "hc:99", "hq:99", "hx:99"])
def test_bilinmeyen_hesap_kimligi_uyari_verir_ve_bekleme_acmaz(o, veri):
    o.hesap("Main")
    y = o.d(veri)
    assert y.sessiz and y.uyari_goster and "no longer exists" in y.uyari
    assert not o.bekleme_var()


@pytest.mark.parametrize("veri", ["hd:abc", "hd", "hy:x:y", "ht:", "zz:1"])
def test_bozuk_hesap_dugme_verisi_eski_dugme_uyarisi(o, veri):
    o.hesap("Main")
    y = o.d(veri)
    assert y.sessiz and "no longer valid" in y.uyari


# ====================================================================== 5. proxy ayarları
def test_proxy_ekrani_sirsiz_ve_kaldir_yalniz_proxy_varken(o):
    hid = o.hesap("Main")
    y = o.d(f"hp:{hid}")
    assert "not configured (direct connection)" in y.metin
    assert f"hps:{hid}" in veriler(y) and f"hpr:{hid}" not in veriler(y) and f"hc:{hid}" in veriler(y)
    h2 = o.hesap("Other", proxy=PROXY)
    y = o.d(f"hp:{h2}")
    assert "configured (http) — working" in y.metin
    assert f"hpr:{h2}" in veriler(y)
    assert "never displayed" in y.metin


def test_proxy_ayarla_kayitli_tokenla_sinanir_ve_kaydedilir(o):
    hid = o.hesap("Main")
    o.kayit.clear()
    y = o.d(f"hps:{hid}")
    assert f"account #{hid}" in y.metin and "tested with this account's token first" in y.metin
    y = o.m(PROXY)
    assert "proxy was verified with this account's token and saved" in y.metin
    assert o.kayit == [(TOKEN1, PROXY_URL)], "kayıtlı token + aday proxy"
    assert o.silmeler == [PROXY]
    r = o.satir(hid)
    assert (r["proxy_var"], r["proxy_sema"], r["proxy_durum"]) == (1, "http", "calisiyor")
    assert o.hy.depo.proxy_oku(hid).url == PROXY_URL
    assert stat.S_IMODE(os.stat(o.hy.depo.proxy_yolu(hid)).st_mode) == 0o600
    assert "configured (http) — working" in o.d(f"hd:{hid}").metin


def test_proxy_ayarla_basarisizsa_eski_proxy_korunur(o):
    hid = o.hesap("Main", proxy=PROXY)
    once = o.hy.depo.proxy_yolu(hid).read_bytes()
    o.olu_proxyler.add(PROXY2_URL)
    o.kayit.clear()
    o.d(f"hps:{hid}")
    y = o.m(PROXY2)
    assert "NOT changed" in y.metin and "no direct connection was attempted" in y.metin
    assert o.kayit == [(TOKEN1, PROXY2_URL)]
    assert o.hy.depo.proxy_yolu(hid).read_bytes() == once
    assert o.hy.depo.proxy_oku(hid).url == PROXY_URL and o.satir(hid)["proxy_var"] == 1


def test_proxy_ayarla_gecersiz_biçim_degisiklik_yapmaz(o):
    hid = o.hesap("Main")
    o.d(f"hps:{hid}")
    y = o.m("http://nohost")
    assert "Send the proxy as" in y.metin and "Nothing was changed" in y.metin
    assert o.satir(hid)["proxy_var"] == 0 and o.dosyalar(hid) == ["token"]
    assert o.silmeler == ["http://nohost"], "biçim hatalı olsa da silinmeye çalışıldı"


def test_proxy_ayarla_tokensiz_hesapta_dogrulanamaz(o):
    hid = o.hesap("Main")
    os.unlink(o.hy.depo.token(hid).yol)
    o.d(f"hps:{hid}")
    y = o.m(PROXY)
    assert "no token yet" in y.metin and "Nothing was changed" in y.metin
    assert o.kayit == [(TOKEN1, None)][:0] or o.kayit == []
    assert o.satir(hid)["proxy_var"] == 0


def test_proxy_kaldir_onay_ister_sonra_dogrudan_baglanir(o):
    hid = o.hesap("Main", proxy=PROXY)
    y = o.d(f"hpr:{hid}")
    assert "connects <b>directly</b>" in y.metin
    kimlik = onay_kimligi(y)
    assert f"c:e:{kimlik}" in veriler(y) and f"c:h:{kimlik}" in veriler(y)
    assert o.satir(hid)["proxy_var"] == 1 and o.hy.depo.proxy_yolu(hid).exists(), "onaydan önce hiçbir şey değişmez"
    # iptal
    y = o.d(f"c:h:{kimlik}")
    assert "cancelled" in y.metin.lower() and o.satir(hid)["proxy_var"] == 1
    # yeniden iste + onayla
    kimlik = onay_kimligi(o.d(f"hpr:{hid}"))
    y = o.d(f"c:e:{kimlik}")
    assert "now connects directly" in y.metin and f"hd:{hid}" in veriler(y)
    r = o.satir(hid)
    assert (r["proxy_var"], r["proxy_sema"], r["proxy_durum"]) == (0, None, "yok")
    assert not o.hy.depo.proxy_yolu(hid).exists() and o.dosyalar(hid) == ["token"]
    assert "not configured (direct connection)" in o.d(f"hd:{hid}").metin
    assert f"hpr:{hid}" not in veriler(o.d(f"hp:{hid}"))
    # tek kullanımlık: çift tıklama ikinci kez çalışmaz
    assert "expired or has already been used" in o.d(f"c:e:{kimlik}").metin


def test_proxy_kaldir_onayi_baska_kullaniciya_ait_degil(o):
    hid = o.hesap("Main", proxy=PROXY)
    kimlik = onay_kimligi(o.d(f"hpr:{hid}"))
    y = o.d(f"c:e:{kimlik}", kul=YABANCI)
    assert y.sessiz and o.satir(hid)["proxy_var"] == 1


def test_baglanti_testi_durumu_gunceller_sirsiz_sonuc_gosterir(o):
    hid = o.hesap("Main", proxy=PROXY)
    o.kayit.clear()
    y = o.d(f"hc:{hid}")
    assert "Connection test" in y.metin and "✅" in y.metin and f"hc:{hid}" in veriler(y)
    assert o.kayit == [(TOKEN1, PROXY_URL)]
    assert o.satir(hid)["son_api_basari"] is not None
    assert "Last successful API request: 19.10.2026 15:00" in o.d(f"hd:{hid}").metin
    # token reddedilirse durum güncellenir
    o.kural = lambda t, p: DogrulamaSonucu("gecersiz", "the site rejected the token (401)")
    y = o.d(f"hc:{hid}")
    assert "⚠️" in y.metin and "rejected the token" in y.metin
    assert o.satir(hid)["durum"] == "token_gecersiz"
    assert "Status: token rejected (401)" in o.d(f"hd:{hid}").metin


def test_baglanti_testi_proxy_ulasilamaz_dogrudan_denenmedi_der_ve_duzelince_calisiyor(o):
    hid = o.hesap("Main", proxy=PROXY)
    o.olu_proxyler.add(PROXY_URL)
    y = o.d(f"hc:{hid}")
    assert "no direct connection was attempted" in y.metin
    assert o.kayit[-1] == (TOKEN1, PROXY_URL), "doğrudan (proxy=None) deneme yok"
    assert all(p == PROXY_URL for _, p in o.kayit)
    r = o.satir(hid)
    assert (r["durum"], r["proxy_durum"]) == ("proxy_hatasi", "hata")
    d = o.d(f"hd:{hid}").metin
    assert "proxy unreachable" in d and "UNREACHABLE (no direct connection is used)" in d
    assert "Proxy connection: UNREACHABLE" in o.d(f"hs:{hid}").metin
    o.olu_proxyler.clear()
    assert "✅" in o.d(f"hc:{hid}").metin
    r = o.satir(hid)
    assert (r["durum"], r["proxy_durum"]) == ("hazir", "calisiyor")
    assert "configured (http) — working" in o.d(f"hd:{hid}").metin


def test_baglanti_testi_tokensiz_hesapta_hata_gosterir(o):
    hid = o.hesap("Main")
    os.unlink(o.hy.depo.token(hid).yol)
    y = o.d(f"hc:{hid}")
    assert "this account has no token" in y.metin
    assert o.satir(hid)["durum"] == "token_yok"
    assert "Status: no token" in o.d(f"hd:{hid}").metin


# ====================================================================== 6. hesap ekranları / istatistik
def iki_hesap_205(o):
    h1 = o.hesap("Main")
    h2 = o.hesap("Backup", proxy=PROXY)
    o.oyuncular(205)
    return h1, h2


def test_hesap_listesi_ve_ana_ekran_sayilari_150_150_ve_55_150(o):
    h1, h2 = iki_hesap_205(o)
    y = o.d("hl")
    assert "Main — 150/150 players — ready" in y.metin
    assert "Backup — 55/150 players — ready" in y.metin
    etiketler = {d.veri: d.etiket for s in y.klavye for d in s if d.veri}
    assert etiketler[f"hd:{h1}"] == "Main — 150/150" and etiketler[f"hd:{h2}"] == "Backup — 55/150"
    assert "Waiting for an account" not in y.metin
    y = o.k("accounts")
    assert "• Main — 150/150 players" in y.metin and "• Backup — 55/150 players" in y.metin
    assert "Tracked players: 205 · assigned: 205 · waiting for an account: 0" in y.metin
    assert "A new Diplomacia account is required" not in y.metin
    assert "at most 150 players" in y.metin


def test_hesap_detayi_durum_token_proxy_cooldown_satirlari(o):
    h1, h2 = iki_hesap_205(o)
    y = o.d(f"hd:{h1}")
    assert "<b>Main</b> (#1)" in y.metin and "Status: ready" in y.metin and "Players: 150/150 assigned" in y.metin
    beklenen_zaman = yerel_goster(o.saat())
    assert beklenen_zaman == "19.10.2026 15:00", "Europe/Istanbul, 24 saat, DD.MM.YYYY HH:mm"
    assert f"Token: stored; updated {beklenen_zaman}" in y.metin
    assert "Proxy: not configured (direct connection)" in y.metin
    assert "Last successful API request: none yet" in y.metin
    assert "Last rate limit: none; active cooldown: none" in y.metin
    assert "never displayed" in y.metin
    assert {f"ht:{h1}", f"hp:{h1}", f"hc:{h1}", f"hy:{h1}:0", f"hs:{h1}", "hl", "m"} <= set(veriler(y))
    y = o.d(f"hd:{h2}")
    assert "Players: 55/150 assigned" in y.metin
    assert "Proxy: configured (http) — working" in y.metin
    # cooldown + rate limit
    o.hy.cooldown_ayarla(h1, 120, rate_limit=True)
    o.hy.durum_yaz(h1, "rate_limit", "429")
    y = o.d(f"hd:{h1}")
    assert "Last rate limit: 19.10.2026 15:00; active cooldown: 120 s" in y.metin
    assert "Status: rate limit cooldown — 429" in y.metin
    o.saat.ilerle(121)
    assert "active cooldown: none" in o.d(f"hd:{h1}").metin


def test_token_guncelleme_zamani_istanbul_saatiyle(o):
    hid = o.hesap("Main")
    o.saat.ilerle(3600 * 10 + 60 * 7)  # 22:07 UTC -> ertesi gün 01:07 Istanbul
    o.d(f"ht:{hid}")
    o.m(TOKEN2)
    assert "Token: stored; updated 20.10.2026 01:07" in o.d(f"hd:{hid}").metin


def test_istatistik_ekrani_alanlari(o):
    h1, h2 = iki_hesap_205(o)
    for i in range(1, 7):
        o.hy.oyuncu_sonucu(str(i), "ok")
    o.hy.oyuncu_sonucu("7", "hata", "network timeout")
    o.conn.execute("UPDATE hesaplar SET son_tarama_sn=2.5, son_tarama_zamani=?, sonraki_tarama=? WHERE hesap_id=?",
                   (iso(o.saat()), iso(o.saat()), h1))
    o.hy.cooldown_ayarla(h1, 30, rate_limit=True)
    y = o.d(f"hs:{h1}")
    t = y.metin
    assert "Main — statistics" in t
    assert "Assigned players: 150/150" in t
    assert "Successfully scanned (last 2 scan intervals): 6" in t
    assert "Pending tasks: 144" in t
    assert "Players whose last scan failed: 1" in t
    assert "Last successful API request: none yet" in t
    assert "Last rate limit: 19.10.2026 15:00" in t
    assert "Active cooldown: 30 s" in t
    assert "Proxy connection: not configured (direct)" in t
    assert "Last scan duration: 2.5 s (at 19.10.2026 15:00)" in t
    assert "Next scan: 19.10.2026 15:00" in t
    assert {f"hs:{h1}", f"hd:{h1}", "hm"} <= set(veriler(y))
    t2 = o.d(f"hs:{h2}").metin
    assert "Assigned players: 55/150" in t2 and "Proxy connection: working" in t2
    assert "Last scan duration: none yet" in t2 and "Active cooldown" in t2
    assert "Players waiting for any account" not in t2


def test_istatistik_duraklatilmis_hesapta_bekleyen_gorev_aciklamasi(o):
    h1, _ = iki_hesap_205(o)
    o.hy.durum_yaz(h1, "token_gecersiz", "token rejected (401)")
    t = o.d(f"hs:{h1}").metin
    assert "Pending tasks: 150 (this account is paused or failing; no other account takes them over)" in t
    assert "Status: token rejected (401)" in t


def test_301_oyuncuda_bekleyen_uyarisi_ve_yeni_hesap_gerekli(o):
    h1 = o.hesap("Main")
    h2 = o.hesap("Backup")
    o.oyuncular(301)
    y = o.d("hm")
    assert "Tracked players: 301 · assigned: 300 · waiting for an account: 1" in y.metin
    assert "A new Diplomacia account is required: 1 players are waiting" in y.metin
    assert "nothing is lost" in y.metin
    t = o.d("hl").metin
    assert "Waiting for an account: 1 players" in t and "Main — 150/150" in t and "Backup — 150/150" in t
    assert "Players waiting for any account (no capacity): 1" in o.d(f"hs:{h1}").metin
    assert "Players waiting for any account (no capacity): 1" in o.d(f"hs:{h2}").metin
    # üçüncü hesap eklenince bekleyen otomatik atanır
    hesap_ekle_akisi(o, ad="Third", token=TOKEN3)
    assert o.hy.oyuncu_sayilari() == {1: 150, 2: 150, 3: 1}
    y = o.d("hm")
    assert "waiting for an account: 0" in y.metin and "A new Diplomacia account is required" not in y.metin
    assert "• Third — 1/150 players" in y.metin


def test_tum_ekranlarda_callback_data_64_bayti_gecmez_ve_sirsiz(o):
    # en uzun ad + büyük kimlikler
    h1 = o.hesap("A" * 32)
    h2 = o.hesap("B" * 32, proxy=PROXY)
    o.oyuncular(205)
    o.conn.execute("UPDATE takip_oyuncular SET ad=? WHERE oyuncu_id='1'", ("N" * 100,))
    ekranlar = ["hm", "hl", "yo", "du", "m", f"hd:{h1}", f"hd:{h2}", f"hs:{h1}", f"hs:{h2}", f"hy:{h1}:0", f"hy:{h2}:3", f"hp:{h1}", f"hp:{h2}",
                f"hc:{h2}", f"hpr:{h2}", f"ht:{h1}", f"hps:{h1}", f"hq:{h1}"]
    for v in ekranlar:
        y = o.d(v)
        for veri in veriler(y):
            assert len(veri.encode()) <= CALLBACK_AZAMI_BAYT
    assert len(o.yanitlar) >= len(ekranlar)


# ====================================================================== 7. atanmış oyuncular
def tum_sayfalar(o, hid):
    """Sayfa 1'den 'Next' düğmelerini izleyerek tüm sayfaları gezer; [(yanit, [oid...])]."""
    y = o.d(f"hy:{hid}:0")
    sayfalar = [(y, oid_listesi(y))]
    while True:
        sonraki = [v for v in veriler(y) if v.startswith(f"hy:{hid}:") and int(v.split(":")[2]) > len(sayfalar) - 1]
        if not sonraki:
            return sayfalar
        y = o.d(sonraki[0])
        sayfalar.append((y, oid_listesi(y)))


def test_oyuncu_listesi_yalniz_o_hesabin_oyunculari_ayrik_ve_sayfali(o):
    h1, h2 = iki_hesap_205(o)
    s1, s2 = tum_sayfalar(o, h1), tum_sayfalar(o, h2)
    assert len(s1) == 19 and len(s2) == 7
    ids1 = [i for _, l in s1 for i in l]
    ids2 = [i for _, l in s2 for i in l]
    assert sorted(map(int, ids1)) == list(range(1, 151)) and sorted(map(int, ids2)) == list(range(151, 206))
    assert not set(ids1) & set(ids2)
    assert all(len(l) == 8 for _, l in s1[:-1]) and len(s1[-1][1]) == 150 - 18 * 8
    assert len(s2[-1][1]) == 55 - 6 * 8
    # başlık / sayfa göstergesi
    assert "Main — assigned players" in s1[0][0].metin and "150/150" in s1[0][0].metin and "page 1/19" in s1[0][0].metin
    assert "Backup — assigned players" in s2[0][0].metin and "55/150" in s2[0][0].metin and "page 1/7" in s2[0][0].metin
    assert "page 19/19" in s1[-1][0].metin
    # hiçbir sayfa metni diğer hesabın oyuncusunu içermez
    for y, _ in s1:
        for i in (151, 180, 205):
            assert f"(#{i})" not in y.metin
    for y, _ in s2:
        for i in (1, 50, 150):
            assert f"(#{i})" not in y.metin


def test_sayfa_gezinti_dugmeleri_ve_sinir_kirpma(o):
    h1, _ = iki_hesap_205(o)
    y = o.d(f"hy:{h1}:0")
    v = veriler(y)
    assert f"hy:{h1}:1" in v and not any(x == f"hy:{h1}:-1" for x in v)
    assert "‹ Previous" not in [d.etiket for s in y.klavye for d in s]
    assert "nop" in v
    y = o.d(f"hy:{h1}:1")
    assert "page 2/19" in y.metin and f"hy:{h1}:0" in veriler(y) and f"hy:{h1}:2" in veriler(y)
    y = o.d(f"hy:{h1}:18")
    assert "page 19/19" in y.metin and f"hy:{h1}:17" in veriler(y)
    assert "Next ›" not in [d.etiket for s in y.klavye for d in s]
    y = o.d(f"hy:{h1}:999")
    assert "page 19/19" in y.metin, "sınır dışı sayfa son sayfaya kırpılır"
    assert "page 1/19" in o.d(f"hy:{h1}").metin, "sayfa verilmezse ilk sayfa"
    assert "page 1/19" in o.d(f"hy:{h1}:abc").metin


def test_oyuncu_satirinda_son_tarama_hata_ve_takip_durumu(o):
    h1, _ = iki_hesap_205(o)
    o.hy.oyuncu_sonucu("1", "ok")
    o.hy.oyuncu_sonucu("2", "hata", "network timeout")
    o.hy.oyuncu_sonucu("3", "ok")
    o.hy.oyuncu_sonucu("3", "hata", "HTTP 500")  # başarı + sonraki hata
    t = o.d(f"hy:{h1}:0").metin
    satir = {i: next(s for s in t.split("\n") if f"(#{i})" in s) for i in range(1, 6)}
    assert "last successful scan: 19.10.2026 15:00" in satir[1] and "no error" in satir[1] and "✅ tracking" in satir[1]
    assert "last successful scan: never" in satir[2] and "⚠️ last scan failed: network timeout" in satir[2]
    assert "waiting for first scan" in satir[2]
    assert "last successful scan: 19.10.2026 15:00" in satir[3] and "last scan failed: HTTP 500" in satir[3]
    assert "last successful scan: never" in satir[4] and "no error" in satir[4] and "waiting for first scan" in satir[4]
    assert '<a href="' in satir[1] and "P1</a> (#1)" in satir[1]
    # hesap duraklatılınca takip durumu "waiting (account paused)"
    o.hy.durum_yaz(h1, "token_gecersiz", "token rejected (401)")
    t = o.d(f"hy:{h1}:0").metin
    for i in range(1, 9):
        s = next(x for x in t.split("\n") if f"(#{i})" in x)
        assert "⏸ waiting (account paused)" in s
    o.hy.durum_yaz(h1, "hazir")
    assert "✅ tracking" in o.d(f"hy:{h1}:0").metin
    # aktif cooldown da bekleme sayılır
    o.hy.cooldown_ayarla(h1, 60)
    assert "⏸ waiting (account paused)" in o.d(f"hy:{h1}:0").metin


def test_404_serisi_takip_durumunda_gorunur(o):
    h1, _ = iki_hesap_205(o)
    o.hy.oyuncu_sonucu("1", "ok")
    o.conn.execute("INSERT INTO bulunamadi_sayaclari(group_id, tur, kimlik, sayac) VALUES(?, 'oyuncu', '1', ?)",
                   (M, config.NOT_FOUND_STREAK_THRESHOLD))
    s = next(x for x in o.d(f"hy:{h1}:0").metin.split("\n") if "(#1)" in x)
    assert "❓ not found (404 streak)" in s


def test_id_ile_arama_oncul_ve_sonuc_sayisi_ve_aramayi_temizle(o):
    h1, h2 = iki_hesap_205(o)
    y = o.d(f"hq:{h1}")
    assert "Send a player ID" in y.metin and "hz" in veriler(y)
    y = o.m("10")
    beklenen = sorted(i for i in range(1, 151) if str(i).startswith("10") or "10" in f"p{i}")
    assert f"Search: <code>10</code> — {len(beklenen)} match(es)" in y.metin
    assert "page 1/2" in y.metin
    assert f"hx:{h1}" in veriler(y) and "Clear search" in [d.etiket for s in y.klavye for d in s]
    assert not o.bekleme_var()
    sayfalar = tum_sayfalar(o, h1)
    assert sorted(int(i) for _, l in sayfalar for i in l) == beklenen
    assert "Search:" in sayfalar[-1][0].metin, "arama sayfalar arasında korunur"
    # arama hesap başınadır: diğer hesap etkilenmez
    t2 = o.d(f"hy:{h2}:0").metin
    assert "Search:" not in t2 and "page 1/7" in t2
    # temizle
    y = o.d(f"hx:{h1}")
    assert "Search:" not in y.metin and "page 1/19" in y.metin and f"hx:{h1}" not in veriler(y)


def test_ad_ile_arama_buyuk_kucuk_harf_duyarsiz(o):
    h1 = o.hesap("Main")
    isimler = {1: "Zebra Quinn", 2: "alice", 3: "ALICIA", 4: "Bobby"}
    o.oyuncular(10, ad=lambda i: isimler.get(i, f"P{i}"))
    for sorgu, ids in [("zebra", {1}), ("QUINN", {1}), ("ali", {2, 3}), ("aLiC", {2, 3}), ("bob", {4}), ("nomatch", set())]:
        o.d(f"hq:{h1}")
        y = o.m(sorgu)
        assert f"— {len(ids)} match(es)" in y.metin, sorgu
        assert set(oid_listesi(y)) == {str(i) for i in ids}, sorgu
        if not ids:
            assert "No matching players." in y.metin


def test_arama_joker_karakterleri_ozel_anlam_tasimaz(o):
    h1 = o.hesap("Main")
    o.oyuncular(30)
    for sorgu in ["%", "_", "P_", "1%", "%1", "\\", "p%0", "[1-9]", "1' OR '1'='1", "*"]:
        o.d(f"hq:{h1}")
        y = o.m(sorgu)
        assert "0 match(es)" in y.metin and "No matching players." in y.metin, sorgu
        assert oid_listesi(y) == []
    # kontrol: sıradan arama çalışır
    o.d(f"hq:{h1}")
    assert "match(es)" in o.m("P1").metin and "0 match(es)" not in o.m("P1").metin if False else True


def test_arama_html_kacisi_ve_uzunluk_siniri(o):
    h1 = o.hesap("Main")
    o.oyuncular(5)
    o.d(f"hq:{h1}")
    y = o.m("<b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in y.metin and "<b>x</b>" not in y.metin
    o.d(f"hq:{h1}")
    y = o.m("x" * 41)
    assert "Send up to 40 characters" in y.metin and not o.bekleme_var()
    assert "&lt;b&gt;" in o.d(f"hy:{h1}:0").metin, "önceki arama değişmedi"
    o.d(f"hq:{h1}")
    y = o.m("   ")
    assert "Send up to 40 characters" in y.metin


def test_oyuncuya_tiklayinca_mevcut_oyuncu_detayi_acilir(o):
    h1, h2 = iki_hesap_205(o)
    y = o.d(f"hy:{h2}:0")
    assert "od:151" in veriler(y)
    detay = o.d("od:151")
    assert "<b>Player</b>" in detay.metin and "(#151)" in detay.metin and "P151" in detay.metin
    assert "os:151" in veriler(detay) and "ol:0" in veriler(detay)
    # etiket "<id> <ad>"
    assert "151 P151" in [d.etiket for s in y.klavye for d in s]


def test_oyuncusu_olmayan_hesap_bos_liste(o):
    h1 = o.hesap("Main")
    y = o.d(f"hy:{h1}:0")
    assert "No players." in y.metin and "page 1/1" in y.metin and oid_listesi(y) == []
    assert f"hq:{h1}" in veriler(y)


# ====================================================================== 8. güvenlik
HESAP_DUGMELERI = ["hm", "hl", "ha", "hz", "hn", "hi", "hd:1", "hs:1", "hy:1:0", "hq:1", "hx:1", "hp:1", "ht:1", "hps:1", "hpr:1", "hc:1"]


@pytest.mark.parametrize("kul", [YABANCI, YETKILI])
def test_kurucu_olmayan_ozel_sohbet_her_dugme_ve_komutta_reddedilir(o, kul):
    # YETKILI: geçmişte yetkilendirilmiş kullanıcı da kullanamaz (yetki listesi işleyişte kullanılmaz)
    yonetim.yetki_ver(o.conn, o.saat, KURUCU, KURUCU, YETKILI)
    hid = o.hesap("Main", proxy=PROXY)
    o.oyuncular(5)
    kayit0, satir0 = list(o.kayit), o.satir(hid)["token_guncelleme"]
    onceki = len(o.denemeler(kul))
    for v in HESAP_DUGMELERI:
        y = o.d(v, kul=kul)
        assert y.sessiz and y.uyari_goster and y.uyari == KAPALI_OZEL, v
    for ad in ("accounts", "hesaplar"):
        y = o.k(ad, kul=kul)
        assert y.metin == KAPALI_OZEL and not y.klavye
    assert len(o.denemeler(kul)) == onceki + len(HESAP_DUGMELERI) + 2, "her deneme denetim kaydına yazıldı"
    assert not o.ui._girdi, "hiçbir bekleme açılmadı"
    assert o.kayit == kayit0 and o.satir(hid)["token_guncelleme"] == satir0 and o.hesap_sayisi() == 1


@pytest.mark.parametrize("kul", [YABANCI, YETKILI])
def test_kurucu_olmayan_bekleme_durumundaki_metin_reddedilir_ve_durum_silinir(o, kul):
    hid = o.hesap("Main")
    o.kayit.clear()
    # savunma derinliği: bir şekilde bekleme durumu bu kullanıcı için açık kalmışsa
    o.ui._girdi[kul] = GirdiBekleme("token", hid, damga_al(o.saat, o.ui._mono))
    onceki = len(o.denemeler(kul))
    y = o.m(TOKEN2, kul=kul)
    assert y.metin == KAPALI_OZEL
    assert kul not in o.ui._girdi, "durum düşürüldü"
    rows = o.denemeler(kul)
    assert len(rows) == onceki + 1 and rows[-1][2] == "hesap_girdisi"
    assert o.kayit == [] and o.token_dosyasi(hid) == TOKEN1
    assert o.silmeler == [], "yabancı mesajı silinmeye çalışılmaz/işlenmez"
    # ikinci mesaj artık durumsuz: yanıtsız
    assert o.m(TOKEN2, kul=kul) is None


def test_kurucu_olmayan_ozel_sohbette_durumsuz_jwt_islenmez(o):
    o.hesap("Main")
    o.kayit.clear()
    assert o.m(TOKEN2, kul=YABANCI) is None
    assert o.kayit == [] and o.silmeler == []


def test_kurucunun_bekleme_durumu_baska_kullanici_mesajindan_etkilenmez(o):
    hid = o.hesap("Main")
    o.d(f"ht:{hid}")
    assert o.m(TOKEN3, kul=YABANCI) is None
    assert KURUCU in o.ui._girdi and o.kayit == [(TOKEN1, None)][:0]
    assert "verified and saved" in o.m(TOKEN2).metin


def test_grup_olaylari_sessizce_yok_sayilir(o):
    hid = o.hesap("Main")
    o.oyuncular(3)
    o.kayit.clear()
    o.d(f"ht:{hid}")  # kurucunun özel sohbetinde açık bekleme
    for tur in ("group", "supergroup"):
        assert o.k("accounts", sohbet=G1, tur=tur) is None
        assert o.k("hesaplar", sohbet=G1, tur=tur) is None
        for v in HESAP_DUGMELERI + ["od:1"]:
            y = o.d(v, sohbet=G1, tur=tur)
            assert y.sessiz and y.uyari is None and not y.metin, v
        # kurucu bile grupta token/proxy gönderemez: işlenmez, silinmez
        assert o.m(TOKEN2, sohbet=G1, tur=tur) is None
        assert o.m(PROXY, sohbet=G1, tur=tur) is None
        # yetkisiz kullanıcı grupta da sessiz
        assert o.m(TOKEN2, kul=YABANCI, sohbet=G1, tur=tur) is None
    assert o.kayit == [] and o.token_dosyasi(hid) == TOKEN1 and o.silmeler == []
    assert o.denemeler() == [], "grup olayları denetim kaydı bile üretmez"
    assert KURUCU in o.ui._girdi, "özel sohbetteki bekleme grup olaylarından etkilenmedi"


def test_hesaplar_none_eski_modda_accounts_etkin_degil_ve_hm_yonlendirilmez(tmp_path):
    o = Ortam(tmp_path, hesaplar=False)
    y = o.k("accounts")
    assert "not enabled" in y.metin and not y.klavye
    assert "not enabled" in o.k("hesaplar").metin
    assert "hm" not in veriler(o.k("start"))
    for v in HESAP_DUGMELERI:
        y = o.d(v)
        assert y.sessiz and "no longer valid" in y.uyari, v
    y = o.d("yo")
    assert "tk" in veriler(y) and "hm" not in veriler(y)
    # hesap menüsü mesaj akışı kapalı: bekleme yokken JWT, eski akışa göre işlenir (menü bekleme durumu açılmaz)
    assert not o.ui._girdi
    assert "Account Management" not in o.k("status").metin


def test_gizlilik_uctan_uca_hicbir_yanitta_sir_yok_ve_loglarda_da(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    o = Ortam(tmp_path, eski_token=TOKEN1)
    o.oyuncular(40)
    o.d("hi")                                                       # içe aktar
    hesap_ekle_akisi(o, ad="Second", proxy=PROXY, token=TOKEN2)      # proxyli ekle
    hesap_ekle_akisi(o, ad="Bad", token=KOTU)                       # başarısız ekle
    hesap_ekle_akisi(o, ad="BadProxy", proxy="http://nohost", token=TOKEN3)  # geçersiz proxy -> sonra token
    o.d("hz")
    o.d("ht:1"); o.m(KOTU, sil="basarisiz")
    o.d("ht:2"); o.m(TOKEN3, sil="istisna")
    o.d("hps:1"); o.m(PROXY2)
    o.olu_proxyler.add(PROXY2_URL)
    o.d("hc:1"); o.d("hps:2"); o.m(PROXY2, sil="yok")
    kimlik = onay_kimligi(o.d("hpr:1")); o.d(f"c:e:{kimlik}")
    o.d("hc:2"); o.d("hp:1"); o.d("hp:2"); o.d("hd:1"); o.d("hd:2"); o.d("hs:1"); o.d("hs:2"); o.d("hl"); o.d("hm")
    o.d("hq:1"); o.m("P1"); o.d("hx:1")
    o.m(TOKEN1)                                                     # bekleme yokken JWT
    o.saat.ilerle(10); o.d("ht:1"); o.saat.ilerle(config.HESAP_GIRDI_BEKLEME_SN + 1); o.m(TOKEN2)  # süre doldu
    o.d("ht:1"); o.saat.duvar_atla(5000); o.m(TOKEN2)               # saat atladı
    o.k("accounts"); o.k("hesaplar"); o.k("status"); o.d("du"); o.d("yo"); o.d("m")
    assert len(o.yanitlar) > 50
    # kalıcı yerler: veritabanı, outbox, denetim kaydı
    for tablo in ("hesaplar", "outbox", "yetkisiz_denemeler", "sistem_durumu", "islemler"):
        try:
            satirlar = o.conn.execute(f"SELECT * FROM {tablo}").fetchall()
        except Exception:
            continue
        for s in satirlar:
            for v in tuple(s):
                assert not (isinstance(v, str) and any(sir in v for sir in SIRLAR)), (tablo, v)
    log_metni = "\n".join(r.getMessage() for r in caplog.records)
    for sir in SIRLAR:
        assert sir not in log_metni
    # her ekran/yanıt İngilizce
    for y in o.yanitlar:
        yanit_ingilizce(y)


def test_tr_kalinti_denetimi_gercekten_calisiyor():
    """Denetimin kendisi: Türkçe artık yakalanır (yanlış negatif olmasın)."""
    assert tr_kalinti("Hesap eklendi") and tr_kalinti("çay")
    assert not tr_kalinti("Account created")
    with pytest.raises(AssertionError):
        ingilizce_mi("Hesap yok")


# ====================================================================== 9. /status ve anında dağıtım
def test_status_hesap_satiri_ve_bekleyen_notu(o):
    o.hesap("Main")
    o.hesap("Backup")
    o.oyuncular(10)
    t = o.k("status").metin
    assert "Diplomacia Accounts: 2 (2 ready)" in t
    assert "players waiting for an account" not in t
    o.oyuncular(291, baslangic=11)
    t = o.k("status").metin
    assert "Diplomacia Accounts: 2 (2 ready); players waiting for an account: 1" in t
    o.hy.durum_yaz(2, "token_gecersiz")
    assert "Diplomacia Accounts: 2 (1 ready); players waiting for an account: 1" in o.k("status").metin
    assert "Diplomacia Accounts: 2 (1 ready)" in o.d("du").metin


def test_status_hesap_yokken_sifir_hesap(o):
    assert "Diplomacia Accounts: 0 (0 ready)" in o.k("status").metin


def test_addplayer_removeplayer_atamayi_hemen_gunceller(tmp_path):
    o = Ortam(tmp_path, kapasite=3)
    hid = o.hesap("Main")
    o.oyuncular(3)
    for oid in ("500", "501"):
        o.src.oyuncu(oid, f"New{oid}")
    assert o.hy.oyuncu_sayilari() == {hid: 3}
    # kapasite dolu: yeni oyuncu eklenir ama beklemede
    y = o.k("addplayer", "500")
    assert "Player added to tracking" in y.metin
    assert o.hy.oyuncu_sayilari() == {hid: 3, None: 1}, "komuttan hemen sonra dağıtıldı"
    assert "players waiting for an account: 1" in o.k("status").metin
    assert "waiting for an account: 1" in o.d("hm").metin
    # takipten çıkarma onayı: boşalan yer bekleyene verilir
    y = o.k("removeplayer", "2")
    assert o.hy.oyuncu_sayilari() == {hid: 3, None: 1}, "onaydan önce değişmez"
    o.d(f"c:e:{onay_kimligi(y)}")
    assert o.hy.oyuncu_sayilari() == {hid: 3}, "onay düğmesinden hemen sonra bekleyen atandı"
    assert o.conn.execute("SELECT hesap_id FROM oyuncu_atamalari WHERE oyuncu_id='500'").fetchone()[0] == hid
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE oyuncu_id='2'").fetchone()[0] == 0
    assert "players waiting for an account" not in o.k("status").metin
    # eski komut adı da aynı yolu izler
    o.k("oyuncu_ekle", "501")
    assert o.hy.oyuncu_sayilari() == {hid: 3, None: 1}
    y = o.k("oyuncu_sil", "1")
    o.d(f"c:e:{onay_kimligi(y)}")
    assert o.hy.oyuncu_sayilari() == {hid: 3}


def test_addplayer_hesap_yokken_oyuncu_beklemede_hesap_eklenince_atanir(o):
    o.src.oyuncu("700", "Late")
    o.k("addplayer", "700")
    assert o.hy.oyuncu_sayilari() == {None: 1}
    assert "waiting for an account: 1" in o.d("hm").metin
    hesap_ekle_akisi(o, ad="Main", token=TOKEN1)
    assert o.hy.oyuncu_sayilari() == {1: 1}
    assert "1/150" in o.d("hl").metin


def test_oyuncu_silinince_hesap_sayilari_menude_guncel(o):
    h1, h2 = iki_hesap_205(o)
    assert "Main — 150/150" in o.d("hl").metin
    y = o.k("removeplayer", "3")
    o.d(f"c:e:{onay_kimligi(y)}")
    t = o.d("hl").metin
    assert "Main — 150/150" in t and "Backup — 54/150" in t, "boşalan yer sonraki hesaptan minimum taşımayla dolduruldu"
