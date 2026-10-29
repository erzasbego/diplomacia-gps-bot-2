"""Çok hesaplı mimari çekirdeği: görev ataması (150 kapasite, sıralı doldurma), sır deposu (0600/0700), güvenli token/proxy güncelleme,
proxy'de doğrudan bağlantıya geçmeme. MOCK testler: sahte hesaplar/API yanıtları, ağ yok (yerel kapalı port hariç)."""
import asyncio
import json
import os
import socket
import sqlite3
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from conftest import run
from fakes import SahteKaynak, SahteSaat

from dipbot import config, db
from dipbot.diplomacia.dogrulayici import OyuncuDogrulayici
from dipbot.diplomacia.istemci import IstemciKaynagi, OrtamKimlik
from dipbot.hesaplar import (
    HESAP_AZAMI, HesapDeposu, HesapHatasi, HesapYoneticisi, ProxyAyar, ad_dogrula, proxy_ayristir, token_biçimi_dogrula,
)
from dipbot.kaynak import ProxyErisilemez, TokenGecersiz
from dipbot.tg.token import DogrulamaSonucu, TokenDeposu
from dipbot.zaman import iso

M = config.MERKEZ_ID
TOKEN1 = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0MSJ9.c2lnbmF0dXJlMQ"
TOKEN2 = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0MiJ9.c2lnbmF0dXJlMg"
PROXY = "http://proxyuser:S3cretPass@10.1.2.3:8080"


class SahteDogrulayici:
    """Aday token'ı + kullanılan proxy'yi KAYDEDER; sonuç tokena göre verilir."""

    def __init__(self, sonuclar, kayit):
        self.sonuclar, self.kayit = sonuclar, kayit

    async def dogrula(self, token):
        self.kayit.append((token, self.proxy))
        s = self.sonuclar.get(token)
        if callable(s):
            s = s()
        return s or DogrulamaSonucu("gecersiz", "the site rejected the token (401)")


def kur(tmp_path, saat=None, conn=None, **kw):
    conn = conn or db.baglan(":memory:")
    saat = saat or SahteSaat()
    kayit: list = []
    sonuclar = {TOKEN1: DogrulamaSonucu("gecerli", "ok"), TOKEN2: DogrulamaSonucu("gecerli", "ok")}

    def fab(proxy):
        d = SahteDogrulayici(sonuclar, kayit)
        d.proxy = proxy.url if proxy else None
        return d

    hy = HesapYoneticisi(conn, saat, tmp_path / "hesaplar", dogrulayici_fabrikasi=fab, **kw)
    hy.kayit, hy.sonuclar = kayit, sonuclar
    return hy, conn, saat


def oyuncular(conn, n, baslangic=1, saat=None):
    for i in range(baslangic, baslangic + n):
        conn.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi, baseline_tamam) VALUES(?,?,?,?,1)",
                     (M, str(i), f"P{i}", f"2026-10-01T10:{i // 60 % 60:02d}:{i % 60:02d}.{i:06d}Z"))


