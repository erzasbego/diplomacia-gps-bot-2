"""Çok hesaplı tarama zamanlayıcısı (CokluZamanlayici) davranış testleri. MOCK: sahte hesap kaynakları, bellek içi SQLite, ağ yok.

Kurallar (kurucu tarafından sabitlenmiş): merkezi alan MERKEZ_ID=0; hesap başına EN FAZLA 150 oyuncu, sıralı doldurma; arızalı hesabın
görevleri BEKLER (devralan yok); otomatik hesap açılmaz; proxy bozulunca doğrudan bağlantıya DÜŞÜLMEZ; bekleme (429/403/proxy) hesap
başına SQLite'ta kalıcıdır ve aynı çıkış noktasını paylaşan hesaplar arasında ortaktır; parti/savaş ve etkileşimli istekler yalnız
BİRİNCİL (en küçük hesap_id) hesapla yapılır; kurucu bildirimleri outbox'ta 'hesap_durumu' / 'hesap_gerekli'."""
import asyncio
import json
import logging
import os
from types import SimpleNamespace

import pytest

from conftest import KURUCU, bildirimler
from fakes import SahteGonderici, SahteKaynak, SahteSaat

from dipbot import config, db
from dipbot.coklu import CokluZamanlayici
from dipbot.hesaplar import GenelSinir, HesapYoneticisi
from dipbot.kaynak import (
    AgZamanAsimi,
    ErisimReddedildi,
    IstekSiniri,
    KaynakHatasi,
    ProxyErisilemez,
    TokenGecersiz,
    TokenYok,
)
from dipbot.takip import TakipServisi
from dipbot.tg import bicim
from dipbot.tg.kanal import KanalDogrulayici
from dipbot.tg.token import DogrulamaSonucu
from dipbot.zaman import iso

M = config.MERKEZ_ID
T_A = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjb2tsdUEifQ.c2lnbmF0dXJlQUFBQQ"
T_B = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjb2tsdUIifQ.c2lnbmF0dXJlQkJCQg"
T_C = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjb2tsdUMifQ.c2lnbmF0dXJlQ0NDQw"
T_D = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjb2tsdUQifQ.c2lnbmF0dXJlRERERA"
T_YENI = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ5ZW5pVCJ9.c2lnbmF0dXJlWVlZWQ"
T_KOTU = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJrb3R1VCJ9.c2lnbmF0dXJlS09UVQ"  # doğrulayıcı reddeder
TOKENLAR = [T_A, T_B, T_C, T_D]
PROXY_A = "http://proxyuser9:Pr0xyS3cretPW@10.20.30.40:8080"
PROXY_A2 = "http://baskakullanici:BaskaParola77@10.20.30.40:8080"  # aynı host:port = aynı çıkış noktası
PROXY_B = "http://pxb:Pr0xyOtherPW@10.20.30.41:8080"
SIRLAR = [T_A, T_B, T_C, T_D, T_YENI, T_KOTU, "Pr0xyS3cretPW", "proxyuser9", "10.20.30.40", "BaskaParola77",
          "baskakullanici", "Pr0xyOtherPW", "10.20.30.41", PROXY_A, PROXY_B]


class HesapSahte(SahteKaynak):
    """Hesap başına sahte uzak API: SahteKaynak + HesapKaynagi'nın beklediği öznitelikler."""

    yapilandirildi = True
    token_hazir = True

    async def aclose(self):
        self.kapatildi = getattr(self, "kapatildi", 0) + 1


class SahteDogrulayici:
    def __init__(self, sonuclar, proxy):
        self.sonuclar, self.proxy = sonuclar, proxy

    async def dogrula(self, token):
        return self.sonuclar.get(token) or DogrulamaSonucu("gecersiz", "the site rejected the token (401)")


