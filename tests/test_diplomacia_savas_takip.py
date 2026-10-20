"""Ayrıştırıcı çıktısıyla mevcut TakipServisi.savaslari_tara akışı. MOCK; canlı doğrulama DEĞİL.
Çekirdek DEĞİŞTİRİLMEDİ: yalnız SavasOzet(savas_id, ad) taşındığı için güç/durum değişimi
çekirdeğe hiç ulaşmaz; bu test bunu belgeler."""
import asyncio
import copy
import json
from pathlib import Path

from dipbot.diplomacia.savas import savas_listesine_cevir, savaslari_ayristir
from dipbot.kaynak import EksikVeri, SavasListesi

from conftest import G1

FIXTURE = Path(__file__).parent / "fixtures" / "savaslar_ornek.json"


def _govde():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _yukle(src, govde):
    sl = savas_listesine_cevir(savaslari_ayristir(govde))
    src.savaslar = list(sl.savaslar)
    src.savas_tamam = sl.tamam


def _tara(servis, src):
    return asyncio.run(servis.savaslari_tara(G1, src))


def _yeni(conn):
    return [json.loads(r["veri"])["savas_id"] for r in
            conn.execute("SELECT * FROM outbox WHERE tur='yeni_savas' ORDER BY id")]


def _yeni_savas(w0):
    w = copy.deepcopy(w0)
    w["id"] = "ffffffff-0000-4000-8000-000000000001"
    w["war_number"] = 5400
    return w


def test_baseline_sonra_yalniz_yeni_id(conn, servis, src):
    g = _govde()
    _yukle(src, g)
    assert _tara(servis, src).ok and _yeni(conn) == []
    g["wars"].append(_yeni_savas(g["wars"][1]))
    _yukle(src, g)
    assert _tara(servis, src).degisiklik == 1
    assert _yeni(conn) == ["ffffffff-0000-4000-8000-000000000001"]


def test_guc_ve_durum_degisimi_bildirim_uretmez(conn, servis, src):
    g = _govde()
    _yukle(src, g)
    _tara(servis, src)
    g["wars"][0]["status"] = "active"  # preparing -> active
    g["wars"][0]["attacker_power"] = 999
    g["wars"][1]["defender_power"] = None
    _yukle(src, g)
    assert _tara(servis, src).degisiklik == 0 and _yeni(conn) == []


def test_war_number_ayni_id_ayni_savas_ve_id_farkliysa_yeni(conn, servis, src):
    g = _govde()
    _yukle(src, g)
    _tara(servis, src)
    g["wars"][2]["war_number"] = 1  # numara değişti, id aynı: yeni değil
    _yukle(src, g)
    assert _tara(servis, src).degisiklik == 0
    g["wars"][2]["id"] = "aaaaaaaa-0000-4000-8000-000000000002"  # id değişti, numara aynı kalsa bile yeni
    _yukle(src, g)
    assert _tara(servis, src).degisiklik == 1


def test_tamam_false_basarili_tarama_degil(conn, servis, src):
    g = _govde()
    g["next"] = "x"
    _yukle(src, g)
    assert src.savas_tamam is False
    r = _tara(servis, src)
    assert not r.ok
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 0


def test_ayristirici_hatasi_basarili_tarama_degil(conn, servis, src):
    g = _govde()
    _yukle(src, g)
    _tara(servis, src)
    bozuk = _govde()
    bozuk["wars"][3]["id"] = ""
    try:
        savaslari_ayristir(bozuk)
        raise AssertionError("EksikVeri bekleniyordu")
    except EksikVeri as e:
        src.hatalar[("savaslar", None)] = e  # istemci bu hatayı fırlatır
    r = _tara(servis, src)
    assert not r.ok and _yeni(conn) == []


def test_bos_liste_ilk_taramada_baseline_tamamlamaz(conn, servis, src):
    _yukle(src, {"wars": []})
    assert isinstance(SavasListesi((), True), SavasListesi)
    r = _tara(servis, src)
    assert r.ok and r.uyarilar and _yeni(conn) == []
