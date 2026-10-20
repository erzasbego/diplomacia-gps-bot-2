"""Grup A güvenilirlik testleri: Y1 (servis yetkisi), Y2 (boş alan), Y3 (savaş tamlığı), Y4 (yeniden onay).

MOCK kaynak/gönderici; canlı doğrulama DEĞİLDİR.
"""
import asyncio

import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte

from dipbot import takip, yonetim
from dipbot.kaynak import EksikVeri, OyuncuAnlik, SavasListesi, SavasOzet, SiteErisilemez
from tg_fakes import YETKILI, grup_dugme, grup_komut, onay_kimligi

G3 = -1003  # onaysız grup


def _grup_savas_baseline(conn, g):
    return conn.execute("SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (g,)).fetchone()[0]


# ---------------------------------------------------------------- Y1
def test_y1_onaysiz_grup_icin_servis_degisiklik_yapmaz(conn, servis, src):
    src.oyuncu("1", "A")
    src.parti("P", "Parti", [("1", "A")])
    cagri_oncesi = len(src.cagrilar)
    for fn in (
        lambda: servis.oyuncu_ekle(G3, "1", src),
        lambda: servis.parti_ekle(G3, "P", src),
        lambda: servis.parti_kaldirma_hazirla(G3, "P", src),
        lambda: servis.parti_kaldirma_onayla(G3, "P", src, "x"),
    ):
        with pytest.raises(takip.GrupOnaysiz):
            run(fn())
    with pytest.raises(takip.GrupOnaysiz):
        servis.oyuncu_sil(G3, "1")
    assert len(src.cagrilar) == cagri_oncesi, "onaysız grup için kaynağa istek bile atılmamalı"
    for tablo in ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "engeller"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tablo} WHERE group_id=?", (G3,)).fetchone()[0] == 0


def test_y1_onayi_kaldirilan_grupta_mevcut_takip_degismez(conn, servis, src, saat):
    src.oyuncu("1", "A")
    src.oyuncu("2", "B")
    src.parti("P", "Parti", [("2", "B")])
    run(servis.oyuncu_ekle(G1, "1", src))
    run(servis.parti_ekle(G1, "P", src))
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    with pytest.raises(takip.GrupOnaysiz):
        servis.oyuncu_sil(G1, "1")
    with pytest.raises(takip.GrupOnaysiz):
        run(servis.oyuncu_ekle(G1, "9", src))
    o = None
    with pytest.raises(takip.GrupOnaysiz):
        run(servis.parti_kaldirma_onayla(G1, "P", src, "x"))
    assert takipte(conn, G1) == {"1", "2"}
    assert o is None
    # Başka (onaylı) grup etkilenmez
    run(servis.oyuncu_ekle(G2, "1", src))
    assert takipte(conn, G2) == {"1"}


def test_y1_await_sirasinda_onay_kaldirilirsa_yazilmaz(conn, servis, src, saat):
    src.oyuncu("1", "A")
    src.parti("P", "Parti", [("1", "A")])
    kapi = src.kapilar[("parti", "P")] = asyncio.Event()

    async def senaryo():
        gorev = asyncio.ensure_future(servis.parti_ekle(G1, "P", src))
        await asyncio.sleep(0.01)  # kaynak çağrısında bekliyor
        yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
        kapi.set()
        await gorev

    with pytest.raises(takip.GrupOnaysiz):
        run(senaryo())
    assert takipte(conn, G1) == set()
    assert conn.execute("SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", (G1,)).fetchone()[0] == 0


def test_y1_onay_kaldirilinca_bekleyen_onay_islemi_servise_ulasmaz(conn, saat, src):
    """tg: onay anında grup onayı yeniden denetlenir; ayrıca servis katmanı da reddeder."""
    from dipbot.tg.arayuz import Arayuz

    ui = Arayuz(conn, src, KURUCU, saat=saat)
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("oyuncu_ekle", "72400")))
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "72400"))))
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    r = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert r is not None
    assert "72400" in takipte(conn, G1), "onaysız gruba onay butonu ile silme yapılamaz"


def test_y1_tg_servis_yetkisiz_grup_islemi_acik_hata_verir_cokmez(conn, saat, src):
    """Doğrudan oluşturulmuş (sohbet bağlamıyla uyumlu) ama grubu onaysız işlem: kullanıcıya açık yanıt."""
    from dipbot.tg.arayuz import Arayuz

    ui = Arayuz(conn, src, KURUCU, saat=saat)
    kid = ui.islemler.olustur("oyuncu_sil", G3, KURUCU, G3, {"id": "1"})
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", grup=G3)))
    assert r.sessiz and r.uyari_goster, "onaysız grupta düğme sessizce reddedilmeli"
    assert takipte(conn, G3) == set()