class Ortam:
    """Hesap yöneticisi + zamanlayıcı + hesap başına sahte kaynaklar. TEK olay döngüsü kullanır (asyncio kilitleri döngüye bağlanır)."""

    def __init__(self, tmp_path, conn=None, saat=None):
        self.tmp = tmp_path
        self.conn = conn or db.baglan(":memory:")
        self.saat = saat or SahteSaat()
        self.loop = asyncio.new_event_loop()
        self.kaynaklar: dict[int, HesapSahte] = {}
        self.fab_kayit: list[tuple[int, str | None]] = []
        self.dogrulama = {t: DogrulamaSonucu("gecerli", "ok") for t in (*TOKENLAR, T_YENI)}
        self.profil: dict[str, tuple[str, str]] = {}  # oyuncu_id -> (ad, bölge)
        self.son_id = 0
        self._hesap_sayaci = 0
        self.aralik = config.DEFAULT_SCAN_INTERVAL_MINUTES * 60
        self.yeniden_baslat()

    # ---- kurulum
    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def kapat(self):
        try:
            self.loop.close()
        except Exception:
            pass

    def yeniden_baslat(self):
        """Süreç yeniden başlatma benzetimi: AYNI veritabanı ve veri dizini, yeni yönetici + zamanlayıcı (bellekteki durum yok)."""
        def fab(hesap_id, proxy, depo):
            self.fab_kayit.append((hesap_id, proxy.url if proxy else None))
            return self.kaynak(hesap_id)

        self.hy = HesapYoneticisi(
            self.conn, self.saat, self.tmp / "hesaplar", istemci_fabrikasi=fab,
            dogrulayici_fabrikasi=lambda proxy: SahteDogrulayici(self.dogrulama, proxy),
            genel_sinir=GenelSinir(esler=4, aralik=0), hesap_aralik=0)
        self.z = CokluZamanlayici(self.conn, self.hy, KURUCU, self.saat)

    def kaynak(self, hesap_id) -> HesapSahte:
        s = self.kaynaklar.get(hesap_id)
        if s is None:
            s = self.kaynaklar[hesap_id] = HesapSahte()
            for oid, (ad, bolge) in self.profil.items():
                s.oyuncu(oid, ad, bolge, "Ulke1")
            s.parti("5", "Party", [])
        return s

    def oyuncu_ekle(self, n):
        for _ in range(n):
            self.son_id += 1
            i = self.son_id
            self.conn.execute(
                "INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi, baseline_tamam) VALUES(?,?,?,?,1)",
                (M, str(i), f"P{i}", f"2026-10-01T10:{i // 60 % 60:02d}:{i % 60:02d}.{i:06d}Z"))
            self.profil[str(i)] = (f"P{i}", "Varna")
            for s in self.kaynaklar.values():
                s.oyuncu(str(i), f"P{i}", "Varna", "Ulke1")

    def bolge_degistir(self, oyuncu_id, bolge):
        ad, _ = self.profil[oyuncu_id]
        self.profil[oyuncu_id] = (ad, bolge)
        for s in self.kaynaklar.values():
            s.oyuncu(oyuncu_id, ad, bolge, "Ulke1")

    def hesap(self, ad, token=None, proxy=None) -> int:
        token = token or TOKENLAR[self._hesap_sayaci % len(TOKENLAR)]
        self._hesap_sayaci += 1
        hid, s = self.run(self.hy.hesap_olustur(ad, token, proxy))
        assert hid is not None, s
        return hid

    # ---- çalıştırma / gözlem
    def tick(self):
        return self.run(self.z.tick_ve_bekle())

    def ilerle_tick(self, sn):
        self.saat.ilerle(sn)
        return self.tick()

    def sonraki_hazir(self, *hids):
        """Hesapların 'sonraki tarama' zamanını serbest bırakır: yalnız BEKLEME (cooldown) kapısının etkisini ölçmek için."""
        if hids:
            for h in hids:
                self.conn.execute("UPDATE hesaplar SET sonraki_tarama=NULL WHERE hesap_id=?", (h,))
        else:
            self.conn.execute("UPDATE hesaplar SET sonraki_tarama=NULL")

    def say(self, hesap_id) -> list[str]:
        """Hesabın kaynağının aldığı fetch_player kimlikleri (çağrı sırasıyla)."""
        return [k for t, k in self.kaynak(hesap_id).cagrilar if t == "oyuncu"]

    def cagri_sayisi(self, hesap_id) -> int:
        return len(self.kaynak(hesap_id).cagrilar)

    def h(self, hesap_id):
        return self.hy.getir(hesap_id)

    def durumlar(self):
        return bildirimler(self.conn, "hesap_durumu")

    def ids(self, hesap_id) -> set[str]:
        return set(self.hy.atanan_idler(hesap_id))


@pytest.fixture
def o(tmp_path):
    ortam = Ortam(tmp_path)
    yield ortam
    ortam.kapat()


# ================================================================= 1. iki hesapla 205 oyuncu: 150/55
def test_205_oyuncu_iki_hesap_her_hesap_yalniz_kendi_idlerini_tarar(o):
    o.oyuncu_ekle(205)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    ids1, ids2 = o.ids(h1), o.ids(h2)
    assert (len(ids1), len(ids2)) == (150, 55) and not ids1 & ids2
    o.tick()
    c1, c2 = o.say(h1), o.say(h2)
    assert (len(c1), len(c2)) == (150, 55), "her oyuncu bir kez istendi"
    assert set(c1) == ids1 and set(c2) == ids2, "her hesap YALNIZ kendisine atanmış kimlikleri istedi"
    assert set(c1) | set(c2) == {str(i) for i in range(1, 206)} and not set(c1) & set(c2)
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE son_basari IS NULL").fetchone()[0] == 0
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE son_hata IS NOT NULL").fetchone()[0] == 0
    for hid in (h1, h2):
        r = o.h(hid)
        assert r["son_tarama_sn"] is not None and r["son_tarama_zamani"] == iso(o.saat()) and r["durum"] == "hazir"
        assert r["sonraki_tarama"] == iso(o.saat() + __import__("datetime").timedelta(seconds=o.aralik))
    j = o.conn.execute("SELECT * FROM tarama_isleri WHERE group_id=? AND is_tipi='oyuncular'", (M,)).fetchone()
    assert j is not None and j["son_basari"] is not None and j["son_hata"] is None
    # aralık dolmadan ikinci tick hiçbir hesabı taramaz
    n1, n2 = len(c1), len(c2)
    assert o.ilerle_tick(o.aralik - 60) == []
    assert (len(o.say(h1)), len(o.say(h2))) == (n1, n2)
    # aralık dolunca yeniden taranır
    o.saat.ilerle(61)
    o.tick()
    assert (len(o.say(h1)), len(o.say(h2))) == (2 * n1, 2 * n2)
    assert set(o.say(h1)) == ids1 and set(o.say(h2)) == ids2


