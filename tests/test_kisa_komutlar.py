"""Kısa komutlar (/oyuncuekle /oyuncusil /partiekle /partisil), eski adların geriye dönük uyumu, yetki/onay
korunumu ve ID/bağlantı kabulü. MOCK; canlı Telegram doğrulaması DEĞİLDİR."""
import pytest

from conftest import bildirimler, run, takipte
from tg_fakes import (
    G1, G3, KURUCU, YABANCI, YETKILI, dugme, gercek_grup_komut, grup_dugme, grup_komut, komut, onay_kimligi, veriler,
)

from dipbot import yonetim
from dipbot.tg.arayuz import EN_KOMUTLAR, ESKI_KOMUTLAR, KOMUTLAR, TR_KOMUTLAR, Arayuz
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
    for yeni in ("addplayer", "removeplayer", "addparty", "removeparty"):
        assert yeni in ad
    # the menu lists only the English names; legacy and short Turkish aliases are hidden
    assert not set(ESKI_KOMUTLAR) & set(ad)
    assert not set(TR_KOMUTLAR) & set(ad)
    assert ESKI_KOMUTLAR == {"oyuncu_ekle": "oyuncuekle", "oyuncu_sil": "oyuncusil",
                             "parti_ekle": "partiekle", "parti_sil": "partisil"}
    assert set(ESKI_KOMUTLAR.values()) <= set(TR_KOMUTLAR)
    # English names map to the internal identifiers; the short Turkish aliases stay registered
    assert EN_KOMUTLAR["addplayer"] == "oyuncuekle" and EN_KOMUTLAR["removeplayer"] == "oyuncusil"
    assert EN_KOMUTLAR["addparty"] == "partiekle" and EN_KOMUTLAR["removeparty"] == "partisil"
    assert {"oyuncuekle", "oyuncusil", "partiekle", "partisil"} <= set(TR_KOMUTLAR)


def test_yardim_ve_menuler_yalniz_kisa_komutlari_gosterir(ui, src):
    ekranlar = [
        run(ui.komut(grup_komut("help"))).metin,
        run(ui.komut(grup_komut("players"))).metin,   # empty-list redirect
        run(ui.komut(grup_komut("parties"))).metin,
        run(ui.buton(grup_dugme("oe"))).metin,
        run(ui.buton(grup_dugme("pe"))).metin,
    ]
    tum = "\n".join(ekranlar)
    for yeni in ("/addplayer", "/removeplayer", "/addparty", "/removeparty"):
        assert yeni in tum, yeni
    for eski in ESKI_KOMUTLAR:
        assert f"/{eski}" not in tum, eski
    for tr in ("oyuncuekle", "oyuncusil", "partiekle", "partisil"):
        assert f"/{tr}" not in tum, tr


def test_yardim_eski_ve_turkce_alias_ayni_ekrani_verir(ui):
    ana = run(ui.komut(grup_komut("help"))).metin
    assert run(ui.komut(grup_komut("yardim"))).metin == ana
    assert run(ui.komut(grup_komut("oyuncular"))).metin == run(ui.komut(grup_komut("players"))).metin
    assert run(ui.komut(grup_komut("partiler"))).metin == run(ui.komut(grup_komut("parties"))).metin


# ------------------------------------------------------------------ davranış (yeni adlar)
def test_oyuncuekle_id_ile_ve_oyuncusil_onayli(ui, conn, src):
    src.oyuncu("72400", "Ali", "Ankara", "TR")
    y = run(ui.komut(grup_komut("oyuncuekle", "72400")))
    assert "Player added to tracking" in y.metin and takipte(conn, G1) == {"72400"}
    y = run(ui.komut(grup_komut("oyuncusil", "72400")))
    assert takipte(conn, G1) == {"72400"}, "silme onay ister"
    assert "c:e:" + onay_kimligi(y) in veriler(y)
    run(ui.buton(grup_dugme(f"c:e:{onay_kimligi(y)}")))
    assert takipte(conn, G1) == set()


def test_partiekle_ve_partisil_onayli(ui, conn, src):
    src.oyuncu("1", "A1"); src.oyuncu("2", "A2")
    src.parti("1244", "TEKNO", [("1", "A1"), ("2", "A2")])
    y = run(ui.komut(grup_komut("partiekle", "1244")))
    assert "Party added to tracking" in y.metin and takipte(conn, G1) == {"1", "2"}
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
    assert "Player added to tracking" in run(ui.komut(grup_komut("oyuncu_ekle", "72400"))).metin
    assert "Party added to tracking" in run(ui.komut(grup_komut("parti_ekle", "1244"))).metin
    y = run(ui.komut(grup_komut("oyuncu_sil", "72400")))
    run(ui.buton(grup_dugme(f"c:e:{onay_kimligi(y)}")))
    assert "72400" not in takipte(conn, G1)
    y = run(ui.komut(grup_komut("parti_sil", "1244")))
    run(ui.buton(grup_dugme(f"c:e:{onay_kimligi(y)}")))
    assert takipte(conn, G1) == set()


@pytest.mark.parametrize("ad", ["oyuncuekle", "oyuncu_ekle", "oyuncusil", "oyuncu_sil",
                                "partiekle", "parti_ekle", "partisil", "parti_sil",
                                "addplayer", "removeplayer", "addparty", "removeparty"])
