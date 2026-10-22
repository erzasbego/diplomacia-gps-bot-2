"""OyuncuDogrulayici (Tur A: GET /api/players/72400, gövde ayrıştırılır; asıl testler test_tur_a.py). MOCK; canlı DEĞİL.

403 anlamı DOĞRULANMADI: 403 artık 'geçersiz' değil 'doğrulanamadı' (hata)."""
import asyncio
import logging

import httpx

import json
from pathlib import Path

from dipbot.diplomacia.dogrulayici import OyuncuDogrulayici as ProfilDogrulayici
from dipbot.diplomacia.istemci import OrtamKimlik
from dipbot.tg.token import TokenDeposu

ORTAM = {"DIPLOMACIA_AUTH_BASLIK": "Authorization", "DIPLOMACIA_AUTH_SEMA": "Bearer"}
OYUNCU = json.loads((Path(__file__).parent / "fixtures" / "diplomacia_ornek.json").read_text(encoding="utf-8"))["player_response"]
ADAY = "ADAY-TOKEN-XYZ"
ESKI = "ESKI-TOKEN-ABC"


def run(c):
    return asyncio.run(c)


def kur(tmp_path, isleyici, ortam=ORTAM):
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz(ESKI)
    return ProfilDogrulayici(OrtamKimlik(depo, ortam), transport=httpx.MockTransport(isleyici)), depo


def test_200_gecerli_yalniz_aday_token_ve_dosya_degismez(tmp_path):
    istekler = []

    def h(req):
        istekler.append(req)
        return httpx.Response(200, json=OYUNCU)

    d, depo = kur(tmp_path, h)
    s = run(d.dogrula(ADAY))
    assert s.gecerli and s.durum == "gecerli"
    assert len(istekler) == 1 and istekler[0].method == "GET"
    assert str(istekler[0].url) == "https://diplomacia.com.tr/api/players/72400"
    assert istekler[0].headers["Authorization"] == f"Bearer {ADAY}"
    assert depo.oku() == ESKI  # mevcut dosyaya dokunulmadı


def test_401_gecersiz_403_dogrulanamadi(tmp_path):
    d, _ = kur(tmp_path, lambda r: httpx.Response(401))
    s = run(d.dogrula(ADAY))
    assert s.durum == "gecersiz" and not s.gecerli
    d, _ = kur(tmp_path, lambda r: httpx.Response(403))
    s = run(d.dogrula(ADAY))
    assert s.durum == "hata" and not s.gecerli


def test_diger_kodlar_ve_ag_hatasi_dogrulanamadi(tmp_path):
    for kod in (404, 429, 500, 302):
        d, _ = kur(tmp_path, lambda r, k=kod: httpx.Response(k))
        assert run(d.dogrula(ADAY)).durum == "hata"

    def patla(req):
        raise httpx.ConnectError(f"baglanti {ADAY}")

    d, _ = kur(tmp_path, patla)
    s = run(d.dogrula(ADAY))
    assert s.durum == "hata" and ADAY not in s.aciklama

    def zaman(req):
        raise httpx.ReadTimeout("t")

    d, _ = kur(tmp_path, zaman)
    assert run(d.dogrula(ADAY)).durum == "hata"


def test_kimlik_ortami_ayarsizsa_istek_atilmaz(tmp_path):
    istekler = []
    d, _ = kur(tmp_path, lambda r: istekler.append(r) or httpx.Response(200), ortam={"DIPLOMACIA_AUTH_BASLIK": "Bad Header"})
    s = run(d.dogrula(ADAY))
    assert s.durum == "yapilandirilmadi" and istekler == []
    assert d.yapilandirildi is False


def test_gecersiz_token_bicimi_istek_atmaz(tmp_path):
    istekler = []
    d, _ = kur(tmp_path, lambda r: istekler.append(r) or httpx.Response(200))
    for kotu in ("a b", "a\r\nX: y", "", "çğ"):
        assert run(d.dogrula(kotu)).durum == "gecersiz"
    assert istekler == []


def test_token_loga_ve_aciklamaya_girmez(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    d, _ = kur(tmp_path, lambda r: httpx.Response(401, text=ADAY))
    s = run(d.dogrula(ADAY))
    assert ADAY not in s.aciklama and ADAY not in caplog.text and ADAY not in repr(d)


def test_arayuz_token_akisi_basarisizsa_degistirmez(conn, saat, tmp_path):
    from conftest import KURUCU
    from dipbot.kaynak import SinirliKaynak
    from dipbot.tg.arayuz import Arayuz
    from dipbot.tg.kaynak_yok import YapilandirilmamisKaynak

    d, depo = kur(tmp_path, lambda r: httpx.Response(503))
    ui = Arayuz(conn, YapilandirilmamisKaynak(), KURUCU, saat=saat, dogrulayici=d, token_deposu=depo)
    from dipbot.tg.modeller import Olay
    o = Olay(sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU, metin=ADAY)
    y = run(ui._token_isle(o))
    assert "NOT CHANGED" in y.metin and depo.oku() == ESKI


def test_calistir_dogrulayici_olustur(tmp_path):
    from dipbot.tg import calistir as c
    from dipbot.tg.token import YapilandirilmamisDogrulayici
    assert isinstance(c.dogrulayici_olustur(tmp_path / "t", ORTAM), ProfilDogrulayici)
    assert isinstance(c.dogrulayici_olustur(tmp_path / "t", {}), ProfilDogrulayici)  # varsayılan Bearer
    assert isinstance(c.dogrulayici_olustur(tmp_path / "t", {"DIPLOMACIA_AUTH_BASLIK": "Bad Header"}), YapilandirilmamisDogrulayici)
