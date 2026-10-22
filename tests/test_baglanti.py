"""Oyuncu/parti kimliği ve bağlantı ayrıştırma (güvenilir alan adı + beklenen yol yapısı). MOCK/saf fonksiyon."""
import pytest

from dipbot.tg.baglanti import BaglantiHatasi, kimlik_coz_ayrintili
from dipbot.tg.arayuz import kimlik_coz


@pytest.mark.parametrize("arg", [
    "72400",
    "https://diplomacia.com.tr/player/72400",
    "http://diplomacia.com.tr/player/72400",
    "https://www.diplomacia.com.tr/player/72400",
    "diplomacia.com.tr/player/72400",
    "www.diplomacia.com.tr/player/72400/",
    "https://diplomacia.com.tr/profile/player/72400",
    "https://diplomacia.com.tr/press/player/72400",
    "https://diplomacia.com.tr/press/player/72400?ref=x#bolum",
    "HTTPS://DIPLOMACIA.COM.TR/Profile/player/72400".replace("Profile", "profile"),
])
def test_oyuncu_kabul(arg):
    assert kimlik_coz_ayrintili(arg, "player") == "72400"
    assert kimlik_coz(arg, "player") == "72400"


@pytest.mark.parametrize("arg", [
    "1244",
    "https://diplomacia.com.tr/party/1244",
    "https://diplomacia.com.tr/profile/party/1244",
    "https://diplomacia.com.tr/press/party/1244/",
    "www.diplomacia.com.tr/press/party/1244",
])
def test_parti_kabul(arg):
    assert kimlik_coz_ayrintili(arg, "party") == "1244"


def test_kullanici_ornekleri():
    assert kimlik_coz("https://diplomacia.com.tr/profile/player/192", "player") == "192"
    assert kimlik_coz("https://diplomacia.com.tr/press/player/124796", "player") == "124796"


@pytest.mark.parametrize("arg", [
    "https://evil.example/player/72400",
    "https://diplomacia.com.tr.evil.example/player/72400",
    "https://evildiplomacia.com.tr/player/72400",
    "https://diplomacia.com.tr@evil.example/player/72400",
    "https://diplomacia.com.tr\\@evil.example/player/72400",
    "https://evil.example/https://diplomacia.com.tr/player/72400",
    "evil.example/diplomacia.com.tr/player/72400",
    "ftp://diplomacia.com.tr/player/72400",
    "javascript:alert(1)//diplomacia.com.tr/player/72400",
    "https://diplomacia.com.tr:8443/player/72400",
    "https://diplomacia.com.tr./player/72400",
    "https://user:pw@diplomacia.com.tr/player/72400",
    "https://diplomacia.com.tr/player/0",
    "https://diplomacia.com.tr/player/072400",
    "https://diplomacia.com.tr/player/-5",
    "https://diplomacia.com.tr/player/abc",
    "https://diplomacia.com.tr/player/72400/extra",
    "https://diplomacia.com.tr/player/72400x",
    "https://diplomacia.com.tr/player/1234567890123",  # 13 hane
    "https://diplomacia.com.tr/other/player/72400",
    "https://diplomacia.com.tr/profile/press/player/72400",
    "https://diplomacia.com.tr/profile/region/varna",
    "https://diplomacia.com.tr/profile/country/b0f1c3a4-0000-4000-8000-000000000000",
    "https://diplomacia.com.tr/wars/war/5333",
    "https://diplomacia.com.tr/player/%37%32400",
    "https://diplomacia.com.tr/player/72 400",
    "https://diplomacia.com.tr/",
    "diplomacia.com.tr",
    "00",
    "-1",
    "1.5",
    "72400abc",
    "<b>1</b>",
    "",
    "   ",
    "٣٢",  # Arap-Hint rakamları
])
def test_oyuncu_reddet(arg):
    with pytest.raises(BaglantiHatasi):
        kimlik_coz_ayrintili(arg, "player")
    assert kimlik_coz(arg, "player") is None


def test_tur_karismasi_acik_hata_verir():
    with pytest.raises(BaglantiHatasi) as e:
        kimlik_coz_ayrintili("https://diplomacia.com.tr/party/1244", "player")
    assert "party link" in str(e.value).lower() and "player link" in str(e.value).lower()
    with pytest.raises(BaglantiHatasi) as e:
        kimlik_coz_ayrintili("https://diplomacia.com.tr/press/player/72400", "party")
    assert "player link" in str(e.value).lower() and "party link" in str(e.value).lower()
    assert kimlik_coz("https://diplomacia.com.tr/profile/party/5", "player") is None


def test_hata_mesaji_girdiyi_yansitmaz():
    with pytest.raises(BaglantiHatasi) as e:
        kimlik_coz_ayrintili("<script>alert(1)</script>", "player")
    assert "script" not in str(e.value)
