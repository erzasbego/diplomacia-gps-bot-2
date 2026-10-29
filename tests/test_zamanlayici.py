"""Tarama zamanlayıcısı çekirdeği (MOCK kaynakla; gerçek zamanlama/ağ doğrulaması DEĞİLDİR)."""
import asyncio

import pytest

from conftest import G1, G2, KURUCU, bildirimler, run

from dipbot import config, yonetim
from dipbot.kaynak import (
    AgZamanAsimi,
    IstekSiniri,
    SinirliKaynak,
    SiteErisilemez,
    TokenGecersiz,
)
from dipbot.takip import TakipServisi
from dipbot.zamanlayici import TaramaZamanlayici
from fakes import SahteKaynak

G3 = -1003


def _z(conn, src, saat, **kw):
    return TaramaZamanlayici(conn, src, KURUCU, saat, **kw)


def _tarama_isi(conn, grup, tip="oyuncular"):
    return conn.execute(
        "SELECT * FROM tarama_isleri WHERE group_id=? AND is_tipi=?", (grup, tip)
    ).fetchone()


def _oyuncu_ekle(servis, src, grup, oid):
    src.oyuncu(oid, oid.upper())
    run(servis.oyuncu_ekle(grup, oid, src))


def test_ayni_is_ust_uste_binmez_ve_aralik_gozetilir(conn, src, saat, servis):
    _oyuncu_ekle(servis, src, G1, "p1")
    _oyuncu_ekle(servis, src, G2, "p2")  # G2 de kapıda beklesin: iki tarama da sürerken sınanır
    z = _z(conn, src, saat)

    async def sen():
        gate = asyncio.Event()
        src.kapilar[("oyuncu", "p1")] = gate
        src.kapilar[("oyuncu", "p2")] = gate
        ilk = z.tick()
        await asyncio.sleep(0.01)
        assert len(ilk) == 2 and z.calisiyor_mu(G1) and z.calisiyor_mu(G2)  # ikisinin de vadesi geldi
        saat.ilerle(7200)  # vade çoktan geçti, ama tarama hâlâ sürüyor
        assert z.tick() == []  # aynı iş üst üste binmez
        gate.set()
        await asyncio.gather(*ilk)
        assert not z.calisiyor_mu(G1)
        assert z.tick() == []  # aralık dolmadı (10 dk)
        saat.ilerle(601)
        assert len(z.tick()) == 2
        await asyncio.sleep(0.05)

    run(sen())


def test_grup_basina_ayri_aralik(conn, src, saat):
    yonetim.tarama_araligi_ayarla(conn, G1, 5)
    yonetim.tarama_araligi_ayarla(conn, G2, 20)
    z = _z(conn, src, saat)

    async def sen():
        await z.tick_ve_bekle()
        saat.ilerle(5 * 60 + 1)
        g = z.tick()
        assert len(g) == 1  # yalnız G1 (5 dk); G2 20 dk
        await asyncio.gather(*g)
        saat.ilerle(15 * 60)
        assert len(z.tick()) == 2  # G1 yine vadede, G2 artık vadede
        await asyncio.sleep(0.05)

    run(sen())


def test_yavas_grup_digerini_durdurmaz(conn, src, saat, servis):
    _oyuncu_ekle(servis, src, G1, "yavas")
    _oyuncu_ekle(servis, src, G2, "hizli")
    z = _z(conn, src, saat)

    async def sen():
        gate = asyncio.Event()
        src.kapilar[("oyuncu", "yavas")] = gate
        gorevler = z.tick()
        await asyncio.sleep(0.05)
        assert _tarama_isi(conn, G2)["son_basari"] is not None  # hızlı grup bitti
        assert _tarama_isi(conn, G1) is None or _tarama_isi(conn, G1)["son_basari"] is None
        assert z.calisiyor_mu(G1) and not z.calisiyor_mu(G2)
        gate.set()
        await asyncio.gather(*gorevler)
        assert _tarama_isi(conn, G1)["son_basari"] is not None

    run(sen())