# ================================================================= 2. token geçersiz: yalnız o hesap bekler
def test_token_gecersiz_hesap_bekler_digeri_surer_devralan_yok(o):
    o.oyuncu_ekle(205)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    ids1, ids2 = o.ids(h1), o.ids(h2)
    o.kaynak(h2).hatalar[("*", "*")] = TokenGecersiz("401")
    o.tick()
    assert o.h(h2)["durum"] == "token_gecersiz"
    b = o.durumlar()
    assert len(b) == 1 and b[0]["veri"]["durum"] == "token_gecersiz" and b[0]["veri"]["hesap_id"] == h2
    assert b[0]["hedef_sohbet"] == KURUCU and b[0]["grup_id"] is None
    assert set(o.say(h1)) == ids1 and len(o.say(h1)) == 150
    cagri2 = o.cagri_sayisi(h2)
    for _ in range(3):  # sonraki turlarda hesap 2 yeniden DENENMEZ; hesap 1 kendi görevlerini sürdürür
        o.ilerle_tick(o.aralik + 60)
    assert o.cagri_sayisi(h2) == cagri2, "token'ı geçersiz hesap yeniden denenmedi"
    assert len(o.say(h1)) == 150 * 4 and set(o.say(h1)) == ids1
    assert not set(o.say(h1)) & ids2, "hesap 1 hiçbir zaman hesap 2'nin oyuncusunu istemedi (devralma yok)"
    assert o.ids(h2) == ids2, "hesap 2'nin oyuncuları ona atanmış kaldı"
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=? AND son_basari IS NOT NULL", (h2,)).fetchone()[0] == 0
    assert len(o.durumlar()) == 1, "tekrarlayan turlar yinelenen bildirim üretmedi"
    assert o.h(h1)["durum"] == "hazir"


def test_token_yenilenince_hesap_devam_eder_ve_iyilesme_bildirimi_kuyruga_alinir(o):
    o.oyuncu_ekle(205)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    ids2 = o.ids(h2)
    o.kaynak(h2).hatalar[("*", "*")] = TokenGecersiz("401")
    o.tick()
    cagri2 = o.cagri_sayisi(h2)
    s = o.run(o.hy.token_guncelle(h2, T_YENI))
    assert s.gecerli
    o.kaynak(h2).hatalar.clear()
    o.tick()
    yeni = o.kaynak(h2).cagrilar[cagri2:]
    assert {k for t, k in yeni if t == "oyuncu"} == ids2 and len(yeni) == 55, "hesap 2 kendi 55 oyuncusunu taradı"
    assert o.h(h2)["durum"] == "hazir"
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=? AND son_basari IS NOT NULL", (h2,)).fetchone()[0] == 55
    b = o.durumlar()
    assert [x["veri"]["durum"] for x in b] == ["token_gecersiz", "hazir"], "geçiş + iyileşme bildirimi"
    assert all(x["hedef_sohbet"] == KURUCU and x["grup_id"] is None for x in b)
    for _ in range(2):
        o.ilerle_tick(o.aralik + 60)
    assert len(o.durumlar()) == 2, "tekrarlayan turlarda yinelenen bildirim yok"


# ================================================================= 3. 429: hesap bekleme penceresi
def _rl_kur(o, proxy1=None, proxy2=None, n=205, bekle=90):
    o.oyuncu_ekle(n)
    h1, h2 = o.hesap("A1", proxy=proxy1), o.hesap("A2", proxy=proxy2)
    o.kaynak(h1).hatalar[("oyuncu", "1")] = IstekSiniri("429", bekle)
    o.tick()
    return h1, h2


def test_istek_siniri_hesabi_bekletir_ve_sure_dolunca_devam_eder(o):
    h1, h2 = _rl_kur(o)
    r = o.h(h1)
    assert r["durum"] == "rate_limit" and r["son_rate_limit"] == iso(o.saat())
    assert r["cooldown_bitis"] == iso(o.saat() + __import__("datetime").timedelta(seconds=90))
    assert o.hy.cooldown_kalan(h1) == 90
    assert o.durumlar() == [], "rate limit kurucuya bildirilmez"
    n1, n2 = o.cagri_sayisi(h1), o.cagri_sayisi(h2)
    o.sonraki_hazir()  # tarama aralığı değil, yalnız bekleme penceresi ölçülsün
    assert o.ilerle_tick(60) == [] and o.cagri_sayisi(h1) == n1 and o.cagri_sayisi(h2) == n2, "60 sn: hâlâ bekleniyor"
    o.kaynak(h1).hatalar.clear()
    o.ilerle_tick(31)  # toplam 91 sn > 90
    assert o.cagri_sayisi(h1) > n1, "bekleme bitince hesap 1 devam etti"
    r = o.h(h1)
    assert r["durum"] == "hazir" and r["cooldown_bitis"] is None
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=? AND son_basari IS NULL", (h1,)).fetchone()[0] == 0


@pytest.mark.parametrize(
    "p1,p2,paylasir",
    [
        (None, None, True),          # ikisi de doğrudan: aynı IP
        (PROXY_A, PROXY_A2, True),   # aynı proxy host:port (farklı kimlik bilgisi): aynı çıkış noktası
        (None, PROXY_B, False),      # doğrudan / proxy
        (PROXY_A, PROXY_B, False),   # farklı proxy'ler
    ],
)
def test_ayni_cikis_noktasini_paylasan_hesaplar_bekleme_penceresini_paylasir(o, p1, p2, paylasir):
    h1, h2 = _rl_kur(o, p1, p2)
    assert o.hy.cooldown_kalan(h2) == (90 if paylasir else 0)
    assert o.h(h2)["cooldown_bitis"] is None, "diğer hesabın kendi penceresi yok; yalnız ortak çıkış nedeniyle bekler"
    n1, n2 = o.cagri_sayisi(h1), o.cagri_sayisi(h2)
    o.sonraki_hazir()
    o.ilerle_tick(60)
    assert o.cagri_sayisi(h1) == n1, "429 alan hesap bekliyor"
    if paylasir:
        assert o.cagri_sayisi(h2) == n2, "ortak çıkış noktası: diğer hesap da duraklatıldı"
    else:
        assert o.cagri_sayisi(h2) > n2, "farklı çıkış noktası: diğer hesap etkilenmedi"