def sil(conn, ids):
    for i in ids:
        conn.execute("DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (M, str(i)))


def hesap(hy, ad, tmp=None):
    hid, s = run(hy.hesap_olustur(ad, TOKEN1, None))
    assert hid is not None, s
    return hid


def dagilim(hy):
    c = hy.oyuncu_sayilari()
    return [c.get(h["hesap_id"], 0) for h in hy.listele()], c.get(None, 0)


# ================================================================= 150'şer kapasiteli sıralı doldurma
def test_hesap_yokken_hicbir_hesap_olusturulmaz_oyuncular_beklemede(tmp_path):
    hy, c, _ = kur(tmp_path)
    oyuncular(c, 10)
    r = hy.dagit()
    assert r == {"atanan": 0, "bekleyen": 10, "hesap_sayisi": 0}
    assert hy.listele() == [], "hesaplar OTOMATİK oluşturulmaz"
    assert c.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 10, "oyuncu silinmedi"


@pytest.mark.parametrize(
    "n,hesap_sayisi,beklenen,bekleyen",
    [
        (100, 1, [100], 0),
        (205, 2, [150, 55], 0),
        (300, 2, [150, 150], 0),
        (301, 2, [150, 150], 1),
        (450, 3, [150, 150, 150], 0),
        (451, 3, [150, 150, 150], 1),
        (0, 2, [0, 0], 0),
        (1, 3, [1, 0, 0], 0),
        (150, 2, [150, 0], 0),
        (151, 2, [150, 1], 0),
    ],
)
def test_sirali_doldurma_kurali(tmp_path, n, hesap_sayisi, beklenen, bekleyen):
    hy, c, _ = kur(tmp_path)
    for i in range(hesap_sayisi):
        hesap(hy, f"Account {i + 1}")
    oyuncular(c, n)
    hy.dagit()
    assert dagilim(hy) == (beklenen, bekleyen)
    assert hy.kapasite == 150


def test_301_oyuncuda_ucuncu_hesap_eklenince_150_150_1(tmp_path):
    hy, c, _ = kur(tmp_path)
    hesap(hy, "A1"); hesap(hy, "A2")
    oyuncular(c, 301)
    hy.dagit()
    assert dagilim(hy) == ([150, 150], 1)
    k = hy.kapasite_ozeti()
    assert k["bekleyen"] == 1 and k["hesap_sayisi"] == 2
    hesap(hy, "A3")  # kurucu üçüncü hesabı AÇIKÇA ekler -> bekleyen otomatik atanır
    assert dagilim(hy) == ([150, 150, 1], 0)


def test_kapasite_dolunca_oyuncu_silinmez_beklemede_kalir_ve_yeniden_baslatmada_korunur(tmp_path):
    yol = tmp_path / "d.db"
    c = db.baglan(yol)
    hy, _, _ = kur(tmp_path, conn=c)
    hesap(hy, "A1")
    oyuncular(c, 151)
    hy.dagit()
    c.close()
    c2 = db.baglan(yol)  # yeniden başlatma
    hy2, _, _ = kur(tmp_path, conn=c2)
    assert dagilim(hy2) == ([150], 1)
    bekleyen = [r[0] for r in c2.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id IS NULL")]
    assert len(bekleyen) == 1 and c2.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 151


def test_oyuncu_silinince_bos_yer_sirayla_doldurulur_en_az_tasima(tmp_path):
    hy, c, _ = kur(tmp_path)
    hesap(hy, "A1"); hesap(hy, "A2")
    oyuncular(c, 300)
    hy.dagit()
    once = {r[0]: r[1] for r in c.execute("SELECT oyuncu_id, hesap_id FROM oyuncu_atamalari")}
    sil(c, range(1, 101))  # hesap 1'den 100 oyuncu silinir
    hy.dagit()
    assert dagilim(hy) == ([150, 50], 0), "kanonik sıralı doldurma geri geldi (150/50)"
    sonra = {r[0]: r[1] for r in c.execute("SELECT oyuncu_id, hesap_id FROM oyuncu_atamalari")}
    tasinan = [o for o in sonra if once[o] != sonra[o]]
    assert len(tasinan) == 100, "yalnız boşalan yer kadar oyuncu taşındı (hesap 2 -> 1)"
    assert all(once[o] == 2 and sonra[o] == 1 for o in tasinan)


def test_oyuncu_eklenince_mevcut_atamalar_degismez_yeni_oyuncu_ilk_bos_yere(tmp_path):
    hy, c, _ = kur(tmp_path)
    hesap(hy, "A1"); hesap(hy, "A2")
    oyuncular(c, 205)
    hy.dagit()
    once = {r[0]: r[1] for r in c.execute("SELECT oyuncu_id, hesap_id FROM oyuncu_atamalari")}
    oyuncular(c, 10, baslangic=1000)
    hy.dagit()
    sonra = {r[0]: r[1] for r in c.execute("SELECT oyuncu_id, hesap_id FROM oyuncu_atamalari")}
    assert all(sonra[o] == once[o] for o in once), "mevcut görevler kıpırdamadı"
    assert dagilim(hy) == ([150, 65], 0)


def test_dagit_idempotent_ve_hesap_secimi_yok(tmp_path):
    hy, c, _ = kur(tmp_path)
    hesap(hy, "A1"); hesap(hy, "A2")
    oyuncular(c, 205)
    hy.dagit()
    ilk = [tuple(r) for r in c.execute("SELECT oyuncu_id, hesap_id FROM oyuncu_atamalari ORDER BY oyuncu_id")]
    for _ in range(3):
        hy.dagit()
    assert [tuple(r) for r in c.execute("SELECT oyuncu_id, hesap_id FROM oyuncu_atamalari ORDER BY oyuncu_id")] == ilk


def test_atama_degisikligi_oyuncu_gecmisini_sifirlamaz(tmp_path):
    hy, c, _ = kur(tmp_path)
    hesap(hy, "A1"); hesap(hy, "A2")
    oyuncular(c, 300)
    c.execute("UPDATE takip_oyuncular SET bolge='Varna', vatandaslik_ulkesi='BG', son_gozlem='2026-10-02T00:00:00.000000Z', uid='u'")
    hy.dagit()
    once = [tuple(r) for r in c.execute("SELECT * FROM takip_oyuncular ORDER BY oyuncu_id")]
    sil(c, range(1, 51))
    once = [r for r in once if int(r[1]) > 50]
    hy.dagit()  # taşıma oldu
    sonra = [tuple(r) for r in c.execute("SELECT * FROM takip_oyuncular ORDER BY oyuncu_id")]
    assert sonra == once, "bölge/vatandaşlık/son gözlem/baseline AYNEN korundu"


def test_atanmis_hesap_kaydi_kaybolursa_oyuncu_beklemeye_doner(tmp_path):
    hy, c, _ = kur(tmp_path)
    oyuncular(c, 3)
    c.execute("INSERT INTO oyuncu_atamalari(oyuncu_id, hesap_id) VALUES('1', 99)")
    hy.dagit()
    assert dagilim(hy) == ([], 3)


def test_kapasite_parametresi_kucultulurse_en_son_eklenenler_beklemeye(tmp_path):
    hy, c, _ = kur(tmp_path, kapasite=5)
    hesap(hy, "A1")
    oyuncular(c, 8)
    hy.dagit()
    assert dagilim(hy) == ([5], 3)
    hy.kapasite = 3
    hy.dagit()
    assert dagilim(hy) == ([3], 5)
    kalan = {r[0] for r in c.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id IS NOT NULL")}
    assert kalan == {"1", "2", "3"}


# ================================================================= oyuncu ekranı sorguları
def test_oyuncu_sayfasi_yalniz_o_hesabin_oyuncularini_sayfali_ve_aramali_verir(tmp_path):
    hy, c, _ = kur(tmp_path)
    h1, h2 = hesap(hy, "A1"), hesap(hy, "A2")
    oyuncular(c, 205)
    hy.dagit()
    s1, t1 = hy.oyuncu_sayfasi(h1, 0, 8)
    s2, t2 = hy.oyuncu_sayfasi(h2, 0, 8)
    assert t1 == 150 and t2 == 55 and len(s1) == 8 and len(s2) == 8
    ids1 = {r["oyuncu_id"] for r in hy.oyuncu_sayfasi(h1, 0, 1000)[0]}
    ids2 = {r["oyuncu_id"] for r in hy.oyuncu_sayfasi(h2, 0, 1000)[0]}
    assert not ids1 & ids2 and len(ids1) == 150 and len(ids2) == 55
    son, _ = hy.oyuncu_sayfasi(h2, 6, 8)
    assert len(son) == 55 - 48
    ara, n = hy.oyuncu_sayfasi(h1, 0, 8, "P10")
    assert [r["oyuncu_id"] for r in ara][:1] == ["10"] or n >= 1
    ara_id, n = hy.oyuncu_sayfasi(h1, 0, 50, "10")
    assert {r["oyuncu_id"] for r in ara_id} <= ids1 and all(r["oyuncu_id"].startswith("10") or "10" in (r["ad"] or "").lower() for r in ara_id)
    ara_ad, n = hy.oyuncu_sayfasi(h2, 0, 50, "p20")  # adın içinde, büyük/küçük harf duyarsız; yalnız bu hesabınkiler
    assert n >= 1 and {r["oyuncu_id"] for r in ara_ad} <= ids2
    assert hy.oyuncu_sayfasi(h1, 0, 8, "%")[1] == 0, "joker karakterleri kaçırılır"


def test_istatistik_ve_oyuncu_sonucu(tmp_path):
    hy, c, saat = kur(tmp_path)
    h1 = hesap(hy, "A1")
    oyuncular(c, 10)
    hy.dagit()
    for i in range(1, 7):
        hy.oyuncu_sonucu(str(i), "ok")
    hy.oyuncu_sonucu("7", "hata", "network timeout")
    st = hy.istatistik(h1)
    assert (st["atanan"], st["basarili"], st["bekleyen_gorev"], st["hatali"]) == (10, 6, 4, 1)
    saat.ilerle(3 * 3600)  # eski başarılar "son 2 aralık" dışında kalır
    assert hy.istatistik(h1)["basarili"] == 0


# ================================================================= sır depolama
def _izin(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_sirlar_yalniz_0600_dosyalarda_dizinler_0700(tmp_path):
    hy, c, _ = kur(tmp_path)
    old = os.umask(0o022)  # kötü umask'e rağmen izinler zorlanır
    try:
        hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
        run(hy.token_guncelle(hid, TOKEN2))  # .prev de oluşur
    finally:
        os.umask(old)
    kok = tmp_path / "hesaplar"
    assert _izin(kok) == 0o700 and _izin(kok / str(hid)) == 0o700
    dosyalar = sorted(p.name for p in (kok / str(hid)).iterdir())
    assert dosyalar == ["proxy", "token", "token.prev"]
    for p in (kok / str(hid)).iterdir():
        assert _izin(p) == 0o600, p.name
    assert hy.depo.izin_sorunlari() == []
    os.chmod(kok / str(hid) / "token", 0o644)
    assert any("token" in x for x in hy.depo.izin_sorunlari())


def test_veritabaninda_ve_diskte_veri_dizini_disinda_sir_yok(tmp_path):
    yol = tmp_path / "d.db"
    c = db.baglan(yol)
    hy, _, _ = kur(tmp_path, conn=c)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ham = yol.read_bytes()
    for sir in (TOKEN1.encode(), b"S3cretPass", b"proxyuser", b"10.1.2.3"):
        assert sir not in ham, sir
    for r in c.execute("SELECT * FROM hesaplar"):
        assert not any(isinstance(v, str) and (TOKEN1 in v or "S3cretPass" in v or "10.1.2.3" in v) for v in tuple(r))
    kayit = c.execute("SELECT proxy_var, proxy_sema FROM hesaplar").fetchone()
    assert (kayit[0], kayit[1]) == (1, "http"), "DB'de yalnız 'proxy var' ve şema"
    assert TOKEN1 in (tmp_path / "hesaplar" / str(hid) / "token").read_text()


def test_sirlar_loglara_hata_metinlerine_repr_e_sizmaz(tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    hy, c, _ = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    for e in (HesapHatasi("x"),):
        pass
    ayar = proxy_ayristir(PROXY)
    assert "S3cretPass" not in repr(ayar) and "10.1.2.3" not in repr(ayar) and "proxyuser" not in repr(ayar)
    for bozuk in ("ftp://u:S3cretPass@10.1.2.3:21", "http://u:S3cretPass@10.1.2.3", "u:S3cretPass@10.1.2.3:8080", "http://10.1.2.3:8080/yol?x=S3cretPass"):
        with pytest.raises(HesapHatasi) as e:
            proxy_ayristir(bozuk)
        assert "S3cretPass" not in str(e.value) and "10.1.2.3" not in str(e.value)
    with pytest.raises(HesapHatasi) as e:
        token_biçimi_dogrula("bu bir token degil")
    assert "token degil" not in str(e.value)
    toplam = "\n".join(r.getMessage() for r in caplog.records)
    for sir in (TOKEN1, "S3cretPass", "10.1.2.3", "proxyuser"):
        assert sir not in toplam


# ================================================================= doğrulayıcılar
@pytest.mark.parametrize("ad", ["Main", "Account 2", "a-b_c.d", "X" * 32])
def test_ad_dogrula_gecerli(ad):
    assert ad_dogrula(ad) == ad


@pytest.mark.parametrize("ad", ["", " ", "-x", "a<b>", "x" * 33, "<script>", "ö"])
def test_ad_dogrula_gecersiz(ad):
    with pytest.raises(HesapHatasi):
        ad_dogrula(ad)


def test_proxy_ayristir_kurallari():
    a = proxy_ayristir("http://u:p%40ss@example.com:3128")
    assert a.sema == "http" and a.url == "http://u:p%40ss@example.com:3128"
    assert proxy_ayristir("https://example.com:443").sema == "https"
    assert proxy_ayristir("http://[::1]:8080").url == "http://[::1]:8080"
    assert proxy_ayristir("http://1.2.3.4:80").egress == proxy_ayristir("http://x:y@1.2.3.4:80").egress != proxy_ayristir("http://1.2.3.4:81").egress
    for kotu in ("socks5://h:1080", "h:8080", "http://h", "http://h:0", "http://h:99999", "http://h:8080/x", "http://h:8080?a=b", "", "http://h :8080"):
        with pytest.raises(HesapHatasi):
            proxy_ayristir(kotu)


# ================================================================= token/proxy güncelleme: ÖNCE sına, başarısızsa koru
def test_hesap_olustur_once_dogrular_basarisizsa_hicbir_sey_olusmaz(tmp_path):
    hy, c, _ = kur(tmp_path)
    kotu = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJiYWQifQ.YmFkc2lnbmF0dXJl"
    hid, s = run(hy.hesap_olustur("Bad", kotu, PROXY))
    assert hid is None and not s.gecerli
    assert hy.listele() == [] and not (tmp_path / "hesaplar").exists()
    assert hy.kayit[-1] == (kotu, PROXY), "aday token ADAY proxy üzerinden sınandı"


def test_hesap_olustur_proxy_ile_dogrulama_proxy_uzerinden_yapilir_ve_dogrudan_denenmez(tmp_path):
    hy, c, _ = kur(tmp_path)
    hid, s = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    assert hid == 1 and s.gecerli
    assert [k[1] for k in hy.kayit] == [PROXY]
    h = hy.getir(hid)
    assert (h["proxy_var"], h["durum"], h["proxy_durum"]) == (1, "hazir", "calisiyor")
    assert hy.depo.proxy_oku(hid).url == PROXY


def test_ayni_ad_ve_azami_hesap_sayisi(tmp_path):
    hy, c, _ = kur(tmp_path)
    hesap(hy, "Main")
    with pytest.raises(HesapHatasi):
        run(hy.hesap_olustur("main", TOKEN1, None))
    for i in range(HESAP_AZAMI - 1):
        hesap(hy, f"N{i}")
    with pytest.raises(HesapHatasi):
        run(hy.hesap_olustur("Extra", TOKEN1, None))


def test_token_guncelle_basarisizsa_onceki_gecerli_token_ve_durum_korunur(tmp_path):
    hy, c, saat = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    c.execute("UPDATE hesaplar SET durum='token_gecersiz' WHERE hesap_id=?", (hid,))
    dosya = tmp_path / "hesaplar" / str(hid) / "token"
    kotu = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJiYWQifQ.YmFkc2lnbmF0dXJl"
    s = run(hy.token_guncelle(hid, kotu))
    assert not s.gecerli and dosya.read_text() == TOKEN1 and not dosya.with_name("token.prev").exists()
    assert hy.getir(hid)["durum"] == "token_gecersiz", "başarısız denemede durum değişmez"
    assert hy.kayit[-1] == (kotu, PROXY), "doğrulama hesabın proxy'si üzerinden"
    s = run(hy.token_guncelle(hid, TOKEN2))
    assert s.gecerli and dosya.read_text() == TOKEN2 and dosya.with_name("token.prev").read_text() == TOKEN1
    h = hy.getir(hid)
    assert h["durum"] == "hazir" and h["token_guncelleme"] and h["ardisik_hata"] == 0 and h["sonraki_tarama"] is None


def test_token_guncelle_dogrulama_hatasi_veya_zaman_asiminda_dosya_degismez(tmp_path):
    hy, c, _ = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, None))
    hy.sonuclar[TOKEN2] = DogrulamaSonucu("hata", "could not reach the site for verification")
    s = run(hy.token_guncelle(hid, TOKEN2))
    assert s.durum == "hata" and (tmp_path / "hesaplar" / str(hid) / "token").read_text() == TOKEN1
    hy.sonuclar[TOKEN2] = DogrulamaSonucu("proxy_hatasi", "could not connect through the configured proxy")
    s = run(hy.token_guncelle(hid, TOKEN2))
    assert s.durum == "proxy_hatasi" and (tmp_path / "hesaplar" / str(hid) / "token").read_text() == TOKEN1


def test_proxy_guncelle_token_ile_sinar_basarisizsa_eski_proxy_korunur(tmp_path):
    hy, c, _ = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    yeni = "http://u2:p2@10.9.9.9:3128"
    hy.sonuclar[TOKEN1] = DogrulamaSonucu("proxy_hatasi", "could not connect through the configured proxy (no direct connection was attempted)")
    s = run(hy.proxy_guncelle(hid, yeni))
    assert not s.gecerli and hy.depo.proxy_oku(hid).url == PROXY, "eski proxy aynen kaldı"
    assert hy.kayit[-1] == (TOKEN1, yeni), "aday proxy hesabın MEVCUT token'ıyla sınandı"
    hy.sonuclar[TOKEN1] = DogrulamaSonucu("gecerli", "ok")
    assert run(hy.proxy_guncelle(hid, yeni)).gecerli
    assert hy.depo.proxy_oku(hid).url == yeni and hy.getir(hid)["proxy_durum"] == "calisiyor"
    with pytest.raises(HesapHatasi):
        run(hy.proxy_guncelle(hid, "socks5://x:1"))


def test_proxy_kaldir_acik_islem_ve_token_yokken_proxy_dogrulanamaz(tmp_path):
    hy, c, _ = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    run(hy.proxy_kaldir(hid))
    h = hy.getir(hid)
    assert (h["proxy_var"], h["proxy_sema"], h["proxy_durum"]) == (0, None, "yok") and hy.depo.proxy_oku(hid) is None
    os.unlink(tmp_path / "hesaplar" / str(hid) / "token")
    with pytest.raises(HesapHatasi, match="no token"):
        run(hy.proxy_guncelle(hid, PROXY))


def test_baglanti_testi_durumu_isler(tmp_path):
    hy, c, _ = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    hy.sonuclar[TOKEN1] = DogrulamaSonucu("proxy_hatasi", "could not connect through the configured proxy (no direct connection was attempted)")
    s = run(hy.baglanti_testi(hid))
    h = hy.getir(hid)
    assert s.durum == "proxy_hatasi" and h["durum"] == "proxy_hatasi" and h["proxy_durum"] == "hata"
    hy.sonuclar[TOKEN1] = DogrulamaSonucu("gecersiz", "the site rejected the token (401)")
    run(hy.baglanti_testi(hid))
    assert hy.getir(hid)["durum"] == "token_gecersiz"
    hy.sonuclar[TOKEN1] = DogrulamaSonucu("gecerli", "ok")
    run(hy.baglanti_testi(hid))
    h = hy.getir(hid)
    assert h["durum"] == "hazir" and h["proxy_durum"] == "calisiyor" and h["son_api_basari"]


def test_eski_token_ice_aktarma_yalniz_hesap_yokken_ve_dosya_kopyalanir(tmp_path):
    hy, c, _ = kur(tmp_path)
    eski = tmp_path / "diplomacia.token"
    TokenDeposu(eski).yaz(TOKEN1)
    hid, s = run(hy.eski_token_ice_aktar(eski))
    assert hid == 1 and hy.getir(1)["ad"] == "Account 1" and eski.read_text() == TOKEN1, "eski dosya yerinde kaldı"
    with pytest.raises(HesapHatasi):
        run(hy.eski_token_ice_aktar(eski))


# ================================================================= bekleme/cooldown (kalıcı, çıkış noktası paylaşımı)
def test_cooldown_kalici_ve_ayni_cikis_noktasini_paylasan_hesaplari_etkiler(tmp_path):
    hy, c, saat = kur(tmp_path)
    a = hesap(hy, "A")                                   # doğrudan
    b, _ = run(hy.hesap_olustur("B", TOKEN1, PROXY))     # proxy 1
    d = hesap(hy, "D")                                   # doğrudan (A ile aynı IP)
    hy.cooldown_ayarla(a, 120, rate_limit=True)
    assert hy.cooldown_kalan(a) == 120 and hy.cooldown_kalan(d) == 120, "aynı çıkış noktası: ortak kota korunur"
    assert hy.cooldown_kalan(b) == 0, "farklı çıkış noktası etkilenmez"
    saat.ilerle(121)
    assert hy.cooldown_kalan(a) == 0 and hy.cooldown_kalan(d) == 0
    assert hy.getir(a)["son_rate_limit"]


# ================================================================= proxy: doğrudan bağlantıya ASLA geçilmez (gerçek httpx)
class _Sayici(BaseHTTPRequestHandler):
    istekler = 0

    def do_GET(self):
        _Sayici.istekler += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def _bos_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_proxy_kapaliyken_ProxyErisilemez_ve_site_sunucusuna_DOGRUDAN_istek_gitmez(tmp_path):
    _Sayici.istekler = 0
    sunucu = HTTPServer(("127.0.0.1", 0), _Sayici)
    t = threading.Thread(target=sunucu.serve_forever, daemon=True)
    t.start()
    try:
        taban = f"http://127.0.0.1:{sunucu.server_port}"
        TokenDeposu(tmp_path / "t").yaz(TOKEN1)
        kimlik = OrtamKimlik(TokenDeposu(tmp_path / "t"))
        k = IstemciKaynagi(kimlik, taban=taban, proxy=f"http://u:p@127.0.0.1:{_bos_port()}", zaman_asimi=3)
        with pytest.raises(ProxyErisilemez) as e:
            run(k.fetch_player("1"))
        assert "u:p" not in str(e.value) and "127.0.0.1" not in str(e.value)
        run(k.aclose())
        # doğrulayıcı da proxy'siz denemez
        d = OyuncuDogrulayici(kimlik, taban=taban, proxy=f"http://u:p@127.0.0.1:{_bos_port()}", zaman_asimi=3)
        s = run(d.dogrula(TOKEN1))
        assert s.durum == "proxy_hatasi" and "no direct connection" in s.aciklama
        assert _Sayici.istekler == 0, "proxy kapalıyken site sunucusuna hiç DOĞRUDAN istek gitmedi"
        # kontrol: proxy'siz istemci aynı sunucuya erişebilir (testin geçerliliği)
        k2 = IstemciKaynagi(kimlik, taban=taban, zaman_asimi=3)
        with pytest.raises(Exception):
            run(k2.fetch_player("1"))  # {} gövde ayrıştırılamaz ama İSTEK sunucuya ulaşır
        run(k2.aclose())
        assert _Sayici.istekler == 1
    finally:
        sunucu.shutdown()


@pytest.mark.parametrize("hata,beklenen", [(httpx.ProxyError("407"), ProxyErisilemez), (httpx.ConnectError("x"), ProxyErisilemez),
                                          (httpx.ConnectTimeout("x"), ProxyErisilemez)])
def test_proxyli_istemci_baglanti_hatalari_proxy_hatasi_sayilir(tmp_path, hata, beklenen):
    TokenDeposu(tmp_path / "t").yaz(TOKEN1)

    def isle(request):
        raise hata

    k = IstemciKaynagi(OrtamKimlik(TokenDeposu(tmp_path / "t")), transport=httpx.MockTransport(isle), proxy="http://u:p@h:1")
    with pytest.raises(beklenen):
        run(k.fetch_player("1"))
    run(k.aclose())


def test_proxysiz_istemcide_baglanti_hatasi_proxy_hatasi_sayilmaz(tmp_path):
    from dipbot.kaynak import SiteErisilemez

    TokenDeposu(tmp_path / "t").yaz(TOKEN1)

    def isle(request):
        raise httpx.ConnectError("x")

    k = IstemciKaynagi(OrtamKimlik(TokenDeposu(tmp_path / "t")), transport=httpx.MockTransport(isle))
    with pytest.raises(SiteErisilemez):
        run(k.fetch_player("1"))
    run(k.aclose())


def test_okunamayan_proxy_dosyasi_dogrudan_baglantiya_dusmez(tmp_path):
    hy, c, _ = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    (tmp_path / "hesaplar" / str(hid) / "proxy").write_text("bozuk")
    with pytest.raises(ProxyErisilemez):
        hy.kaynak(hid)
    s = run(hy.baglanti_testi(hid))
    assert s.durum == "proxy_hatasi"


# ================================================================= hız/kota: genel + hesap eşzamanlılığı ve pacing
def test_genel_sinir_tum_hesaplarin_toplam_eszamanli_istegini_sinirlar(tmp_path):
    from dipbot.hesaplar import GenelSinir

    aktif = {"n": 0, "azami": 0}

    class Ic:
        yapilandirildi = True
        token_hazir = True

        async def fetch_player(self, oid):
            aktif["n"] += 1
            aktif["azami"] = max(aktif["azami"], aktif["n"])
            await asyncio.sleep(0.02)
            aktif["n"] -= 1
            return oid

    genel = GenelSinir(esler=2, aralik=0)
    hy, c, _ = kur(tmp_path, genel_sinir=genel, hesap_aralik=0, istemci_fabrikasi=lambda hid, proxy, depo: Ic())
    ids = [hesap(hy, f"A{i}") for i in range(4)]

    async def hepsi():
        await asyncio.gather(*[hy.kaynak(h).fetch_player(str(i)) for h in ids for i in range(5)])

    run(hepsi())
    assert aktif["azami"] <= 2, "4 hesap eklemek genel eşzamanlı kotayı artırmadı"


def test_hesap_pacing_istekler_arasi_en_az_aralik(tmp_path):
    import time

    ts = []

    class Ic:
        yapilandirildi = True
        token_hazir = True

        async def fetch_player(self, oid):
            ts.append(time.monotonic())
            return oid

    from dipbot.hesaplar import GenelSinir

    hy, c, _ = kur(tmp_path, genel_sinir=GenelSinir(esler=4, aralik=0), hesap_aralik=0.05, istemci_fabrikasi=lambda *a: Ic())
    h = hesap(hy, "A")
    async def dort():
        await asyncio.gather(*[hy.kaynak(h).fetch_player(str(i)) for i in range(4)])

    run(dort())
    farklar = [b - a for a, b in zip(ts, ts[1:])]
    assert all(f >= 0.045 for f in farklar), farklar


def test_token_gecersiz_kusagi_hata_uzerinde_tasinir(tmp_path):
    class Ic:
        yapilandirildi = True
        token_hazir = True

        async def fetch_player(self, oid):
            raise TokenGecersiz("x")

    hy, c, _ = kur(tmp_path, hesap_aralik=0, istemci_fabrikasi=lambda *a: Ic())
    h = hesap(hy, "A")
    with pytest.raises(TokenGecersiz) as e:
        run(hy.kaynak(h).fetch_player("1"))
    assert e.value.kusak == 0
    run(hy.kaynagi_yenile(h))
    with pytest.raises(TokenGecersiz) as e2:
        run(hy.kaynak(h).fetch_player("1"))
    assert e2.value.kusak == 1, "token yenilenince kuşak artar: uçuştaki eski 401 yenisini duraklatmaz"


# ================================================================= regresyon: kaynak kuşağı, emekli istemci, birlikte güncelleme, ad tekilliği, açılış temizliği
PROXY2 = "http://proxy2user:Pass2Secret@10.9.8.7:3128"


def zamanli(coro, sn=5):
    """Kilitlenme (deadlock) olursa test askıda kalmasın."""
    return run(asyncio.wait_for(coro, sn))


def test_sinirli_kaynak_hatada_onceden_atanmis_kusagi_ezmez():
    from dipbot.kaynak import SinirliKaynak

    class Ic:
        def __init__(self, kusak):
            self.kusak = kusak

        async def fetch_player(self, oid):
            e = TokenGecersiz("x")
            if self.kusak is not ...:
                e.kusak = self.kusak
            raise e

    for onceki in (3, 0):  # iç (hesap) kaynağının kuşağı: kuşak_fn yokken EZİLMEZ
        with pytest.raises(TokenGecersiz) as e:
            run(SinirliKaynak(Ic(onceki)).fetch_player("1"))
        assert e.value.kusak == onceki
    with pytest.raises(TokenGecersiz) as e:  # hiç kuşak yoksa None olarak işaretlenir (öznitelik her zaman var)
        run(SinirliKaynak(Ic(...)).fetch_player("1"))
    assert e.value.kusak is None


def test_emekli_istemci_ucustaki_istek_varken_kapanmaz_bosalinca_emekli_temizle_kapatir(tmp_path):
    from dipbot.hesaplar import GenelSinir

    ics = []
    kapi, baslandi = asyncio.Event(), asyncio.Event()

    class Ic:
        yapilandirildi = True
        token_hazir = True

        def __init__(self):
            self.kapatildi = 0

        async def fetch_player(self, oid):
            baslandi.set()
            await kapi.wait()
            return oid

        async def aclose(self):
            self.kapatildi += 1

    def fab(hid, proxy, depo):
        ics.append(Ic())
        return ics[-1]

    hy, c, _ = kur(tmp_path, hesap_aralik=0, genel_sinir=GenelSinir(esler=4, aralik=0), istemci_fabrikasi=fab)
    hid = hesap(hy, "A")

    async def senaryo():
        eski = hy.kaynak(hid)
        gorev = asyncio.ensure_future(eski.fetch_player("1"))
        await baslandi.wait()
        assert eski.aktif_istek == 1
        await hy.kaynagi_yenile(hid)
        assert hy.kaynak(hid) is not eski and len(ics) == 2
        assert ics[0].kapatildi == 0, "uçuşta istek varken emekli istemci kapatılmaz"
        await hy.emekli_temizle()
        assert ics[0].kapatildi == 0
        kapi.set()
        assert await gorev == "1" and eski.aktif_istek == 0
        await hy.emekli_temizle()
        assert ics[0].kapatildi == 1 and ics[1].kapatildi == 0 and hy._emekli == []
        await hy.emekli_temizle()
        assert ics[0].kapatildi == 1, "ikinci temizlik tekrar kapatmaz"

    zamanli(senaryo())


def _birlikte_kur(tmp_path):
    hy, c, saat = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, PROXY))
    return hy, c, hid


def test_birlikte_guncelle_ikisi_de_yazilmadan_once_sinanir_basarida_ikisi_de_degisir(tmp_path):
    hy, c, hid = _birlikte_kur(tmp_path)
    hy.durum_yaz(hid, "token_gecersiz", "token rejected (401)")
    gecisler = []
    hy.durum_geri_cagrisi = lambda *a: gecisler.append(a)
    gorulen = {}

    def sonuc():  # doğrulama anında dosyalar HÂLÂ eski
        gorulen["token"], gorulen["proxy"] = hy.depo.token(hid).oku(), hy.depo.proxy_oku(hid).url
        return DogrulamaSonucu("gecerli", "ok")

    hy.sonuclar[TOKEN2] = sonuc
    s = zamanli(hy.birlikte_guncelle(hid, TOKEN2, PROXY2))
    assert s.gecerli and gorulen == {"token": TOKEN1, "proxy": PROXY}
    assert hy.kayit[-1] == (TOKEN2, proxy_ayristir(PROXY2).url), "aday token ADAY proxy üzerinden sınandı"
    assert hy.depo.token(hid).oku() == TOKEN2 and hy.depo.proxy_oku(hid).url == proxy_ayristir(PROXY2).url
    h = hy.getir(hid)
    assert (h["durum"], h["proxy_var"], h["proxy_durum"], h["cooldown_bitis"]) == ("hazir", 1, "calisiyor", None)
    assert gecisler == [(hid, "token_gecersiz", "hazir", None)]
    assert hy._token_kusaklari[hid] == 1
    assert hy.depo.izin_sorunlari() == []


@pytest.mark.parametrize("sonuc", [DogrulamaSonucu("gecersiz", "401"), DogrulamaSonucu("proxy_hatasi", "no proxy")])
def test_birlikte_guncelle_dogrulama_basarisizsa_eski_token_ve_eski_proxy_korunur(tmp_path, sonuc):
    hy, c, hid = _birlikte_kur(tmp_path)
    hy.sonuclar[TOKEN2] = sonuc
    once = (tuple(hy.getir(hid)), (tmp_path / "hesaplar" / str(hid) / "proxy").read_bytes())
    s = zamanli(hy.birlikte_guncelle(hid, TOKEN2, PROXY2))
    assert not s.gecerli
    assert hy.depo.token(hid).oku() == TOKEN1 and hy.depo.proxy_oku(hid).url == proxy_ayristir(PROXY).url
    assert (tuple(hy.getir(hid)), (tmp_path / "hesaplar" / str(hid) / "proxy").read_bytes()) == once
    assert not hy.depo.token(hid).yedek_yolu.exists() and hy._token_kusaklari.get(hid, 0) == 0


@pytest.mark.parametrize("eski_proxy", [PROXY, None])
def test_birlikte_guncelle_token_yazimi_patlarsa_eski_proxy_geri_konur_ve_kilitlenme_yok(tmp_path, monkeypatch, eski_proxy):
    hy, c, _ = kur(tmp_path)
    hid, _ = run(hy.hesap_olustur("Main", TOKEN1, eski_proxy))
    yol = tmp_path / "hesaplar" / str(hid) / "proxy"
    eski_bayt = yol.read_bytes() if eski_proxy else None
    once = tuple(hy.getir(hid))

    def boom(self, token):
        raise OSError("disk full")

    monkeypatch.setattr(TokenDeposu, "yaz", boom)
    with pytest.raises(OSError):
        zamanli(hy.birlikte_guncelle(hid, TOKEN2, PROXY2))
    monkeypatch.undo()
    assert (yol.read_bytes() if yol.exists() else None) == eski_bayt, "eski proxy aynen geri konur (yoksa yeni proxy kalmaz)"
    assert hy.depo.token(hid).oku() == TOKEN1 and tuple(hy.getir(hid)) == once
    assert not list(yol.parent.glob(".token-*")), "geçici dosya kalmadı"
    # kilit/durum bozulmadı: sonraki güncelleme normal çalışır
    assert zamanli(hy.birlikte_guncelle(hid, TOKEN2, PROXY2)).gecerli and hy.depo.token(hid).oku() == TOKEN2


def test_hesap_adi_buyuk_kucuk_harf_duyarsiz_tekil_ikinci_olusturma_temiz_basarisiz(tmp_path):
    hy, c, _ = kur(tmp_path)
    hid = hesap(hy, "main")
    dogrulama_sayisi = len(hy.kayit)
    dizinler = sorted(p.name for p in (tmp_path / "hesaplar").iterdir())
    for ad in ("main", "MAIN", "Main"):
        with pytest.raises(HesapHatasi, match="already exists"):
            run(hy.hesap_olustur(ad, TOKEN2, PROXY))
    assert [r["ad"] for r in hy.listele()] == ["main"] and hid == 1
    assert sorted(p.name for p in (tmp_path / "hesaplar").iterdir()) == dizinler == ["1"], "dizin/dosya oluşmadı"
    assert len(hy.kayit) == dogrulama_sayisi, "ağ doğrulaması bile yapılmadı"
    assert hy.depo.token(hid).oku() == TOKEN1 and hy.depo.proxy_oku(hid) is None
    # veritabanı da tek başına tekilliği zorlar (ux_hesap_ad)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO hesaplar(ad, olusturuldu) VALUES('MaIn', 'x')")


def test_acilis_temizligi_hesap_dizinlerindeki_eski_gecici_dosyalari_siler(tmp_path):
    hy, c, _ = kur(tmp_path)
    a = hesap(hy, "A")
    b, _ = run(hy.hesap_olustur("B", TOKEN2, PROXY))
    for hid, adlar in ((a, [".token-aaa111"]), (b, [".token-bbb222", ".token-ccc333"])):
        for ad in adlar:
            (tmp_path / "hesaplar" / str(hid) / ad).write_text("yarim-token-veya-proxy")
    (tmp_path / "hesaplar" / str(a) / "baska.txt").write_text("kalmali")
    assert hy.acilis_temizligi() == 3
    for hid in (a, b):
        assert not list((tmp_path / "hesaplar" / str(hid)).glob(".token-*"))
    assert (tmp_path / "hesaplar" / str(a) / "baska.txt").exists()
    assert hy.depo.token(a).oku() == TOKEN1 and hy.depo.token(b).oku() == TOKEN2 and hy.depo.proxy_oku(b).url == PROXY
    assert hy.acilis_temizligi() == 0, "idempotent"


# ================================================================= kompaktlama yalnız sağlıklı (hazir) hesaplar arasında
def _atamalar(c):
    return {r[0]: r[1] for r in c.execute("SELECT oyuncu_id, hesap_id FROM oyuncu_atamalari")}


@pytest.mark.parametrize("sorunlu_durum", ["token_gecersiz", "proxy_hatasi", "erisim_reddi", "rate_limit", "hata", "token_yok"])
@pytest.mark.parametrize("sorunlu", [1, 2, 3])
def test_kompaktlama_sorunlu_hesaptan_veya_hesaba_oyuncu_tasimaz(tmp_path, sorunlu, sorunlu_durum):
    hy, c, _ = kur(tmp_path, kapasite=3)
    ids = [hesap(hy, f"A{i}") for i in (1, 2, 3)]
    oyuncular(c, 9)
    hy.dagit()
    assert dagilim(hy) == ([3, 3, 3], 0)
    # hesap 1'den iki oyuncu çıkar -> 1. hesapta 2 boş yer; sağlıklı olanlar kompaktlanabilir, sorunlu olan dokunulmaz kalır
    sil(c, [r[0] for r in c.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id=1 ORDER BY CAST(oyuncu_id AS INTEGER) LIMIT 2")])
    hy.durum_yaz(sorunlu, sorunlu_durum)
    once = _atamalar(c)
    hy.dagit()
    sonra = _atamalar(c)
    # sorunlu hesabın kümesi (hem alıcı hem verici olarak) AYNI kalır; bekleyen yok
    assert all(h is not None for h in sonra.values())
    degisenler = {o for o in sonra if sonra[o] != once.get(o)}
    assert all(once[o] != sorunlu and sonra[o] != sorunlu for o in degisenler), "sorunlu hesaptan/hesaba taşıma yok"
    if sorunlu == 1:
        assert degisenler == set(), "sorunlu hesap alıcı olamaz: diğerleri yerinde kalır"
    elif sorunlu == 2:
        assert len(degisenler) == 2 and all(once[o] == 3 and sonra[o] == 1 for o in degisenler), "yalnız 3 -> 1 taşıması"
    else:
        assert len(degisenler) == 2 and all(once[o] == 2 and sonra[o] == 1 for o in degisenler), "3. hesap sorunlu: 2 -> 1"
    assert hy.dagit() and _atamalar(c) == sonra, "idempotent"