# ---------------------------------------------------------------- Y2
BOS_DEGERLER = [None, "", "   ", "\t\n"]


@pytest.mark.parametrize("bos", BOS_DEGERLER)
def test_y2_bos_bolge_ve_vatandaslik_eski_degeri_ezmez(conn, servis, src, bos):
    src.oyuncu("1", "Ali", "Varna", "Macaristan")
    run(servis.oyuncu_ekle(G1, "1", src))
    src.oyuncular["1"] = OyuncuAnlik("1", "Ali", bos, bos)
    r = run(servis.oyuncular_tara(G1, src))
    assert r.ok and r.degisiklik == 0
    assert getattr(r, "eksik", 0) == 1, "boş alanlar ayrıca 'eksik' sayılmalı"
    assert tuple(conn.execute("SELECT ad, bolge, vatandaslik_ulkesi FROM takip_oyuncular").fetchone()) == (
        "Ali", "Varna", "Macaristan")
    assert bildirimler(conn) == []


@pytest.mark.parametrize("bos", BOS_DEGERLER)
def test_y2_bos_ad_basarisiz_tarama_ve_eski_ad_korunur(conn, servis, src, bos):
    src.oyuncu("1", "Ali", "Varna", "Macaristan")
    run(servis.oyuncu_ekle(G1, "1", src))
    src.oyuncular["1"] = OyuncuAnlik("1", bos, "Istanbul", "Macaristan")
    r = run(servis.oyuncular_tara(G1, src))
    assert not r.ok and r.degisiklik == 0
    assert tuple(conn.execute("SELECT ad, bolge FROM takip_oyuncular").fetchone()) == ("Ali", "Varna")
    assert bildirimler(conn) == []


def test_y2_kimlik_uyusmazligi_ve_bosluklu_gercek_degisiklik(conn, servis, src):
    src.oyuncu("1", "Ali", "Varna", "Macaristan")
    run(servis.oyuncu_ekle(G1, "1", src))
    src.oyuncular["1"] = OyuncuAnlik("2", "Baska", "Yer", "Ulke")
    r = run(servis.oyuncular_tara(G1, src))
    assert not r.ok
    assert tuple(conn.execute("SELECT ad, bolge FROM takip_oyuncular").fetchone()) == ("Ali", "Varna")
    # Boşluklu ama aynı değer: değişiklik değil. Gerçek değişiklik boşluksuz saklanır.
    src.oyuncular["1"] = OyuncuAnlik("1", " Ali ", " Varna ", "Macaristan")
    assert run(servis.oyuncular_tara(G1, src)).degisiklik == 0
    src.oyuncular["1"] = OyuncuAnlik("1", "Ali", " Istanbul ", "Macaristan")
    assert run(servis.oyuncular_tara(G1, src)).degisiklik == 1
    assert conn.execute("SELECT bolge FROM takip_oyuncular").fetchone()[0] == "Istanbul"
    assert bildirimler(conn)[0]["veri"]["degisimler"][0]["yeni"] == "Istanbul"


def test_y2_ilk_ekleme_bos_ad_ve_bosluklu_alanlar(conn, servis, src):
    src.oyuncular["1"] = OyuncuAnlik("1", "  ", "X", "Y")
    with pytest.raises(EksikVeri):
        run(servis.oyuncu_ekle(G1, "1", src))
    src.oyuncular["2"] = OyuncuAnlik("2", " Veli ", "  ", "")
    s = run(servis.oyuncu_ekle(G1, "2", src))
    assert s.ad == "Veli" and s.bolge is None and s.vatandaslik_ulkesi is None
    assert tuple(conn.execute("SELECT ad, bolge, vatandaslik_ulkesi FROM takip_oyuncular WHERE oyuncu_id='2'").fetchone()) == (
        "Veli", None, None)


# ---------------------------------------------------------------- Y3
def test_y3_eksik_liste_baseline_tamamlamaz_ve_bildirim_uretmez(conn, servis, src):
    src.savaslar = [SavasOzet("w1", "A")]
    src.savas_tamam = False
    r = run(servis.savaslari_tara(G1, src))
    assert not r.ok
    assert _grup_savas_baseline(conn, G1) == 0
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 0
    assert bildirimler(conn) == []