def test_bekleme_penceresi_yeniden_baslatmada_korunur(o):
    h1, h2 = _rl_kur(o)
    n1, n2 = o.cagri_sayisi(h1), o.cagri_sayisi(h2)
    o.yeniden_baslat()  # bellekteki her şey gider; yalnız SQLite + dosyalar kalır
    o.sonraki_hazir()
    assert o.hy.cooldown_kalan(h1) == 90
    assert o.ilerle_tick(30) == []
    assert (o.cagri_sayisi(h1), o.cagri_sayisi(h2)) == (n1, n2)
    o.kaynak(h1).hatalar.clear()
    o.ilerle_tick(61)  # toplam 91 sn
    assert o.cagri_sayisi(h1) > n1 and o.cagri_sayisi(h2) > n2


# ================================================================= 4. 403: kısa geri çekilme, bildirim yok
def test_erisim_reddi_kisa_geri_cekilme_bildirimsiz_ve_iyilesir(o):
    o.oyuncu_ekle(205)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    o.kaynak(h1).hatalar[("oyuncu", "1")] = ErisimReddedildi("403")
    o.tick()
    r = o.h(h1)
    assert r["durum"] == "erisim_reddi" and o.hy.cooldown_kalan(h1) == config.ERISIM_REDDI_BEKLEME_SECONDS
    assert r["son_rate_limit"] is None, "403 rate limit sayılmaz"
    assert bildirimler(o.conn, "hesap_durumu") == [] and bildirimler(o.conn, "hesap_gerekli") == []
    n1 = o.cagri_sayisi(h1)
    o.sonraki_hazir()
    o.ilerle_tick(config.ERISIM_REDDI_BEKLEME_SECONDS - 5)
    assert o.cagri_sayisi(h1) == n1
    o.kaynak(h1).hatalar.clear()
    o.ilerle_tick(10)
    r = o.h(h1)
    assert o.cagri_sayisi(h1) > n1 and r["durum"] == "hazir" and r["cooldown_bitis"] is None
    assert bildirimler(o.conn, "hesap_durumu") == [], "iyileşme de bildirimsiz (yalnız token/proxy geçişleri bildirilir)"


# ================================================================= 5. proxy hatası: doğrudan bağlantıya geçiş YOK
def test_proxy_hatasi_ustel_geri_cekilme_dogrudan_baglanti_yok_ve_iyilesme(o):
    o.oyuncu_ekle(205)
    h1 = o.hesap("Direct")
    h2 = o.hesap("Proxied", proxy=PROXY_A)
    o.kaynak(h2).hatalar[("*", "*")] = ProxyErisilemez("cannot connect")
    o.tick()
    beklenen = [60, 120, 240, 480, 900, 900]
    assert beklenen[0] == config.HESAP_PROXY_GERI_CEKILME_SN and beklenen[-1] == config.HESAP_PROXY_GERI_CEKILME_UST_SN
    for n, bekle in enumerate(beklenen, 1):
        if n > 1:
            o.saat.ilerle(beklenen[n - 2] + 1)
            o.sonraki_hazir(h2)
            o.tick()
        r = o.h(h2)
        assert r["durum"] == "proxy_hatasi" and r["proxy_durum"] == "hata" and r["ardisik_hata"] == n
        assert o.hy.cooldown_kalan(h2) == bekle, f"{n}. hata: {bekle} sn"
        assert len(o.durumlar()) == 1, "geçiş bildirimi YALNIZ bir kez"
    b = o.durumlar()[0]
    assert b["veri"]["durum"] == "proxy_hatasi" and b["hedef_sohbet"] == KURUCU and b["grup_id"] is None
    # doğrudan bağlantı yok: hesap 2'nin istemcisi HEP proxy ile kuruldu
    kayit2 = [p for hid, p in o.fab_kayit if hid == h2]
    assert kayit2 and all(p == PROXY_A for p in kayit2), kayit2
    assert (h1, None) in o.fab_kayit
    # diğer hesap etkilenmedi (farklı çıkış noktası)
    assert o.h(h1)["durum"] == "hazir" and o.hy.cooldown_kalan(h1) == 0
    assert set(o.say(h1)) == o.ids(h1) and not set(o.say(h1)) & o.ids(h2)
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=? AND son_basari IS NOT NULL", (h2,)).fetchone()[0] == 0
    # proxy düzelir -> hazır + iyileşme bildirimi
    o.kaynak(h2).hatalar.clear()
    o.saat.ilerle(901)
    o.sonraki_hazir(h2)
    o.tick()
    r = o.h(h2)
    assert (r["durum"], r["proxy_durum"], r["ardisik_hata"], r["cooldown_bitis"]) == ("hazir", "calisiyor", 0, None)
    assert [x["veri"]["durum"] for x in o.durumlar()] == ["proxy_hatasi", "hazir"]
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=? AND son_basari IS NOT NULL", (h2,)).fetchone()[0] == 55
    assert all(p == PROXY_A for hid, p in o.fab_kayit if hid == h2)


def test_okunamayan_proxy_dosyasi_istemci_fabrikasini_proxysiz_cagirmaz(o):
    o.oyuncu_ekle(205)
    h1 = o.hesap("Direct")
    h2 = o.hesap("Proxied", proxy=PROXY_A)
    (o.tmp / "hesaplar" / str(h2) / "proxy").write_text("bozuk")
    o.tick()
    assert o.h(h2)["durum"] == "proxy_hatasi"
    assert [p for hid, p in o.fab_kayit if hid == h2] == [], "okunamayan proxy: istemci HİÇ kurulmadı (proxysiz kurulmadı)"
    assert o.say(h2) == []
    assert len(o.durumlar()) == 1 and set(o.say(h1)) == o.ids(h1)


@pytest.mark.xfail(strict=True, reason="SRC: HesapYoneticisi.egress_anahtari bozuk proxy dosyasında 'direct' döner; proxy'li hesabın "
                   "bekleme penceresi doğrudan hesabı da duraklatır (hesaplar.py:384-386)")
