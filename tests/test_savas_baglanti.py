"""Savaş adaptörünün bağlanması: istemci, bağlantı yardımcısı, tg etiketleri, tarama (iki ardışık görüntü).
MOCK (httpx.MockTransport + fixture kopyaları); canlı doğrulama DEĞİLDİR.
Savaş sayfası bağlantısı (/wars/war/{war_number}) KULLANICI tarafından doğrulandı; kimlik başlığı biçimi DOĞRULANMADI."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from conftest import G1, KURUCU, run
from fakes import SahteKaynak
from tg_fakes import grup_dugme, grup_komut
from test_diplomacia_istemci import Sahte, kur, tam_yanitlar

from dipbot import yonetim
from dipbot.diplomacia.savas import savas_listesine_cevir, savaslari_ayristir
from dipbot.kaynak import (
    EksikVeri, SavasDetay, SavasOzet, SavasTaraf, TokenGecersiz, VarlikBulunamadi,
)
from dipbot.tg.arayuz import Arayuz
from dipbot.tg.bicim import bildirim_metni, savas_baglantisi

FX = Path(__file__).parent / "fixtures"
F1 = json.loads((FX / "savaslar_ornek.json").read_text(encoding="utf-8"))
F2 = json.loads((FX / "savaslar_ornek_2.json").read_text(encoding="utf-8"))
UUID5333 = "a49da765-099f-44b8-bd51-c0f12a7ee1ac"


# ---- bağlantı yardımcısı ----
def test_savas_baglantisi_tam_eslesme():
    assert savas_baglantisi(5333) == "https://diplomacia.com.tr/wars/war/5333"


@pytest.mark.parametrize("kotu", [None, "5333", True, False, 0, -1, 5.0, "../x", [5333], "5333/../x"])
def test_savas_baglantisi_gecersiz_girdi_none(kotu):
    assert savas_baglantisi(kotu) is None


# ---- istemci ----
def _wars(govde):
    return {"/api/wars": httpx.Response(200, json=govde)}


def test_list_active_wars_istemci_alanlar(tmp_path):
    s = Sahte(_wars(F1))
    k = kur(tmp_path, s)
    liste = run(k.list_active_wars())
    assert liste.tamam and len(liste.savaslar) == 5
    assert str(s.istekler[0].url) == "https://diplomacia.com.tr/api/wars"
    o = {x.savas_id: x for x in liste.savaslar}[UUID5333]
    assert (o.war_number, o.durum, o.tur) == (5333, "active", "standard")
    assert (o.attacker_power, o.defender_power) == (28552, 46567)
    assert o.durum_taninmiyor is False and o.attacker_name


def test_list_active_wars_hata_esleme(tmp_path):
    with pytest.raises(TokenGecersiz):
        run(kur(tmp_path, Sahte(_wars(F1) | {"/api/wars": httpx.Response(401)})).list_active_wars())
    with pytest.raises(EksikVeri):
        run(kur(tmp_path, Sahte({"/api/wars": httpx.Response(200, text="json degil")})).list_active_wars())
    with pytest.raises(EksikVeri):
        run(kur(tmp_path, Sahte({"/api/wars": httpx.Response(200, json={"wars": [{"id": ""}]})})).list_active_wars())


def test_kimlik_yoksa_savas_istegi_atilmaz(tmp_path):
    from dipbot.diplomacia.istemci import KimlikYapilandirilmadi
    s = Sahte(_wars(F1))
    with pytest.raises(KimlikYapilandirilmadi):
        run(kur(tmp_path, s, ortam={"DIPLOMACIA_AUTH_BASLIK": "Bad Header"}).list_active_wars())
    assert s.istekler == []


def test_get_war_detail_listeden_bulur_bulunamazsa_aktif_listede_yok(tmp_path):
    k = kur(tmp_path, Sahte(_wars(F1)))
    d = run(k.get_war_detail(UUID5333))
    assert d.savas_id == UUID5333 and d.durum == "active"
    assert (d.taraflar[0].guc, d.taraflar[1].guc) == (28552, 46567)
    assert all(t.hasar is None for t in d.taraflar)  # güç != hasar: hasar'a YAZILMAZ
    with pytest.raises(VarlikBulunamadi) as e:
        run(k.get_war_detail("00000000-0000-4000-8000-000000000000"))
    assert "aktif listede yok" in str(e.value) and "silindi" not in str(e.value).lower()


# ---- tg ----
@pytest.fixture
def ui(conn, src, saat):
    return Arayuz(conn, src, KURUCU, saat=saat)


def test_liste_baglanti_etiket_ve_kacis(ui, src):
    src.savaslar = [
        SavasOzet(UUID5333, None, durum="preparing", tur="rebellion", war_number=5333,
                  attacker_name="<b>Ali</b> & Ortakları", defender_name="مملكة"),
        SavasOzet("id-2", "Adlı <Savaş>", durum="active", war_number=7),
        SavasOzet("id-3", None, durum="yeni<durum>", durum_taninmiyor=True, tur="x&y", tur_taninmiyor=True,
                  war_number=None, attacker_name="A", defender_name="B"),
        SavasOzet("id-4", None, war_number=True),  # yanlış tür: bağlantı yok
    ]
    m = run(ui.komut(grup_komut("savaslar"))).metin
    assert 'href="https://diplomacia.com.tr/wars/war/5333"' in m
    # BİLİNÇLİ DEĞİŞİKLİK (sade liste): 'Saldıran – Savunan · Durum · #numara'; tür yalnız emoji (ayrıntıda metin)
    assert "&lt;b&gt;Ali&lt;/b&gt; &amp; Ortakları – مملكة</a> · Preparing · #5333" in m
    assert "🔥" in m and "Rebellion" not in m  # ayaklanma = 🔥
    assert "Preparing" in m and "Active" in m
    assert 'href="https://diplomacia.com.tr/wars/war/7"' in m and "Adlı &lt;Savaş&gt;" in m
    assert "yeni&lt;durum&gt;" in m and "yeni<durum>" not in m
    assert m.count("wars/war/") == 2  # yalnız geçerli numaralılar
    assert 'href="https://diplomacia.com.tr/wars"' in m  # liste bağlantısı kalır
    assert UUID5333 not in m.split("wars/war/5333")[1][:5]  # id ile numara karışmaz


def test_detay_guc_none_ve_sifir_ayri_ve_baglanti(ui, src):
    src.savas_detay["s1"] = SavasDetay(
        "s1", None, "preparing",
        (SavasTaraf("A", guc=1234567), SavasTaraf("B", guc=None), SavasTaraf("C", guc=0)),
        war_number=5333, tur="independence",
    )
    m = run(ui.buton(grup_dugme("sd:s1"))).metin
    assert 'href="https://diplomacia.com.tr/wars/war/5333"' in m
    assert "Status: ⏳ Preparing" in m and "Independence" in m and m.startswith("🏳️")
    assert "A — Power: 1,234,567" in m and "B — Power: no data" in m and "C — Power: 0" in m
    # "damage" may appear only in the C1c caveat ("not been verified that the 'Power' value represents damage").
    assert "damage" not in m.lower().replace("represents damage", "")
    assert "has not been verified that the 'power' value represents damage" in m.lower()


def test_yeni_savas_bildirimi_baglanti_ve_kacis():
    m = bildirim_metni("yeni_savas", {"savas_id": UUID5333, "ad": None, "war_number": 5333,
                                      "attacker_name": "<i>X</i>", "defender_name": "Y", "durum": "preparing",
                                      "tespit": "2026-10-19T12:00:00.000000Z"})
    assert 'href="https://diplomacia.com.tr/wars/war/5333"' in m and "&lt;i&gt;X&lt;/i&gt;" in m
    assert "Preparing" in m and "damage" not in m.lower()
    m2 = bildirim_metni("yeni_savas", {"savas_id": UUID5333, "ad": "Ad", "war_number": None})
    assert "wars/war/" not in m2 and "Ad" in m2


# ---- tarama: iki ardışık görüntü ----
def _kuyruk(conn):
    return [json.loads(r["veri"]) for r in conn.execute("SELECT veri FROM outbox WHERE tur='yeni_savas' ORDER BY id")]


def _yukle(src, govde):
    sl = savas_listesine_cevir(savaslari_ayristir(govde))
    src.savaslar, src.savas_tamam = list(sl.savaslar), sl.tamam


def test_iki_ardisik_goruntu_guc_ve_durum_bildirim_uretmez(conn, servis, src):
    ilk = copy.deepcopy(F1)
    w5334 = copy.deepcopy(next(w for w in F2["wars"] if w["war_number"] == 5334))
    w5334.update(status="preparing", attacker_power=0, defender_power=0)
    ilk["wars"].append(w5334)
    _yukle(src, ilk)
    r1 = run(servis.savaslari_tara(G1, src))
    assert r1.ok and _kuyruk(conn) == []  # ilk tarama bildirimsiz baseline
    idler1 = {r[0] for r in conn.execute("SELECT savas_id FROM savaslar WHERE group_id=?", (G1,))}
    _yukle(src, F2)  # 5333/5324 gücü arttı, 5334 preparing -> active
    r2 = run(servis.savaslari_tara(G1, src))
    assert r2.ok and _kuyruk(conn) == []
    idler2 = {r[0] for r in conn.execute("SELECT savas_id FROM savaslar WHERE group_id=?", (G1,))}
    assert idler2 == idler1 and w5334["id"] in idler2


def test_19_savaslik_ilk_etkinlestirme_yagmur_uretmez_yeni_id_tek_bildirim(conn, servis, src):
    wars = []
    for i in range(19):
        w = copy.deepcopy(F1["wars"][1])
        w["id"] = f"00000000-0000-4000-8000-{i:012d}"
        w["war_number"] = 5000 + i
        wars.append(w)
    _yukle(src, {"wars": wars})
    assert run(servis.savaslari_tara(G1, src)).ok and _kuyruk(conn) == []
    yeni = copy.deepcopy(wars[0])
    yeni.update(id="ffffffff-0000-4000-8000-000000000001", war_number=5400, war_name=None)
    _yukle(src, {"wars": wars + [yeni]})
    assert run(servis.savaslari_tara(G1, src)).ok
    k = _kuyruk(conn)
    assert len(k) == 1 and k[0]["savas_id"] == yeni["id"] and k[0]["war_number"] == 5400
    run(servis.savaslari_tara(G1, src))
    assert len(_kuyruk(conn)) == 1  # tekrar taramada çift yok
