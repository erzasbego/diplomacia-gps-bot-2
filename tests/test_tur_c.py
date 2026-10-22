"""Tur C: D1 (onayda yetki yeniden denetimi), D2 (onay kalkınca durum yazılmaz), D5 (hata sınıfı mesajı),
A5-kalan (uid'siz otomatik ekleme mükerrer), O-c (404 = başarı değil). MOCK; canlı doğrulama DEĞİLDİR."""
import asyncio

import pytest
from conftest import G1, KURUCU, bildirimler, run, takipte

from dipbot import config, yonetim
from dipbot.kaynak import (
    ErisimReddedildi,
    IstekSiniri,
    OyuncuAnlik,
    TokenGecersiz,
    TokenYok,
)

YETKILI = 55


def _parti_kur(src, uyeler=("a", "b", "c")):
    for u in uyeler:
        src.oyuncu(u, u.upper())
    src.parti("5830", "TEKNO", [(u, u.upper()) for u in uyeler])


# ---------------------------------------------------------------- D1
def test_d1_onay_beklerken_yetki_alinirsa_silme_yapilmaz(conn, servis, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    _parti_kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    o = run(servis.parti_kaldirma_hazirla(G1, "5830", src))
    kapi = asyncio.Event()
    src.kapilar[("parti", "5830")] = kapi

    async def senaryo():
        t = asyncio.ensure_future(
            servis.parti_kaldirma_onayla(G1, "5830", src, o.ozet_anahtari, islem_yapan=YETKILI, kurucu_id=KURUCU)
        )
        await asyncio.sleep(0.01)
        yonetim.yetki_al(conn, saat, KURUCU, KURUCU, YETKILI)  # ağ beklerken yetki alındı
        kapi.set()
        return await t

    s = run(senaryo())
    assert s.durum == "yetkisiz"
    assert takipte(conn, G1) == {"a", "b", "c"}
    assert conn.execute("SELECT COUNT(*) FROM takip_partiler").fetchone()[0] == 1


def test_d1_baslangicta_yetkisiz_kullanici_silemez_yetkili_ve_kurucu_silebilir(conn, servis, src, saat):
    _parti_kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    o = run(servis.parti_kaldirma_hazirla(G1, "5830", src))
    s = run(servis.parti_kaldirma_onayla(G1, "5830", src, o.ozet_anahtari, islem_yapan=999, kurucu_id=KURUCU))
    assert s.durum == "yetkisiz" and takipte(conn, G1) == {"a", "b", "c"}
    s = run(servis.parti_kaldirma_onayla(G1, "5830", src, o.ozet_anahtari, islem_yapan=KURUCU, kurucu_id=KURUCU))
    assert s.durum == "silindi"


# ---------------------------------------------------------------- D2
def test_d2_onay_kalkarsa_oyuncu_durumu_yazilmaz_yeniden_onayda_degisiklik_gorulur(conn, servis, src, saat):
    src.oyuncu("a", "A")
    run(servis.oyuncu_ekle(G1, "a", src))
    src.oyuncu("a", "A2")
    kapi = asyncio.Event()
    src.kapilar[("oyuncu", "a")] = kapi

    async def senaryo():
        t = asyncio.ensure_future(servis.oyuncular_tara(G1, src))
        await asyncio.sleep(0.01)
        yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)  # tarama uçuşta, onay kalktı
        kapi.set()
        return await t

    run(senaryo())
    assert conn.execute("SELECT ad FROM takip_oyuncular WHERE oyuncu_id='a'").fetchone()[0] == "A"
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    src.kapilar.clear()
    run(servis.oyuncular_tara(G1, src))
    assert len(bildirimler(conn, "oyuncu_degisti")) == 1  # yutulmadı


def test_d2_onay_kalkarsa_parti_durumu_yazilmaz_yeniden_onayda_gorulur(conn, servis, src, saat):
    _parti_kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    src.oyuncu("d", "D")
    src.parti("5830", "TEKNO", [(u, u.upper()) for u in "abcd"])
    kapi = asyncio.Event()
    src.kapilar[("parti", "5830")] = kapi

    async def senaryo():
        t = asyncio.ensure_future(servis.partiler_tara(G1, src))
        await asyncio.sleep(0.01)
        yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
        kapi.set()
        return await t

    run(senaryo())
    n = conn.execute("SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=?", (G1,)).fetchone()[0]
    assert n == 3 and takipte(conn, G1) == {"a", "b", "c"}
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    src.kapilar.clear()
    run(servis.partiler_tara(G1, src))
    b = bildirimler(conn, "parti_uyelik")
    assert len(b) == 1 and [x["oyuncu_id"] for x in b[0]["veri"]["katilanlar"]] == ["d"]