def test_okunamayan_proxy_dosyasi_dogrudan_hesabin_beklemesini_paylasmaz(o):
    o.oyuncu_ekle(205)
    h1 = o.hesap("Direct")
    h2 = o.hesap("Proxied", proxy=PROXY_A)
    (o.tmp / "hesaplar" / str(h2) / "proxy").write_text("bozuk")
    o.tick()
    assert o.hy.cooldown_kalan(h2) > 0
    assert o.hy.cooldown_kalan(h1) == 0, "proxy'li hesabın arızası doğrudan hesabı bekletmemeli"


# ================================================================= 6. kapasite
def test_kapasite_asildi_tek_hesap_gerekli_bildirimi_24_saatte_bir_ucuncu_hesap_atar(o):
    o.oyuncu_ekle(301)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    o.tick()
    b = bildirimler(o.conn, "hesap_gerekli")
    assert len(b) == 1 and b[0]["hedef_sohbet"] == KURUCU and b[0]["grup_id"] is None
    v = b[0]["veri"]
    assert (v["bekleyen"], v["kapasite"], v["hesap_sayisi"]) == (1, 150, 2)
    assert o.conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='hesap_gerekli_zaman'").fetchone() is not None
    bekleyen = [r[0] for r in o.conn.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id IS NULL")]
    assert bekleyen == ["301"]
    assert o.conn.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE oyuncu_id='301'").fetchone()[0] == 1, "oyuncu silinmedi"
    assert "301" not in o.say(h1) + o.say(h2), "bekleyen oyuncu taranmadı"
    assert len(o.say(h1)) == 150 and len(o.say(h2)) == 150
    for _ in range(4):  # 24 saatten az
        o.ilerle_tick(o.aralik + 60)
    assert len(bildirimler(o.conn, "hesap_gerekli")) == 1
    o.ilerle_tick(25 * 3600)
    b = bildirimler(o.conn, "hesap_gerekli")
    assert len(b) == 2 and b[1]["veri"]["bekleyen"] == 1
    assert "301" not in o.say(h1) + o.say(h2)
    o.ilerle_tick(o.aralik + 60)
    assert len(bildirimler(o.conn, "hesap_gerekli")) == 2
    # kurucu ÜÇÜNCÜ hesabı açıkça ekler -> oyuncu 3. hesaba atanır ve taranır; uyarı zamanı temizlenir
    h3 = o.hesap("A3")
    assert o.ids(h3) == {"301"} and o.ids(h1) | o.ids(h2) == {str(i) for i in range(1, 301)}
    o.ilerle_tick(60)
    assert o.say(h3) == ["301"]
    assert o.conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='hesap_gerekli_zaman'").fetchone() is None
    assert o.conn.execute("SELECT son_basari FROM oyuncu_atamalari WHERE oyuncu_id='301'").fetchone()[0] is not None
    o.ilerle_tick(o.aralik + 60)
    assert len(bildirimler(o.conn, "hesap_gerekli")) == 2, "kapasite sorunu bitti: yeni uyarı yok"


def test_hic_hesap_yokken_hesap_gerekli_gonderilir_hicbir_sey_taranmaz_hesap_acilmaz(o):
    o.oyuncu_ekle(12)
    assert o.tick() == []
    b = bildirimler(o.conn, "hesap_gerekli")
    assert len(b) == 1 and b[0]["hedef_sohbet"] == KURUCU
    assert b[0]["veri"]["hesap_sayisi"] == 0 and b[0]["veri"]["bekleyen"] == 12
    assert o.hy.listele() == [], "hesap OTOMATİK oluşturulmadı"
    assert o.kaynaklar == {} and o.fab_kayit == [], "hiçbir istek/istemci yok"
    assert o.conn.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 12
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id IS NULL").fetchone()[0] == 12
    o.ilerle_tick(o.aralik + 60)
    assert len(bildirimler(o.conn, "hesap_gerekli")) == 1


# ================================================================= 7. parti/savaş ve etkileşimli istekler: yalnız BİRİNCİL hesap
def _parti_kur(o):
    o.conn.execute("INSERT INTO takip_partiler(group_id, parti_id, ad, eklendi) VALUES(?,?,?,?)",
                   (M, "5", "Party", "2026-10-01T10:00:00.000000Z"))


def test_parti_ve_savas_taramasi_yalniz_birincil_hesapla(o):
    o.oyuncu_ekle(205)
    _parti_kur(o)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    o.tick()
    s1, s2 = o.kaynak(h1), o.kaynak(h2)
    assert s1.cagri_say("parti") == 1 and s1.cagri_say("savaslar") == 1
    assert s2.cagri_say("parti") == 0 and s2.cagri_say("savaslar") == 0 and s2.cagri_say("savas") == 0
    assert len(o.say(h2)) == 55
    o.ilerle_tick(o.aralik * 3)
    assert s2.cagri_say("parti") == 0 and s2.cagri_say("savaslar") == 0
    assert s1.cagri_say("parti") >= 2


def test_birincil_hesap_bozulunca_parti_savas_bekler_digeri_yalniz_kendi_oyunculari(o):
    o.oyuncu_ekle(205)
    _parti_kur(o)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    ids1, ids2 = o.ids(h1), o.ids(h2)
    s1, s2 = o.kaynak(h1), o.kaynak(h2)
    s1.hatalar[("*", "*")] = TokenGecersiz("401")
    o.tick()
    assert o.h(h1)["durum"] == "token_gecersiz" and o.h(h2)["durum"] == "hazir"
    assert set(o.say(h2)) == ids2 and len(o.say(h2)) == 55, "hesap 2 kendi oyuncularını taradı"
    n1 = o.cagri_sayisi(h1)
    for _ in range(3):
        o.ilerle_tick(o.aralik * 3)
    assert o.cagri_sayisi(h1) == n1, "bozuk birincil yeniden denenmedi"
    assert s2.cagri_say("parti") == 0 and s2.cagri_say("savaslar") == 0 and s2.cagri_say("savas") == 0, \
        "parti/savaş başka hesaba GEÇMEDİ (bekler)"
    assert len(o.say(h2)) == 55 * 4 and set(o.say(h2)) == ids2
    assert not set(o.say(h1)) & ids2
    assert o.ids(h1) == ids1
    # etkileşimli istek: birincil bozukken açık kullanıcı mesajıyla reddedilir (hesap 2'ye düşmez)
    with pytest.raises(TokenGecersiz) as e:
        o.z.ag_onkontrol()
    assert "primary" in e.value.kullanici_mesaji and "Account Management" in e.value.kullanici_mesaji
    assert o.z.token_duraklatildi is True
    n2 = o.cagri_sayisi(h2)
    with pytest.raises(TokenGecersiz):
        o.run(o.z.kaynak.fetch_player("1"))
    assert o.cagri_sayisi(h2) == n2 and o.cagri_sayisi(h1) == n1 + 1, "etkileşimli istek birincil hesaba gitti"


def test_etkilesimli_kaynak_birincil_hesaba_gider_onkontrol_saglamken_gecer(o):
    o.oyuncu_ekle(205)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    o.z.ag_onkontrol()  # hata yok
    s = o.run(o.z.kaynak.fetch_player("7"))
    assert s.oyuncu_id == "7"
    assert ("oyuncu", "7") in o.kaynak(h1).cagrilar and o.kaynak(h2).cagrilar == []
    assert o.z.token_duraklatildi is False


def test_hesap_yokken_etkilesimli_istek_acik_mesajla_reddedilir(o):
    with pytest.raises(TokenYok) as e:
        o.z.ag_onkontrol()
    assert "Account Management" in e.value.kullanici_mesaji
    with pytest.raises(TokenYok):
        o.run(o.z.kaynak.fetch_player("1"))
    assert o.fab_kayit == []


def test_birincil_bekleme_penceresinde_onkontrol_istek_siniri_firlatir(o):
    o.oyuncu_ekle(10)
    h1 = o.hesap("A1")
    o.hy.cooldown_ayarla(h1, 45, rate_limit=True)
    with pytest.raises(IstekSiniri) as e:
        o.z.ag_onkontrol()
    assert "45" in e.value.kullanici_mesaji


# ================================================================= 8. atama değişikliği geçmişi sıfırlamaz, bildirim yinelemez
def test_atama_degisikligi_gecmisi_sifirlamaz_ve_tek_degisiklik_bildirimi_uretir(o):
    o.oyuncu_ekle(301)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    o.tick()  # baseline (bölge/ülke yazılır)
    assert o.conn.execute("SELECT hesap_id FROM oyuncu_atamalari WHERE oyuncu_id='300'").fetchone()[0] == h2
    assert o.conn.execute("SELECT hesap_id FROM oyuncu_atamalari WHERE oyuncu_id='301'").fetchone()[0] is None
    once = tuple(o.conn.execute("SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id='300'", (M,)).fetchone())
    assert once[3] == "Varna" and once[4] == "Ulke1" and once[5] is not None and once[6] == 1
    assert bildirimler(o.conn, "oyuncu_degisti") == []
    # hesap 1'den bir oyuncu silinir: boşalan yeri hesap 2'nin son atananı (300) doldurur, beklemedeki 301 hesap 2'ye girer
    o.conn.execute("DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id='5'", (M,))
    o.hy.dagit()
    assert o.conn.execute("SELECT hesap_id FROM oyuncu_atamalari WHERE oyuncu_id='300'").fetchone()[0] == h1, "300: hesap 2 -> 1"
    assert o.conn.execute("SELECT hesap_id FROM oyuncu_atamalari WHERE oyuncu_id='301'").fetchone()[0] == h2
    sonra = tuple(o.conn.execute("SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id='300'", (M,)).fetchone())
    assert sonra == once, "taşınan oyuncunun bölge/vatandaşlık/son gözlem/baseline satırı BİREBİR aynı"
    # taşımadan SONRA gerçek bölge değişimi: tam bir bildirim
    o.bolge_degistir("300", "Sofia")
    o.ilerle_tick(o.aralik + 60)
    assert 300 and "300" in o.say(h1)[150:], "300 artık hesap 1 tarafından tarandı"
    b = bildirimler(o.conn, "oyuncu_degisti")
    assert len(b) == 1
    assert b[0]["hedef_sohbet"] == config.LOG_KANAL_ID and b[0]["grup_id"] == M
    assert b[0]["veri"]["oyuncu_id"] == "300"
    assert [(d["alan"], d["eski"], d["yeni"]) for d in b[0]["veri"]["degisimler"]] == [("bolge", "Varna", "Sofia")]
    # 301 ilk kez tarandı: baseline, bildirimsiz; değişmeyen oyuncular bildirim üretmez
    assert o.conn.execute("SELECT son_basari FROM oyuncu_atamalari WHERE oyuncu_id='301'").fetchone()[0] is not None
    for _ in range(2):
        o.ilerle_tick(o.aralik + 60)
    assert len(bildirimler(o.conn, "oyuncu_degisti")) == 1, "yinelenen/fazla bildirim yok"
    r = o.conn.execute("SELECT bolge, vatandaslik_ulkesi, baseline_tamam FROM takip_oyuncular WHERE oyuncu_id='300'").fetchone()
    assert tuple(r) == ("Sofia", "Ulke1", 1)


# ================================================================= 9. calis döngüsü
class KanalBot:
    def __init__(self, uye="administrator"):
        self.uye = uye

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, type="channel", title="Log")

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=self.uye, can_post_messages=True)


