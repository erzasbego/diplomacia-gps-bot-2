"""/api/wars ayrıştırıcı testleri. MOCK: fixture gerçek yanıttan kısaltılmış kopyadır;
bu testler CANLI doğrulama DEĞİLDİR."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dipbot.diplomacia.savas import (
    savas_detayina_cevir,
    savas_listesine_cevir,
    savaslari_ayristir,
)
from dipbot.kaynak import EksikVeri

FIXTURE = Path(__file__).parent / "fixtures" / "savaslar_ornek.json"


@pytest.fixture
def govde():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def kayit(govde):
    return {k.savas_id: k for k in savaslari_ayristir(govde).kayitlar}


def _tek(**kw):
    w = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8"))["wars"][1])
    for k, v in kw.items():
        if v is _SIL:
            w.pop(k, None)
        else:
            w[k] = v
    return {"wars": [w]}


_SIL = object()
P = "5434c8dd-b2a1-4089-8c13-c20c6141912b"


def test_bes_savas_ayristirilir(govde):
    r = savaslari_ayristir(govde)
    assert r.tamam and len(r.kayitlar) == 5
    assert [k.war_number for k in r.kayitlar] == [5336, 5333, 5330, 5324, 5301]
    assert [k.durum for k in r.kayitlar] == ["preparing", "active", "active", "active", "active"]
    assert [k.tur for k in r.kayitlar] == ["standard", "standard", "rebellion", "standard", "independence"]
    assert not any(k.durum_taninmiyor or k.tur_taninmiyor for k in r.kayitlar)


def test_alanlar_dogru(kayit):
    k = kayit["a49da765-099f-44b8-bd51-c0f12a7ee1ac"]
    assert k.attacker_name == "Sakson-Latin Birleşik Krallığı"
    assert k.defender_province == "Adana"
    assert k.attacker_power == 28552 and k.defender_power == 46567
    assert k.war_goals == ("fetih",) and k.is_conquest is True
    assert k.war_name == "Tutulmayan Sözler"


def test_rebellion_ve_independence(kayit):
    r = kayit["487ee923-548a-48d2-80bb-470c97dc1012"]
    assert r.attacker_id == r.defender_id and r.war_goals == () and r.is_conquest is False
    i = kayit[P]
    assert i.attacker_id is None and i.defender_id and i.attacker_name == "Bağımsız Bölge"


def test_war_name_null_ve_dolu(kayit):
    assert kayit["67b3d5a3-36ce-4548-b08a-ea2ef93e6daf"].war_name is None
    assert kayit["a49da765-099f-44b8-bd51-c0f12a7ee1ac"].war_name == "Tutulmayan Sözler"
    assert savaslari_ayristir(_tek(war_name="  ")).kayitlar[0].war_name is None


def test_power_sifir_anahtar_yok_null_ayrimi(kayit):
    assert kayit["67b3d5a3-36ce-4548-b08a-ea2ef93e6daf"].attacker_power == 0  # 0 meşru
    assert kayit["a1210970-d226-4274-8442-6924f74ec8fc"].attacker_power == 0
    k = savaslari_ayristir(_tek(attacker_power=_SIL, defender_power=None)).kayitlar[0]
    assert k.attacker_power is None and k.defender_power is None
    for kotu in (True, "12", -1, 1.5):
        assert savaslari_ayristir(_tek(attacker_power=kotu)).kayitlar[0].attacker_power is None


def test_id_ve_war_number_ayri(govde):
    k = savaslari_ayristir(_tek(war_number=_SIL)).kayitlar[0]
    assert k.war_number is None and k.savas_id == "a49da765-099f-44b8-bd51-c0f12a7ee1ac"
    for kotu in ("5333", True, None, -3):
        assert savaslari_ayristir(_tek(war_number=kotu)).kayitlar[0].war_number is None
    k = savaslari_ayristir(_tek(war_number=7)).kayitlar[0]
    assert k.savas_id != "7" and k.war_number == 7


def test_bilinmeyen_durum_ve_tur_korunur_ve_isaretlenir():
    r = savaslari_ayristir(_tek(status="ended", war_type="yeni_tur"))
    k = r.kayitlar[0]
    assert r.tamam and k.durum == "ended" and k.durum_taninmiyor
    assert k.tur == "yeni_tur" and k.tur_taninmiyor


@pytest.mark.parametrize("anahtar_degeri", [{"id": _SIL}, {"id": ""}, {"id": "  "}, {"id": 5}, {"id": None},
                                             {"status": _SIL}, {"status": 3}, {"status": None}, {"status": ""}])
def test_bozuk_oge_tum_liste_hata(govde, anahtar_degeri):
    govde["wars"][2].update({k: v for k, v in anahtar_degeri.items() if v is not _SIL})
    for k, v in anahtar_degeri.items():
        if v is _SIL:
            govde["wars"][2].pop(k)
    with pytest.raises(EksikVeri):
        savaslari_ayristir(govde)


def test_oge_dict_degil(govde):
    govde["wars"].append("x")
    with pytest.raises(EksikVeri):
        savaslari_ayristir(govde)


def test_mukerrer_id(govde):
    govde["wars"].append(copy.deepcopy(govde["wars"][0]))
    with pytest.raises(EksikVeri):
        savaslari_ayristir(govde)


@pytest.mark.parametrize("anahtar", ["next", "next_page", "page", "has_more", "total", "cursor", "offset"])
def test_sayfalama_anahtari_tamam_false(govde, anahtar):
    govde[anahtar] = 2
    r = savaslari_ayristir(govde)
    assert r.tamam is False and anahtar in r.neden and len(r.kayitlar) == 5


def test_bos_liste_gecerli():
    r = savaslari_ayristir({"wars": []})
    assert r.tamam and r.kayitlar == ()


@pytest.mark.parametrize("govde_", [[], None, "x", {}, {"wars": None}, {"wars": {}}, {"wars": "x"}])
def test_yanlis_ust_duzey(govde_):
    with pytest.raises(EksikVeri):
        savaslari_ayristir(govde_)


def test_fazla_alan_yok_sayilir_ve_taninmaz_baglanti_uretilmez(govde):
    govde["wars"][0]["yeni_alan"] = {"x": 1}
    govde["bilinmeyen"] = 1
    r = savaslari_ayristir(govde)
    assert r.tamam
    assert not any("url" in f.lower() or "link" in f.lower() or "flag" in f.lower()
                   for f in r.kayitlar[0].__dataclass_fields__)


def test_tarih_ayristirma(kayit):
    k = kayit["67b3d5a3-36ce-4548-b08a-ea2ef93e6daf"]
    assert k.starts_at == datetime(2026, 10, 19, 20, 2, 43, 6000, tzinfo=timezone.utc)
    assert k.created_at < k.starts_at < k.ends_at
    k = savaslari_ayristir(_tek(starts_at="bozuk", ends_at=None, created_at=5)).kayitlar[0]
    assert k.starts_at is None and k.ends_at is None and k.created_at is None
    assert savaslari_ayristir(_tek(starts_at="2026-10-19T20:02:43")).kayitlar[0].starts_at is None  # tz yok


def test_cekirdek_tipine_donusum(govde):
    r = savaslari_ayristir(govde)
    sl = savas_listesine_cevir(r)
    assert sl.tamam and len(sl.savaslar) == 5
    assert sl.savaslar[1].savas_id == "a49da765-099f-44b8-bd51-c0f12a7ee1ac"
    assert sl.savaslar[1].ad == "Tutulmayan Sözler" and sl.savaslar[0].ad is None
    d = savas_detayina_cevir(r.kayitlar[1])
    assert d.durum == "active" and d.taraflar[0].guc == 28552
    sayfali = dict(govde, next="x")
    assert savas_listesine_cevir(savaslari_ayristir(sayfali)).tamam is False