def test_y3_baseline_sonrasi_eksik_liste_yeni_savas_bildirmez(conn, servis, src):
    src.savaslar = [SavasOzet("w1", "A")]
    assert run(servis.savaslari_tara(G1, src)).ok
    assert _grup_savas_baseline(conn, G1) == 1
    src.savaslar = [SavasOzet("w1", "A"), SavasOzet("w2", "B")]
    src.savas_tamam = False
    r = run(servis.savaslari_tara(G1, src))
    assert not r.ok and bildirimler(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 1
    src.savas_tamam = True  # tam liste gelince w2 gerçekten yeni ve bildirilir
    assert run(servis.savaslari_tara(G1, src)).degisiklik == 1
    assert [b["veri"]["savas_id"] for b in bildirimler(conn, "yeni_savas")] == ["w2"]


def test_y3_hata_ve_supheli_bos_liste_baseline_tamamlamaz(conn, servis, src):
    src.hatalar[("savaslar", None)] = SiteErisilemez("503")
    assert not run(servis.savaslari_tara(G1, src)).ok
    assert _grup_savas_baseline(conn, G1) == 0
    src.hatalar.clear()
    # baseline sonrası boş liste: hiçbir şey silinmez/bildirilmez, sonraki tam liste yağmur üretmez
    src.savaslar = [SavasOzet(f"w{i}", "S") for i in range(5)]
    run(servis.savaslari_tara(G1, src))
    src.savaslar = []
    run(servis.savaslari_tara(G1, src))
    src.savaslar = [SavasOzet(f"w{i}", "S") for i in range(5)]
    run(servis.savaslari_tara(G1, src))
    assert bildirimler(conn) == []


def test_y3_ilk_bos_liste_baseline_tamamlamaz_sonra_dolu_liste_bildirimsiz_degil_baseline(conn, servis, src):
    """İlk taramada boş liste şüpheli: baseline tamamlanmaz (sonraki dolu liste yine bildirimsiz oturur)."""
    src.savaslar = []
    r = run(servis.savaslari_tara(G1, src))
    assert r.ok and r.uyarilar
    assert _grup_savas_baseline(conn, G1) == 0
    src.savaslar = [SavasOzet(f"w{i}", "S") for i in range(30)]
    run(servis.savaslari_tara(G1, src))
    assert bildirimler(conn) == []
    assert _grup_savas_baseline(conn, G1) == 1


def test_y3_savas_listesi_tipi():
    l = SavasListesi([SavasOzet("1")], True)
    assert l.savaslar == (SavasOzet("1"),) and l.tamam is True


# ---------------------------------------------------------------- Y4 / k16
def test_y4_grup_kaldir_yeniden_onayla_savas_baseline(conn, servis, src, saat):
    src.savaslar = [SavasOzet("w1", "A"), SavasOzet("w2", "B")]
    run(servis.savaslari_tara(G1, src))
    src.savaslar.append(SavasOzet("w3", "Yeni"))
    run(servis.savaslari_tara(G1, src))
    assert [b["veri"]["savas_id"] for b in bildirimler(conn, "yeni_savas")] == ["w3"]
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    assert yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)  # yeniden onay
    assert _grup_savas_baseline(conn, G1) == 0
    # kapalıyken açılan w4 da yeniden etkinleştirmede bildirimsiz baseline olur (karar: dp.txt bölüm 8)
    src.savaslar.append(SavasOzet("w4", "Kapaliyken"))
    onceki = len(bildirimler(conn, "yeni_savas"))
    assert run(servis.savaslari_tara(G1, src)).degisiklik == 0
    assert len(bildirimler(conn, "yeni_savas")) == onceki
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 4
    # baseline sonrası gerçekten yeni savaş bildirilir
    src.savaslar.append(SavasOzet("w5", "Sonradan"))
    assert run(servis.savaslari_tara(G1, src)).degisiklik == 1
    assert [b["veri"]["savas_id"] for b in bildirimler(conn, "yeni_savas")][-1] == "w5"


def test_y4_yeniden_onayda_eksik_liste_baseline_tamamlamaz(conn, servis, src, saat):
    src.savaslar = [SavasOzet("w1", "A")]
    run(servis.savaslari_tara(G1, src))
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    src.savas_tamam = False
    assert not run(servis.savaslari_tara(G1, src)).ok
    assert _grup_savas_baseline(conn, G1) == 0
    src.savas_tamam = True
    src.savaslar.append(SavasOzet("w2", "B"))
    run(servis.savaslari_tara(G1, src))
    assert bildirimler(conn, "yeni_savas") == []
