"""Ayrıştırıcı testleri: fixtures/diplomacia_ornek.json (kısaltılmış GERÇEK örnek). Ağ yok."""
import copy
import json
from pathlib import Path

import pytest

from dipbot.diplomacia import ayristir as a
from dipbot.kaynak import EksikVeri

FX = json.loads((Path(__file__).parent / "fixtures" / "diplomacia_ornek.json").read_text(encoding="utf-8"))


def oy():
    return copy.deepcopy(FX["player_response"])


def pa():
    return copy.deepcopy(FX["party_response"])


def uy():
    return copy.deepcopy(FX["party_members_response"])


def test_oyuncu_fixture_dogru_ayristirilir():
    r = a.oyuncu_ayristir(oy(), "72400")
    assert r.anlik.oyuncu_id == "72400"
    assert r.anlik.ad == "Ayetullah Mutahhari"
    assert r.anlik.bolge == "Horasan"
    assert r.anlik.vatandaslik_ulkesi == "Macaristan Sosyalist Cumhuriyeti."
    assert r.uuid == "56b0a187-62fa-4e63-a459-f379a7a9a563"
    assert r.vatandaslik_ulkesi_id == "395543d8-c980-4c62-acf8-58e75d70a00d"


def test_display_name_ad_olarak_kullanilmaz_parti_etiketi_degisse_ad_ayni():
    # Parti değişince display_name "[BASKA] ..." olur; ad değişmemeli (sahte 'ad değişti' olmaz).
    g1, g2 = oy(), oy()
    g2["player"]["display_name"] = "[BASKA] Ayetullah Mutahhari"
    assert a.oyuncu_ayristir(g1, "1").anlik.ad == a.oyuncu_ayristir(g2, "1").anlik.ad == "Ayetullah Mutahhari"
    # display_name tamamen çıkarılsa da sorun değil (kullanılmıyor)
    del g2["player"]["display_name"]
    assert a.oyuncu_ayristir(g2, "1").anlik.ad == "Ayetullah Mutahhari"


def test_country_alanlari_vatandaslik_icin_kullanilmaz():
    g = oy()
    g["player"]["country_name"] = "BASKA ULKE"
    assert a.oyuncu_ayristir(g, "1").anlik.vatandaslik_ulkesi == "Macaristan Sosyalist Cumhuriyeti."


def test_username_strip():
    g = oy()
    g["player"]["username"] = "  Ali  "
    assert a.oyuncu_ayristir(g, "1").anlik.ad == "Ali"


@pytest.mark.parametrize("deger", ["", "   ", None, 5, ["x"]])
def test_username_bos_veya_gecersiz_eksikveri(deger):
    g = oy()
    g["player"]["username"] = deger
    with pytest.raises(EksikVeri):
        a.oyuncu_ayristir(g, "1")


def test_username_anahtari_yok():
    g = oy()
    del g["player"]["username"]
    with pytest.raises(EksikVeri):
        a.oyuncu_ayristir(g, "1")


@pytest.mark.parametrize("anahtar", ["province_name", "citizenship_country_name", "citizenship_country_id"])
@pytest.mark.parametrize("deger", [None, "", "  "])
def test_bolge_vatandaslik_null_veya_bos_none_olur(anahtar, deger):
    g = oy()
    g["player"][anahtar] = deger
    r = a.oyuncu_ayristir(g, "1")
    alan = {
        "province_name": r.anlik.bolge,
        "citizenship_country_name": r.anlik.vatandaslik_ulkesi,
        "citizenship_country_id": r.vatandaslik_ulkesi_id,
    }[anahtar]
    assert alan is None
    assert r.anlik.ad == "Ayetullah Mutahhari"  # diğer alanlar etkilenmez


@pytest.mark.parametrize("anahtar", ["province_name", "citizenship_country_name"])
def test_bolge_vatandaslik_anahtari_hic_yoksa_eksikveri(anahtar):
    g = oy()
    del g["player"][anahtar]
    with pytest.raises(EksikVeri):
        a.oyuncu_ayristir(g, "1")


@pytest.mark.parametrize("anahtar", ["province_name", "citizenship_country_name"])
def test_bolge_vatandaslik_tur_yanlis(anahtar):
    g = oy()
    g["player"][anahtar] = 123
    with pytest.raises(EksikVeri):
        a.oyuncu_ayristir(g, "1")


@pytest.mark.parametrize("govde", [None, [], "x", {}, {"player": None}, {"player": []}, {"player": "x"}])
def test_oyuncu_govde_bozuk(govde):
    with pytest.raises(EksikVeri):
        a.oyuncu_ayristir(govde, "1")


@pytest.mark.parametrize("deger", [None, 5, "uuid-degil", ""])
def test_oyuncu_id_zorunlu_uuid(deger):
    g = oy()
    g["player"]["id"] = deger
    with pytest.raises(EksikVeri):
        a.oyuncu_ayristir(g, "1")
    g = oy()
    del g["player"]["id"]
    with pytest.raises(EksikVeri):
        a.oyuncu_ayristir(g, "1")


