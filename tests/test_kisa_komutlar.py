"""Kısa komutlar (/oyuncuekle /oyuncusil /partiekle /partisil), eski adların geriye dönük uyumu, yetki/onay
korunumu ve ID/bağlantı kabulü. MOCK; canlı Telegram doğrulaması DEĞİLDİR."""
import pytest

from conftest import bildirimler, run, takipte
from tg_fakes import G1, G3, KURUCU, YABANCI, YETKILI, grup_dugme, grup_komut, komut, onay_kimligi, veriler

from dipbot import yonetim
from dipbot.tg.arayuz import ESKI_KOMUTLAR, KOMUTLAR, Arayuz
from dipbot.tg.uygulama import bot_komutlari


@pytest.fixture
def ui(conn, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return Arayuz(conn, src, KURUCU, saat=saat)


def _kisa(ad):
    return ad.replace("_", "")


# ------------------------------------------------------------------ görünürlük
def test_komut_listesinde_yalniz_yeni_kisa_adlar():
    ad = [c.command for c in bot_komutlari()]
    for yeni in ("oyuncuekle", "oyuncusil", "partiekle", "partisil"):
        assert yeni in ad
    assert not set(ESKI_KOMUTLAR) & set(ad)
    assert ESKI_KOMUTLAR == {"oyuncu_ekle": "oyuncuekle", "oyuncu_sil": "oyuncusil",
                             "parti_ekle": "partiekle", "parti_sil": "partisil"}
    assert set(ESKI_KOMUTLAR.values()) <= set(KOMUTLAR)


def test_yardim_ve_menuler_yalniz_kisa_komutlari_gosterir(ui, src):
    ekranlar = [
        run(ui.komut(grup_komut("yardim"))).metin,
        run(ui.komut(grup_komut("oyuncular"))).metin,   # boş liste yönlendirmesi
        run(ui.komut(grup_komut("partiler"))).metin,
        run(ui.buton(grup_dugme("oe"))).metin,
        run(ui.buton(grup_dugme("pe"))).metin,
    ]
    tum = "\n".join(ekranlar)
    for yeni in ("/oyuncuekle", "/oyuncusil", "/partiekle", "/partisil"):
        assert yeni in tum, yeni
    for eski in ESKI_KOMUTLAR:
        assert f"/{eski}" not in tum, eski


# ------------------------------------------------------------------ davranış (yeni adlar)
def test_oyuncuekle_id_ile_ve_oyuncusil_onayli(ui, conn, src):
    src.oyuncu("72400", "Ali", "Ankara", "TR")
    y = run(ui.komut(grup_komut("oyuncuekle", "72400")))
    assert "takibe eklendi" in y.metin and takipte(conn, G1) == {"72400"}
    y = run(ui.komut(grup_komut("oyuncusil", "72400")))
    assert takipte(conn, G1) == {"72400"}, "silme onay ister"
    assert "c:e:" + onay_kimligi(y) in veriler(y)
    run(ui.buton(grup_dugme(f"c:e:{onay_kimligi(y)}")))
    assert takipte(conn, G1) == set()


def test_partiekle_ve_partisil_onayli(ui, conn, src):
    src.oyuncu("1", "A1"); src.oyuncu("2", "A2")
    src.parti("1244", "TEKNO", [("1", "A1"), ("2", "A2")])
    y = run(ui.komut(grup_komut("partiekle", "1244")))
    assert "Parti takibe eklendi" in y.metin and takipte(conn, G1) == {"1", "2"}
    y = run(ui.komut(grup_komut("partisil", "1244")))
    kid = onay_kimligi(y)
    assert takipte(conn, G1) == {"1", "2"}, "parti silme onay ister"
    run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert takipte(conn, G1) == set()


# ------------------------------------------------------------------ eski adlar çalışır
def test_eski_komut_adlari_ayni_sonucu_verir(ui, conn, src):
    src.oyuncu("72400", "Ali", "Ankara", "TR")
    src.oyuncu("1", "A1")
    src.parti("1244", "TEKNO", [("1", "A1")])
    assert "takibe eklendi" in run(ui.komut(grup_komut("oyuncu_ekle", "72400"))).metin
    assert "Parti takibe eklendi" in run(ui.komut(grup_komut("parti_ekle", "1244"))).metin
    y = run(ui.komut(grup_komut("oyuncu_sil", "72400")))
    run(ui.buton(grup_dugme(f"c:e:{onay_kimligi(y)}")))
    assert "72400" not in takipte(conn, G1)
    y = run(ui.komut(grup_komut("parti_sil", "1244")))
    run(ui.buton(grup_dugme(f"c:e:{onay_kimligi(y)}")))
    assert takipte(conn, G1) == set()


@pytest.mark.parametrize("ad", ["oyuncuekle", "oyuncu_ekle", "oyuncusil", "oyuncu_sil",
                                "partiekle", "parti_ekle", "partisil", "parti_sil"])
def test_yetki_yeni_ve_eski_adlarda_ayni(ui, conn, src, ad):
    src.oyuncu("72400", "Ali"); src.oyuncu("1", "A1"); src.parti("1244", "T", [("1", "A1")])
    arg = "1244" if "parti" in ad else "72400"
    # yetkisiz kullanıcı: onaylı grupta bile işlem yok
    run(ui.komut(grup_komut(ad, arg, kul=YABANCI)))
    assert takipte(conn, G1) == set() and src.cagrilar == []
    assert len(bildirimler(conn, "yetkisiz_deneme")) >= 1
    # onaysız grup
    run(ui.komut(grup_komut(ad, arg, grup=G3)))
    assert src.cagrilar == [] and conn.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 0
    # özel sohbette yetkili (kurucu değil) kullanamaz
    run(ui.komut(komut(ad, arg, kul=YETKILI)))
    assert src.cagrilar == []


def test_silme_onayi_baska_kullaniciya_veya_sohbete_bagli(ui, conn, src):
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("oyuncuekle", "72400")))
    y = run(ui.komut(grup_komut("oyuncusil", "72400", kul=YETKILI)))
    kid = onay_kimligi(y)
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=KURUCU)))  # başka kullanıcı
    assert r.uyari and takipte(conn, G1) == {"72400"}