def test_token_gecersizken_istek_ve_hata_yagmuru_olmaz(conn, src, saat, servis):
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G3)
    for g, o in ((G1, "a"), (G2, "b"), (G3, "c")):
        _oyuncu_ekle(servis, src, g, o)
    src.cagrilar.clear()
    src.hatalar[("*", "*")] = TokenGecersiz("401")
    z = _z(conn, src, saat)
    run(z.tick_ve_bekle())
    assert z.token_duraklatildi
    n = len(src.cagrilar)
    assert n <= 3  # grup başına en fazla bir istek
    for _ in range(5):
        saat.ilerle(3600)
        assert z.tick() == []
    assert len(src.cagrilar) == n  # duraklatılmışken yeni istek yok
    b = bildirimler(conn, "token_gecersiz")
    assert len(b) == 1 and b[0]["hedef_sohbet"] == KURUCU and b[0]["grup_id"] is None  # tek, yalnız kurucuya
    for g in (G1, G2, G3):
        assert _tarama_isi(conn, g)["son_basari"] is None  # başarılı kaydedilmedi
    # Yeniden başlatmada da duraklatma korunur
    assert _z(conn, src, saat).token_duraklatildi
    # Yeni token doğrulandı: kaldığı yerden devam
    src.hatalar.clear()
    z.token_devam_ettir()
    run(z.tick_ve_bekle())
    assert _tarama_isi(conn, G1)["son_basari"] is not None
    assert len(bildirimler(conn, "token_gecersiz")) == 1


def test_basarisiz_tarama_basarili_kaydedilmez_ve_geri_cekilir(conn, src, saat, servis):
    _oyuncu_ekle(servis, src, G1, "p")
    z = _z(conn, src, saat)

    async def sen():
        src.cagrilar.clear()
        src.hatalar[("oyuncu", "p")] = SiteErisilemez("503")
        await z.tick_ve_bekle()
        r = _tarama_isi(conn, G1)
        assert r["son_basari"] is None and r["son_hata"]
        # Hata sonrası aralık 2 katına çıkar (10 dk x 2): 11. dakikada G1 vadede değil
        saat.ilerle(11 * 60)
        gorevler = z.tick()
        await asyncio.gather(*gorevler)
        assert src.cagri_say("oyuncu") == 1  # G1 geri çekilme süresinde: yeniden denenmedi
        assert conn.execute("SELECT ardisik_hata FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()[0] == 1
        src.hatalar.clear()
        saat.ilerle(30 * 60)
        await z.tick_ve_bekle()
        assert _tarama_isi(conn, G1)["son_basari"] is not None
        assert conn.execute("SELECT ardisik_hata FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()[0] == 0

    run(sen())


def test_istek_siniri_bekleme_uygulanir(conn, src, saat, servis):
    _oyuncu_ekle(servis, src, G1, "p")
    z = _z(conn, src, saat)
    src.hatalar[("oyuncu", "p")] = IstekSiniri("429", bekle_saniye=300)
    run(z.tick_ve_bekle())
    assert _tarama_isi(conn, G1)["son_basari"] is None
    src.hatalar.clear()
    saat.ilerle(120)
    assert z.tick() == []  # bekleme penceresi sürüyor
    saat.ilerle(3600)
    run(z.tick_ve_bekle())
    assert _tarama_isi(conn, G1)["son_basari"] is not None


def test_onayi_kaldirilan_grup_taranmaz(conn, src, saat):
    z = _z(conn, src, saat)
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G2)

    async def sen():
        g = z.tick()
        assert len(g) == 1  # yalnız G1
        await asyncio.gather(*g)
        assert _tarama_isi(conn, G2, "savaslar") is None
        assert _tarama_isi(conn, G1, "savaslar") is not None

    run(sen())


def test_tick_istekleri_gruplar_arasinda_paylasir_ama_durum_ayri(conn, src, saat, servis):
    for g in (G1, G2):
        src.oyuncu("ortak", "Ortak", "Varna", "U")
        run(servis.oyuncu_ekle(g, "ortak", src))
    src.cagrilar.clear()
    src.oyuncu("ortak", "Ortak", "Istanbul", "U")
    z = _z(conn, src, saat)
    run(z.tick_ve_bekle())
    assert src.cagri_say("oyuncu") == 1  # paylaşılan tek istek
    b = bildirimler(conn, "oyuncu_degisti")
    assert sorted(x["grup_id"] for x in b) == sorted([G1, G2])  # her liste kendi bildirimini üretir
    assert {x["hedef_sohbet"] for x in b} == {config.LOG_KANAL_ID}  # takip bildirimleri log kanalına gider
    assert len(b) == 2


def test_sinirli_kaynak_esler_ve_zaman_asimi():
    src = SahteKaynak()
    src.gecikme = 0.02
    for i in range(6):
        src.oyuncu(str(i), "x")
    sk = SinirliKaynak(src, esler=2, zaman_asimi=1.0)

    async def sen():
        await asyncio.gather(*(sk.fetch_player(str(i)) for i in range(6)))
        assert src.azami <= 2
        src.kapilar[("oyuncu", "0")] = asyncio.Event()  # hiç açılmaz
        kisa = SinirliKaynak(src, esler=2, zaman_asimi=0.05)
        with pytest.raises(AgZamanAsimi):
            await kisa.fetch_player("0")

    run(sen())