def test_yetki_yeni_ve_eski_adlarda_ayni(ui, conn, src, ad):
    src.oyuncu("72400", "Ali"); src.oyuncu("1", "A1"); src.parti("1244", "T", [("1", "A1")])
    arg = "1244" if "parti" in ad else "72400"
    # BİLİNÇLİ DEĞİŞİKLİK: gruplar artık hiçbir komutu işlemez -> grupta (yetkisiz/yetkili/KURUCU; onaylı/onaysız)
    # sessiz yok sayılır: yanıt yok, denetim kaydı yok, kaynak çağrısı yok, takip değişikliği yok.
    for kul in (YABANCI, YETKILI, KURUCU):
        for g in (G1, G3):
            assert run(ui.komut(gercek_grup_komut(ad, arg, grup=g, kul=kul))) is None
    assert run(ui.komut(grup_komut(ad, arg, kul=YABANCI))) is None
    assert takipte(conn, G1) == set() and src.cagrilar == []
    assert len(bildirimler(conn, "yetkisiz_deneme")) == 0
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 0
    # özel sohbette yetkili (kurucu değil) ve yabancı kullanamaz; kayıt düşer
    for kul in (YETKILI, YABANCI):
        y = run(ui.komut(komut(ad, arg, kul=kul)))
        assert y is not None and "Usage" not in y.metin and "added" not in y.metin
        assert src.cagrilar == []
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 2
    assert len(bildirimler(conn, "yetkisiz_deneme")) >= 1
    assert conn.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 0


def test_silme_onayi_baska_kullaniciya_veya_sohbete_bagli(ui, conn, src):
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("oyuncuekle", "72400")))
    # yetkili kullanıcı grupta silme başlatamaz (sessiz yok sayılır; BİLİNÇLİ tasarım değişikliği)
    assert run(ui.komut(grup_komut("oyuncusil", "72400", kul=YETKILI))) is None
    y = run(ui.komut(grup_komut("oyuncusil", "72400")))  # kurucu, özel sohbet + G1 listesi
    kid = onay_kimligi(y)
    # onay başka kullanıcıya bağlıdır: yetkili hiçbir yerden veremez
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))
    assert r.sessiz and not r.uyari_goster and not r.metin
    r = run(ui.buton(dugme(f"c:e:{kid}", kul=YETKILI)))
    assert r.uyari and r.sessiz and takipte(conn, G1) == {"72400"}
    # kurucu da başka kullanıcı adına açılmış bir onayı veremez
    baskasi = ui.islemler.olustur("oyuncu_sil", KURUCU, YETKILI, G1, {"id": "72400"})
    r = run(ui.buton(grup_dugme(f"c:e:{baskasi}", kul=KURUCU)))  # başka kullanıcı
    assert r.uyari and "Only the user who started" in r.uyari and takipte(conn, G1) == {"72400"}


# ------------------------------------------------------------------ ID/bağlantı çeşitleri komutlarda
@pytest.mark.parametrize("arg", [
    "72400",
    "https://diplomacia.com.tr/player/72400",
    "https://diplomacia.com.tr/profile/player/72400",
    "https://diplomacia.com.tr/press/player/72400",
    "diplomacia.com.tr/press/player/72400/",
])
@pytest.mark.parametrize("ad", ["addplayer", "oyuncuekle", "oyuncu_ekle"])
def test_oyuncuekle_tum_baglanti_cesitleri(ui, conn, src, ad, arg):
    src.oyuncu("72400", "Ali", "Ankara", "TR")
    assert "Player added to tracking" in run(ui.komut(grup_komut(ad, arg))).metin
    assert takipte(conn, G1) == {"72400"}
    # kullanıcıya gösterilen bağlantı kanonik ve tıklanabilir
    y = run(ui.komut(grup_komut("players")))
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
    assert "Party added to tracking" in y.metin and 'href="https://diplomacia.com.tr/party/1244"' in y.metin


@pytest.mark.parametrize("ad,arg,parca", [
    ("oyuncuekle", "https://diplomacia.com.tr/party/1244", "party link"),
    ("oyuncuekle", "https://diplomacia.com.tr/press/party/1244", "party link"),
    ("oyuncusil", "https://diplomacia.com.tr/profile/party/1244", "party link"),
    ("partiekle", "https://diplomacia.com.tr/player/72400", "player link"),
    ("partisil", "https://diplomacia.com.tr/press/player/72400", "player link"),
])
def test_yanlis_tur_url_acik_hata_ve_istek_yok(ui, conn, src, ad, arg, parca):
    y = run(ui.komut(grup_komut(ad, arg)))
    assert parca in y.metin and "Usage" in y.metin
    assert src.cagrilar == [] and takipte(conn, G1) == set()
    assert not veriler(y) or not any(v.startswith("c:e:") for v in veriler(y))


@pytest.mark.parametrize("ad", ["oyuncuekle", "oyuncusil", "partiekle", "partisil",
                                "addplayer", "removeplayer", "addparty", "removeparty"])
@pytest.mark.parametrize("arg", ["", "abc", "0", "-5", "072400", "1 2", "https://evil.example/player/72400",
                                 "https://diplomacia.com.tr.evil.example/player/72400",
                                 "https://diplomacia.com.tr@evil.example/player/1", "<b>1</b>",
                                 "https://diplomacia.com.tr/wars/war/5333"])
def test_gecersiz_girdi_reddedilir_istek_atilmaz(ui, conn, src, ad, arg):
    y = run(ui.komut(grup_komut(ad, arg)))
    assert "Usage" in y.metin
    assert src.cagrilar == [] and takipte(conn, G1) == set()
    assert "<b>1</b>" not in y.metin  # girdi HTML olarak yansıtılmaz