def test_d2_onay_kalkarsa_savas_baseline_ve_kayit_yazilmaz(conn, servis, src, saat):
    from dipbot.kaynak import SavasOzet

    kapi = asyncio.Event()
    src.kapilar[("savaslar", None)] = kapi
    src.savaslar = [SavasOzet("w1", "Savaş 1")]

    async def senaryo():
        t = asyncio.ensure_future(servis.savaslari_tara(G1, src))
        await asyncio.sleep(0.01)
        yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
        kapi.set()
        return await t

    run(senaryo())
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 0
    r = conn.execute("SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()
    assert not r or r[0] == 0


# ---------------------------------------------------------------- D5
@pytest.mark.parametrize(
    "hata,beklenen",
    [
        (TokenGecersiz("401"), "rejected the token (401)"),
        (TokenYok("yok"), "token file is missing"),
        (ErisimReddedildi("403"), "access denied"),
        (IstekSiniri("429"), "rate limit"),
    ],
)
def test_d5_kaldirma_hazirla_hata_sinifina_gore_acik_mesaj_ve_silme_yok(conn, servis, src, hata, beklenen):
    _parti_kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    src.hatalar[("parti", "5830")] = hata
    o = run(servis.parti_kaldirma_hazirla(G1, "5830", src))
    assert not o.basarili and beklenen in o.neden and "no players were removed" in o.neden
    s = run(servis.parti_kaldirma_onayla(G1, "5830", src, "x"))
    assert s.durum == "dogrulanamadi" and beklenen in s.neden
    assert takipte(conn, G1) == {"a", "b", "c"}


# ---------------------------------------------------------------- A5-kalan
def test_a5_parti_taramasinda_ayni_kisi_baska_numarayla_mukerrer_olusturmaz(conn, servis, src):
    src.oyuncular["a"] = OyuncuAnlik("a", "A", "Varna", "U1", uid="UUID-A")
    src.parti("P", "PARTI", [("a", "A")])
    run(servis.parti_ekle(G1, "P", src))
    # aynı kişi parti listesinde başka numarayla (a2) görünür
    src.oyuncular["a2"] = OyuncuAnlik("a2", "A", "Varna", "U1", uid="UUID-A")
    src.parti("P", "PARTI", [("a", "A"), ("a2", "A")])
    for _ in range(3):
        run(servis.partiler_tara(G1, src))
        run(servis.oyuncular_tara(G1, src))
    assert takipte(conn, G1) == {"a"}
    # F1-3b: mükerrer numara 'Katılanlar (takibe alındı)' olarak LİSTELENMEZ; içeriği yalnız ondan
    # ibaret bekleyen bildirim iptal edilir (eskiden aktif kalıp yanlış metni sabitliyordu).
    uyelik = bildirimler(conn, "parti_uyelik")
    assert len(uyelik) == 1 and uyelik[0]["durum"] == "iptal"  # bildirim yağmuru yok
    assert bildirimler(conn, "oyuncu_degisti") == []


# ---------------------------------------------------------------- O-c
def test_oc_hepsi_404_ise_tarama_basarisiz_sayilir(conn, servis, src):
    for u in "ab":
        src.oyuncu(u, u.upper())
        run(servis.oyuncu_ekle(G1, u, src))
    src.oyuncular.clear()  # hepsi 404
    r = run(servis.oyuncular_tara(G1, src))
    assert not r.ok and r.hatalar
    assert takipte(conn, G1) == {"a", "b"}  # otomatik silme YOK


def test_oc_kismen_404_basarili_ama_uyarili(conn, servis, src):
    for u in "ab":
        src.oyuncu(u, u.upper())
        run(servis.oyuncu_ekle(G1, u, src))
    del src.oyuncular["b"]
    r = run(servis.oyuncular_tara(G1, src))
    assert r.ok and any("b" in x for x in r.uyarilar)


def test_oc_hepsi_404_parti_taramasi_basarisiz(conn, servis, src):
    _parti_kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    src.partiler.clear()
    r = run(servis.partiler_tara(G1, src))
    assert not r.ok and r.hatalar


def test_oc_art_arda_n_taramada_404_bulunamiyor_isareti(conn, servis, src):
    for u in "ab":
        src.oyuncu(u, u.upper())
        run(servis.oyuncu_ekle(G1, u, src))
    del src.oyuncular["b"]
    for _ in range(config.NOT_FOUND_STREAK_THRESHOLD - 1):
        run(servis.oyuncular_tara(G1, src))
    assert servis.bulunamayan_oyuncular(G1) == []
    run(servis.oyuncular_tara(G1, src))
    assert servis.bulunamayan_oyuncular(G1) == ["b"]
    assert takipte(conn, G1) == {"a", "b"}
    src.oyuncu("b", "B")  # geri geldi: sayaç sıfırlanır
    run(servis.oyuncular_tara(G1, src))
    assert servis.bulunamayan_oyuncular(G1) == []


def test_oc_hepsi_404_zamanlayicida_son_basari_degismez(conn, servis, src, saat):
    from dipbot.zamanlayici import TaramaZamanlayici

    src.oyuncu("a", "A")
    run(servis.oyuncu_ekle(G1, "a", src))
    z = TaramaZamanlayici(conn, src, KURUCU, saat)
    run(z.tick_ve_bekle())
    ilk = conn.execute(
        "SELECT son_basari FROM tarama_isleri WHERE group_id=? AND is_tipi='oyuncular'", (G1,)
    ).fetchone()[0]
    assert ilk
    src.oyuncular.clear()
    saat.ilerle(3600)
    run(z.tick_ve_bekle())
    son = conn.execute(
        "SELECT son_basari FROM tarama_isleri WHERE group_id=? AND is_tipi='oyuncular'", (G1,)
    ).fetchone()[0]
    assert son == ilk


def test_oc_bulunamiyor_isareti_oyuncu_ayrintisinda_gorunur(conn, servis, src, saat):
    from dipbot.tg.arayuz import Arayuz

    for u in "ab":
        src.oyuncu(u, u.upper())
        run(servis.oyuncu_ekle(G1, u, src))
    del src.oyuncular["b"]
    ui = Arayuz(conn, src, KURUCU, saat=saat)
    for _ in range(config.NOT_FOUND_STREAK_THRESHOLD):
        assert "Not found" not in ui._oyuncu_detay(G1, "b").metin
        run(servis.oyuncular_tara(G1, src))
    assert "Not found" in ui._oyuncu_detay(G1, "b").metin
    assert "Not found" not in ui._oyuncu_detay(G1, "a").metin
