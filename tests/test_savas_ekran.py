"""Sade savaş listesi, ayrıntı ekranı, emoji tutarlılığı ve Telegram sınırları. MOCK."""
import re

from conftest import run
from tg_fakes import KURUCU, grup_dugme, grup_komut, veriler

import pytest

from dipbot import yonetim
from dipbot.kaynak import SavasDetay, SavasOzet, SavasTaraf
from dipbot.tg import bicim
from dipbot.tg.arayuz import SAYFA_BOYUTU, Arayuz
from dipbot.tg.modeller import CALLBACK_AZAMI_BAYT


@pytest.fixture
def ui(conn, src, saat):
    return Arayuz(conn, src, KURUCU, saat=saat)


def _s(i, tur="standard", durum="active", sad="A", sav="B"):
    return SavasOzet(f"id-{i}", None, durum=durum, tur=tur, war_number=1000 + i, attacker_name=sad, defender_name=sav)


def test_liste_yalniz_temel_bilgi_saldiran_savunan_durum_numara(ui, src):
    src.savaslar = [_s(1, sad="Ali", sav="Veli")]
    m = run(ui.komut(grup_komut("savaslar"))).metin
    satir = [l for l in m.splitlines() if l.startswith("1.")][0]
    assert satir == '1. ⚔️ <a href="https://diplomacia.com.tr/wars/war/1001">Ali – Veli</a> · Active · #1001'
    for yasak in ("Power", "Standard", "Type:"):
        assert yasak not in satir


def test_emoji_anlamlari_tutarli_liste_ve_detay_ve_bildirim(ui, src):
    src.savaslar = [_s(1, "standard"), _s(2, "rebellion"), _s(3, "independence"), _s(4, None), _s(5, "yeni_tur")]
    m = run(ui.komut(grup_komut("savaslar"))).metin
    sat = {int(l[0]): l for l in m.splitlines() if re.match(r"^\d\.", l)}
    assert "⚔️" in sat[1] and "🔥" in sat[2] and "🏳️" in sat[3]
    assert "⚔️" in sat[4] and "⚔️" in sat[5], "tür yok/tanınmıyor: genel savaş emojisi"
    for l in sat.values():  # satır başına TEK tür emojisi
        assert sum(l.count(e) for e in ("⚔️", "🔥", "🏳️", "🛡️")) == 1, l
    assert bicim.SAVAS_EMOJI_LEJANDI in m
    assert bicim.tur_emoji("rebellion") == "🔥" and bicim.tur_emoji("independence") == "🏳️"
    assert bicim.durum_emoji("preparing") == "⏳" and bicim.durum_emoji("active") == "🟢" and bicim.durum_emoji("x") == "❔"
    # bildirim başlığı da aynı emoji sözlüğünü kullanır
    assert bicim.bildirim_metni("yeni_savas", {"savas_id": "i", "tur": "independence", "war_number": 5}).count("🏳️") >= 1


def test_detay_kapsamli_bilgi_ve_saldiran_savunan_rozetleri(ui, src):
    src.savas_detay["s1"] = SavasDetay("s1", None, "active", (SavasTaraf("A", 100), SavasTaraf("B", None)),
                                      war_number=77, tur="rebellion")
    m = run(ui.buton(grup_dugme("sd:s1"))).metin
    assert m.startswith("🔥 <b>War</b>")
    assert "Status: 🟢 Active" in m and "Type: Rebellion" in m
    assert "⚔️ Attacker: A — Power: 100" in m and "🛡️ Defender: B — Power: no data" in m
    assert 'href="https://diplomacia.com.tr/wars/war/77"' in m


def test_dugmeler_olculu_emojili_ve_sinirlar_icinde(ui, src):
    src.savaslar = [_s(i, sad="Çok Uzun Bir Saldıran Taraf Adı " * 3, sav="Savunan " * 10) for i in range(1, 30)]
    y = run(ui.komut(grup_komut("savaslar")))
    assert len(y.metin) <= bicim.MESAJ_AZAMI
    for satir in y.klavye:
        for d in satir:
            assert len(d.etiket) <= 64
            if d.veri is not None:
                assert len(d.veri.encode()) <= CALLBACK_AZAMI_BAYT
            ilk_emoji_sayisi = sum(1 for c in d.etiket if ord(c) > 0x2000 and c not in "‹›–—…")
            assert ilk_emoji_sayisi <= 3, d.etiket  # ölçülü: etiket başına bir emoji (+varyasyon işareti)
    ana = run(ui.komut(grup_komut("start")))
    etiketler = [d.etiket for satir in ana.klavye for d in satir]
    assert "👤 Players" in etiketler and "⚔️ Wars" in etiketler and "🏠" not in "".join(etiketler)


def test_uzun_adlar_kisaltilir_html_kacisli(ui, src):
    src.savaslar = [SavasOzet("i1", None, durum="active", war_number=9, attacker_name="<b>" + "x" * 200, defender_name="y&z")]
    m = run(ui.komut(grup_komut("savaslar"))).metin
    assert "<b>x" not in m and "&lt;b&gt;" in m and "…" in m and "y&amp;z" in m
    assert len(m) < 1200


def test_adsiz_ve_taraf_bilgisiz_savas_yedekleri(ui, src):
    src.savaslar = [SavasOzet("uuid-0000-1111", None, war_number=5), SavasOzet("uuid-9999", None),
                    SavasOzet("uuid-adli", "Adlı Savaş", war_number=None)]
    m = run(ui.komut(grup_komut("savaslar"))).metin
    assert "War #5</a> · status unknown · #5" in m
    assert "uuid-9999" in m and "Adlı Savaş" in m and m.count("status unknown") == 3