def _dongu(o, z, g, monkeypatch, sure=0.3):
    async def dongu():
        durdur = asyncio.Event()

        async def durdurucu():
            await asyncio.sleep(sure)
            durdur.set()

        monkeypatch.setattr(config, "TICK_SECONDS", 0.05)
        await asyncio.gather(z.calis(g, durdur), durdurucu())

    o.run(dongu())


def test_calis_dongusu_kanal_hazir_degilken_hicbir_hesap_taranmaz(o, monkeypatch):
    o.oyuncu_ekle(205)
    o.hesap("A1"), o.hesap("A2")
    z = CokluZamanlayici(o.conn, o.hy, KURUCU, o.saat, kanal=KanalDogrulayici(KanalBot(uye="member"), config.LOG_KANAL_ID, o.saat))
    g = SahteGonderici()
    _dongu(o, z, g, monkeypatch)
    assert all(s.cagrilar == [] for s in o.kaynaklar.values()), "kanal hazır değil: tarama yok"
    assert o.conn.execute("SELECT COUNT(*) FROM tarama_isleri").fetchone()[0] == 0
    assert [t for _, t, _ in g.gonderilen] == ["kanal_durumu"] and g.gonderilen[0][0] == KURUCU
    assert o.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id IS NOT NULL").fetchone()[0] == 205, "atama yine de sürer"