# ------------------------------------------------------------------ ID/bağlantı çeşitleri komutlarda
@pytest.mark.parametrize("arg", [
    "72400",
    "https://diplomacia.com.tr/player/72400",
    "https://diplomacia.com.tr/profile/player/72400",
    "https://diplomacia.com.tr/press/player/72400",
    "diplomacia.com.tr/press/player/72400/",
])
@pytest.mark.parametrize("ad", ["oyuncuekle", "oyuncu_ekle"])
def test_oyuncuekle_tum_baglanti_cesitleri(ui, conn, src, ad, arg):
    src.oyuncu("72400", "Ali", "Ankara", "TR")
    assert "takibe eklendi" in run(ui.komut(grup_komut(ad, arg))).metin
    assert takipte(conn, G1) == {"72400"}
    # kullanıcıya gösterilen bağlantı kanonik ve tıklanabilir
    y = run(ui.komut(grup_komut("oyuncular")))
    assert 'href="https://diplomacia.com.tr/player/72400"' in y.metin


@pytest.mark.parametrize("arg", [
    "1244",
    "https://diplomacia.com.tr/party/1244",
    "https://diplomacia.com.tr/profile/party/1244",
    "https://diplomacia.com.tr/press/party/1244",
])
def test_partiekle_tum_baglanti_cesitleri(ui, conn, src, arg):
    src.oyuncu("1", "A1")
    src.parti("1244", "TEKNO", [("1", "A1")])
    y = run(ui.komut(grup_komut("partiekle", arg)))
    assert "Parti takibe eklendi" in y.metin and 'href="https://diplomacia.com.tr/party/1244"' in y.metin


@pytest.mark.parametrize("ad,arg,parca", [
    ("oyuncuekle", "https://diplomacia.com.tr/party/1244", "parti bağlantısı"),
    ("oyuncuekle", "https://diplomacia.com.tr/press/party/1244", "parti bağlantısı"),
    ("oyuncusil", "https://diplomacia.com.tr/profile/party/1244", "parti bağlantısı"),
    ("partiekle", "https://diplomacia.com.tr/player/72400", "oyuncu bağlantısı"),
    ("partisil", "https://diplomacia.com.tr/press/player/72400", "oyuncu bağlantısı"),
])
def test_yanlis_tur_url_acik_hata_ve_istek_yok(ui, conn, src, ad, arg, parca):
    y = run(ui.komut(grup_komut(ad, arg)))
    assert parca in y.metin and "Kullanım" in y.metin
    assert src.cagrilar == [] and takipte(conn, G1) == set()
    assert not veriler(y) or not any(v.startswith("c:e:") for v in veriler(y))


@pytest.mark.parametrize("ad", ["oyuncuekle", "oyuncusil", "partiekle", "partisil"])
@pytest.mark.parametrize("arg", ["", "abc", "0", "-5", "072400", "1 2", "https://evil.example/player/72400",
                                 "https://diplomacia.com.tr.evil.example/player/72400",
                                 "https://diplomacia.com.tr@evil.example/player/1", "<b>1</b>",
                                 "https://diplomacia.com.tr/wars/war/5333"])
def test_gecersiz_girdi_reddedilir_istek_atilmaz(ui, conn, src, ad, arg):
    y = run(ui.komut(grup_komut(ad, arg)))
    assert "Kullanım" in y.metin
    assert src.cagrilar == [] and takipte(conn, G1) == set()
    assert "<b>1</b>" not in y.metin  # girdi HTML olarak yansıtılmaz