def test_fazladan_alanlar_yok_sayilir():
    g = oy()
    g["player"]["yeni_alan"] = {"x": 1}
    g["ekstra"] = 1
    assert a.oyuncu_ayristir(g, "72400").anlik.ad == "Ayetullah Mutahhari"


# ---- parti ----
def test_parti_fixture():
    b = a.parti_ayristir(pa())
    assert (b.parti_id, b.party_number, b.ad, b.member_count) == ("5830", 5830, "TEKNO MERKEZ PARTİSİ", 2)
    assert b.uuid == "f5248460-488a-4750-92a7-4ad55fd80a52"


@pytest.mark.parametrize("anahtar", ["id", "party_number", "name", "member_count"])
def test_parti_zorunlu_anahtar_eksik(anahtar):
    g = pa()
    del g["party"][anahtar]
    with pytest.raises(EksikVeri):
        a.parti_ayristir(g)


@pytest.mark.parametrize(
    "anahtar,deger",
    [("party_number", "5830"), ("party_number", True), ("member_count", "2"), ("member_count", -1),
     ("member_count", 2.0), ("name", ""), ("name", None), ("id", 7)],
)
def test_parti_tur_yanlis(anahtar, deger):
    g = pa()
    g["party"][anahtar] = deger
    with pytest.raises(EksikVeri):
        a.parti_ayristir(g)


def test_parti_govde_bozuk():
    for g in (None, {}, {"party": None}, []):
        with pytest.raises(EksikVeri):
            a.parti_ayristir(g)


# ---- üyeler ----
def test_uyeler_fixture_tam():
    u = a.uyeler_ayristir(uy(), 2)
    assert u.tamam is True
    assert [(x.oyuncu_id, x.ad) for x in u.uyeler] == [("76737", "BATUMLU"), ("72400", "Ayetullah Mutahhari")]
    assert u.uuidler["72400"] == "56b0a187-62fa-4e63-a459-f379a7a9a563"


def test_uye_adi_display_name_degil():
    u = a.uyeler_ayristir(uy(), 2)
    assert all(not x.ad.startswith("[") for x in u.uyeler)


def test_uye_sayisi_uyusmazligi_tamam_false():
    assert a.uyeler_ayristir(uy(), 3).tamam is False
    assert a.uyeler_ayristir(uy(), 1).tamam is False


def test_bos_liste_ve_sifir_uye_tamam():
    assert a.uyeler_ayristir({"members": []}, 0).tamam is True
    assert a.uyeler_ayristir({"members": []}, 2).tamam is False


def test_mukerrer_numara_tamam_false():
    g = uy()
    g["members"].append(copy.deepcopy(g["members"][0]))
    u = a.uyeler_ayristir(g, 3)
    assert u.tamam is False and "mükerrer" in u.neden


def test_mukerrer_uuid_tamam_false():
    g = uy()
    g["members"][1]["player_id"] = g["members"][0]["player_id"]
    assert a.uyeler_ayristir(g, 2).tamam is False


@pytest.mark.parametrize(
    "degisim",
    [
        lambda m: m.pop("player_id"),
        lambda m: m.pop("player_number"),
        lambda m: m.pop("username"),
        lambda m: m.update(username="  "),
        lambda m: m.update(player_number="72400"),
        lambda m: m.update(player_id="bozuk"),
    ],
)
def test_bozuk_uye_tum_liste_eksik_sessiz_atlama_yok(degisim):
    g = uy()
    degisim(g["members"][1])
    with pytest.raises(EksikVeri):
        a.uyeler_ayristir(g, 2)


def test_uye_nesne_degil():
    g = uy()
    g["members"][0] = "x"
    with pytest.raises(EksikVeri):
        a.uyeler_ayristir(g, 2)


@pytest.mark.parametrize("govde", [None, {}, {"members": None}, {"members": {}}, []])
def test_uyeler_govde_bozuk(govde):
    with pytest.raises(EksikVeri):
        a.uyeler_ayristir(govde, 0)


@pytest.mark.parametrize("anahtar", ["next", "next_page", "page", "has_more", "total", "cursor", "offset"])
def test_sayfalama_anahtari_tamam_false(anahtar):
    g = uy()
    g[anahtar] = 1
    u = a.uyeler_ayristir(g, 2)  # sayı eşleşse bile
    assert u.tamam is False and "sayfalama" in u.neden


def test_loyal_ic_count_sayfalama_sayilmaz():
    # fixture'daki "loyal":{"count":..} iç içe; üst düzey değil -> tamam kalır
    assert a.uyeler_ayristir(uy(), 2).tamam is True


def test_birlestir_bayrak_adi_uyeler_tamam():
    p = a.parti_birlestir(a.parti_ayristir(pa()), a.uyeler_ayristir(uy(), 3))
    assert p.parti_id == "5830" and p.uyeler_tamam is False and len(p.uyeler) == 2