def test_calis_dongusu_kanal_hazirsa_hesaplar_taranir_bildirimler_yalniz_log_kanalina_gider(o, monkeypatch):
    o.oyuncu_ekle(205)
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    z = CokluZamanlayici(o.conn, o.hy, KURUCU, o.saat, kanal=KanalDogrulayici(KanalBot(), config.LOG_KANAL_ID, o.saat))
    o.z = z
    o.tick()  # baseline
    o.bolge_degistir("10", "Sofia")   # hesap 1'in oyuncusu
    o.bolge_degistir("200", "Burgas")  # hesap 2'nin oyuncusu
    o.saat.ilerle(o.aralik + 60)
    g = SahteGonderici()
    _dongu(o, z, g, monkeypatch)
    assert len(o.say(h1)) == 300 and len(o.say(h2)) == 110, "iki hesap da döngüde yeniden tarandı"
    assert {h for h, _, _ in g.gonderilen} == {config.LOG_KANAL_ID}
    assert sorted(v["oyuncu_id"] for _, t, v in g.gonderilen if t == "oyuncu_degisti") == ["10", "200"]
    assert {t for _, t, _ in g.gonderilen} == {"oyuncu_degisti"}


# ================================================================= 10. takip.oyuncular_tara kapsamı, hatırlatma, .prev
def test_oyuncular_tara_yalniz_listelenen_idleri_ceker_ve_sonuc_geri_cagrisi(conn, saat, src, servis):
    for i in ("1", "2", "3", "9"):
        conn.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi, baseline_tamam) VALUES(?,?,?,?,1)",
                     (M, i, f"P{i}", f"2026-10-01T10:00:0{i[0]}.000000Z"))
    for i in ("1", "2", "3"):
        src.oyuncu(i, f"P{i}", "Varna", "Ulke1")  # "9" kaynakta yok -> 404
    src.hatalar[("oyuncu", "2")] = AgZamanAsimi("t")
    sonuclar = []
    sn = asyncio.run(servis.oyuncular_tara(M, src, oyuncu_idleri=["1", "2", "9", "777"],
                                           oyuncu_sonucu=lambda *a: sonuclar.append(a)))
    assert src.cagrilar == [("oyuncu", "1"), ("oyuncu", "2"), ("oyuncu", "9")], "yalnız listelenen + takipte olanlar; 3 ve 777 çekilmedi"
    assert [(p, d) for p, d, _ in sonuclar] == [("1", "ok"), ("2", "hata"), ("9", "yok")]
    assert sonuclar[0][2] == "" and sonuclar[1][2] and sonuclar[2][2]
    assert sn.istenen == 3 and sn.gozlenen == 1
    # boş liste = hiçbir şey; None = hepsi
    src.cagrilar.clear()
    asyncio.run(servis.oyuncular_tara(M, src, oyuncu_idleri=[]))
    assert src.cagrilar == []
    src.hatalar.clear()
    asyncio.run(servis.oyuncular_tara(M, src))
    assert [k for _, k in src.cagrilar] == ["1", "2", "3", "9"]


def test_token_hatirlatma_hesap_basina_haftalik(o):
    o.oyuncu_ekle(5)
    h1, h2 = o.hesap("Eski Hesap"), o.hesap("Taze Hesap")
    assert o.z.token_hatirlat() is False and bildirimler(o.conn, "token_hatirlatma") == []
    eski = iso(o.saat() - __import__("datetime").timedelta(days=8))
    o.conn.execute("UPDATE hesaplar SET token_guncelleme=? WHERE hesap_id=?", (eski, h1))
    assert o.z.token_hatirlat() is True
    b = bildirimler(o.conn, "token_hatirlatma")
    assert len(b) == 1 and b[0]["veri"]["hesap_ad"] == "Eski Hesap" and b[0]["hedef_sohbet"] == KURUCU
    assert "Eski Hesap" in bicim.bildirim_metni("token_hatirlatma", b[0]["veri"])
    assert o.z.token_hatirlat() is False and len(bildirimler(o.conn, "token_hatirlatma")) == 1, "aynı hafta yinelenmez"
    o.saat.ilerle(7 * 86400)  # 15 gün: ikinci hafta
    assert o.z.token_hatirlat() is True
    b = bildirimler(o.conn, "token_hatirlatma")
    assert len(b) == 3 or len(b) == 2
    assert {x["veri"]["hesap_ad"] for x in b} == {"Eski Hesap", "Taze Hesap"} or {x["veri"]["hesap_ad"] for x in b} == {"Eski Hesap"}
    # geçersiz token'lı hesaba hatırlatma gitmez
    o.conn.execute("UPDATE hesaplar SET durum='token_gecersiz' WHERE hesap_id=?", (h1,))
    o.saat.ilerle(7 * 86400)
    n = len(bildirimler(o.conn, "token_hatirlatma"))
    o.z.token_hatirlat()
    assert all(x["veri"]["hesap_ad"] != "Eski Hesap" for x in bildirimler(o.conn, "token_hatirlatma")[n:])


def test_token_hatirlatma_taze_tokenlar_icin_yok(o):
    o.hesap("A1"), o.hesap("A2")
    o.saat.ilerle(6 * 86400)
    assert o.z.token_hatirlat() is False
    assert bildirimler(o.conn, "token_hatirlatma") == []
    o.saat.ilerle(2 * 86400)  # 8 gün: her iki hesap için bir hatırlatma
    assert o.z.token_hatirlat() is True
    assert sorted(x["veri"]["hesap_ad"] for x in bildirimler(o.conn, "token_hatirlatma")) == ["A1", "A2"]


def test_prev_politikasi_yalniz_24_saati_asan_yedegi_siler(o):
    h1, h2 = o.hesap("A1"), o.hesap("A2")
    for h in (h1, h2):
        assert o.run(o.hy.token_guncelle(h, T_YENI)).gecerli  # token.prev oluşur
    prev1 = o.tmp / "hesaplar" / str(h1) / "token.prev"
    prev2 = o.tmp / "hesaplar" / str(h2) / "token.prev"
    assert prev1.exists() and prev2.exists()
    simdi = o.saat().timestamp()
    os.utime(prev1, (simdi - 25 * 3600, simdi - 25 * 3600))
    os.utime(prev2, (simdi - 23 * 3600, simdi - 23 * 3600))
    assert o.z.prev_politikasi_uygula() is True
    assert not prev1.exists() and prev2.exists(), "yalnız 24 saati aşan silindi"
    assert (o.tmp / "hesaplar" / str(h1) / "token").read_text() == T_YENI, "ana token'a dokunulmadı"
    assert o.z.prev_politikasi_uygula() is False
    o.saat.ilerle(3 * 3600)  # 23 saatlik yedek 26 saate ulaştı
    assert o.z.prev_politikasi_uygula() is True and not prev2.exists()


# ================================================================= 11. sır sızıntısı yok
def test_hicbir_bildirim_log_ya_da_veritabani_token_veya_proxy_sirri_icermez(o, caplog):
    caplog.set_level(logging.DEBUG)
    o.oyuncu_ekle(301)
    h1 = o.hesap("A1", T_A)
    h2 = o.hesap("A2", T_B, PROXY_A)
    # reddedilen aday token: hesap oluşmaz, sızıntı yok
    hid, s = o.run(o.hy.hesap_olustur("A3", T_KOTU, PROXY_B))
    assert hid is None and not s.gecerli
    # hata metinleri sır içeriyor (kötü niyetli/özensiz kaynak): sistem bunları yansıtmamalı
    o.kaynak(h2).hatalar[("*", "*")] = ProxyErisilemez(f"cannot reach {PROXY_A} with {T_B} (pw Pr0xyS3cretPW)")
    o.kaynak(h1).hatalar[("oyuncu", "3")] = KaynakHatasi(f"boom {T_A} via {PROXY_A}")
    o.tick()
    assert o.h(h2)["durum"] == "proxy_hatasi" and o.h(h1)["durum"] in ("hazir", "hata")
    # token geçersiz geçişi + yenileme
    o.kaynak(h2).hatalar[("*", "*")] = TokenGecersiz(f"rejected {T_B}")
    o.saat.ilerle(901)
    o.sonraki_hazir()
    o.tick()
    assert o.h(h2)["durum"] == "token_gecersiz"
    assert o.run(o.hy.token_guncelle(h2, T_YENI)).gecerli
    o.run(o.hy.proxy_guncelle(h2, PROXY_B))
    o.kaynak(h2).hatalar.clear()
    o.kaynak(h1).hatalar.clear()
    # rate limit + 403
    o.kaynak(h1).hatalar[("oyuncu", "1")] = IstekSiniri(f"429 {T_A}", 90)
    o.ilerle_tick(o.aralik)
    o.kaynak(h1).hatalar[("oyuncu", "1")] = ErisimReddedildi(f"403 {PROXY_A}")
    o.saat.ilerle(2000)
    o.sonraki_hazir()
    o.tick()
    # kapasite uyarısı (301 oyuncu, 2 hesap) + token hatırlatması
    o.saat.ilerle(8 * 86400)
    o.z.token_hatirlat()
    o.tick()
    tur = {r["tur"] for r in o.conn.execute("SELECT tur FROM outbox")}
    assert {"hesap_durumu", "hesap_gerekli", "token_hatirlatma"} <= tur
    metinler = []
    for r in o.conn.execute("SELECT * FROM outbox ORDER BY id"):
        metinler.append(r["veri"])
        metinler.append(r["idem_anahtari"])
        metinler.append(bicim.bildirim_metni(r["tur"], json.loads(r["veri"])))
    metinler.append(caplog.text)
    metinler += [r.getMessage() for r in caplog.records]
    metinler.append("\n".join(o.conn.iterdump()))
    for r in o.conn.execute("SELECT * FROM hesaplar"):
        metinler.append(repr(tuple(r)))
    toplam = "\n".join(metinler)
    for sir in SIRLAR:
        assert sir not in toplam, f"sızıntı: {sir[:12]}..."
    # sırlar yalnız hesap dizinindeki dosyalarda
    assert (o.tmp / "hesaplar" / str(h1) / "token").read_text() == T_A
    assert o.hy.depo.izin_sorunlari() == []
