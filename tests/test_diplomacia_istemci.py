"""İstemci testleri: httpx.MockTransport, AĞ YOK. Gerçek canlı doğrulama DEĞİLDİR.

`varsayim` içeren test adları DOĞRULANMAMIŞ standart-HTTP varsayımını sınar.
"""
import asyncio
import copy
import json
import logging
from pathlib import Path

import httpx
import pytest

from dipbot.diplomacia import istemci as ist
from dipbot.diplomacia.istemci import (
    GecersizNumara,
    IstemciKaynagi,
    KimlikYapilandirilmadi,
    OrtamKimlik,
    SavasSemasiDogrulanmadi,
    baglanti_ayristir,
)
from dipbot.kaynak import (
    AgZamanAsimi,
    DiplomaciaSource,
    EksikVeri,
    ErisimReddedildi,
    IstekSiniri,
    SinirliKaynak,
    SiteErisilemez,
    TokenGecersiz,
    TokenYok,
    VarlikBulunamadi,
)
from dipbot.tg.kaynak_yok import yapilandirildi_mi
from dipbot.tg.token import TokenDeposu

FX = json.loads((Path(__file__).parent / "fixtures" / "diplomacia_ornek.json").read_text(encoding="utf-8"))
GIZLI = "GIZLI-TOKEN-DEGERI-123"


def run(c):
    return asyncio.run(c)


class Sahte:
    """MockTransport işleyicisi: çağrı sayacı + kaydedilen istekler."""

    def __init__(self, yanitlar=None, varsayilan=None):
        self.yanitlar = yanitlar or {}
        self.varsayilan = varsayilan
        self.istekler: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.istekler.append(req)
        y = self.yanitlar.get(req.url.path, self.varsayilan)
        if isinstance(y, Exception):
            raise y
        if callable(y):
            return y(req)
        return y if y is not None else httpx.Response(404)


def tam_yanitlar():
    return {
        "/api/players/72400": httpx.Response(200, json=FX["player_response"]),
        "/api/parties/5830": httpx.Response(200, json=FX["party_response"]),
        "/api/parties/5830/members": httpx.Response(200, json=FX["party_members_response"]),
    }


def kur(tmp_path, sahte, *, ortam=None, token=GIZLI):
    depo = TokenDeposu(tmp_path / "token")
    if token:
        depo.yaz(token)
    if ortam is None:
        ortam = {"DIPLOMACIA_AUTH_BASLIK": "Authorization", "DIPLOMACIA_AUTH_SEMA": "Bearer"}
    k = IstemciKaynagi(OrtamKimlik(depo, ortam), transport=httpx.MockTransport(sahte))
    return k


# ---- başarılı yollar ----
def test_protokolu_karsilar(tmp_path):
    assert isinstance(kur(tmp_path, Sahte()), DiplomaciaSource)


def test_oyuncu_basarili_ve_baslik(tmp_path):
    s = Sahte(tam_yanitlar())
    r = run(kur(tmp_path, s).fetch_player("72400"))
    assert (r.oyuncu_id, r.ad, r.bolge, r.vatandaslik_ulkesi) == (
        "72400", "Ayetullah Mutahhari", "Horasan", "Macaristan Sosyalist Cumhuriyeti.")
    assert len(s.istekler) == 1
    assert str(s.istekler[0].url) == "https://diplomacia.com.tr/api/players/72400"
    assert s.istekler[0].headers["Authorization"] == f"Bearer {GIZLI}"


def test_baslik_sema_bos_ise_yalniz_token(tmp_path):
    s = Sahte(tam_yanitlar())
    run(kur(tmp_path, s, ortam={"DIPLOMACIA_AUTH_BASLIK": "X-Token", "DIPLOMACIA_AUTH_SEMA": ""}).fetch_player("72400"))
    assert s.istekler[0].headers["X-Token"] == GIZLI
    assert "Authorization" not in s.istekler[0].headers


def test_parti_basarili(tmp_path):
    s = Sahte(tam_yanitlar())
    p = run(kur(tmp_path, s).fetch_party("5830"))
    assert p.parti_id == "5830" and p.ad == "TEKNO MERKEZ PARTİSİ" and p.uyeler_tamam is True
    assert [u.oyuncu_id for u in p.uyeler] == ["76737", "72400"]
    assert [r.url.path for r in s.istekler] == ["/api/parties/5830", "/api/parties/5830/members"]


def test_parti_uye_sayisi_uyusmazligi_tamam_false(tmp_path):
    y = tam_yanitlar()
    pa = copy.deepcopy(FX["party_response"])
    pa["party"]["member_count"] = 5
    y["/api/parties/5830"] = httpx.Response(200, json=pa)
    p = run(kur(tmp_path, Sahte(y)).fetch_party("5830"))
    assert p.uyeler_tamam is False


def test_parti_numara_uyusmazligi_eksikveri(tmp_path):
    y = tam_yanitlar()
    y["/api/parties/999"] = y["/api/parties/5830"]
    with pytest.raises(EksikVeri):
        run(kur(tmp_path, Sahte(y)).fetch_party("999"))


def test_parti_bozuk_uye_eksikveri(tmp_path):
    y = tam_yanitlar()
    m = copy.deepcopy(FX["party_members_response"])
    del m["members"][0]["username"]
    y["/api/parties/5830/members"] = httpx.Response(200, json=m)
    with pytest.raises(EksikVeri):
        run(kur(tmp_path, Sahte(y)).fetch_party("5830"))


def test_uuid_tutarsizligi_eksikveri(tmp_path):
    y = tam_yanitlar()
    k = kur(tmp_path, Sahte(y))
    run(k.fetch_player("72400"))
    bozuk = copy.deepcopy(FX["player_response"])
    bozuk["player"]["id"] = "11111111-1111-4111-8111-111111111111"
    y["/api/players/72400"] = httpx.Response(200, json=bozuk)
    with pytest.raises(EksikVeri):
        run(k.fetch_player("72400"))


def test_parti_uye_uuid_oyuncu_uuidi_ile_tutarli_degilse_eksikveri(tmp_path):
    y = tam_yanitlar()
    k = kur(tmp_path, Sahte(y))
    run(k.fetch_player("72400"))
    m = copy.deepcopy(FX["party_members_response"])
    m["members"][1]["player_id"] = "11111111-1111-4111-8111-111111111111"
    y["/api/parties/5830/members"] = httpx.Response(200, json=m)
    with pytest.raises(EksikVeri):
        run(k.fetch_party("5830"))


# ---- numara doğrulama ----
@pytest.mark.parametrize("n", ["../x", "72400/../1", " 72400", "72400 ", "abc", "72a", "", "٧٢٤٠٠", "7240?x=1",
                               "1" * 13, "72400\n", None, 72400])
def test_numara_gecersiz_istek_atilmaz(tmp_path, n):
    s = Sahte(tam_yanitlar())
    k = kur(tmp_path, s)
    with pytest.raises(GecersizNumara):
        run(k.fetch_player(n))
    with pytest.raises(GecersizNumara):
        run(k.fetch_party(n))
    assert s.istekler == []


# ---- bağlantı ayrıştırıcı ----
@pytest.mark.parametrize("m,beklenen", [
    ("https://diplomacia.com.tr/player/72400", ("player", "72400")),
    ("https://diplomacia.com.tr/party/5830", ("party", "5830")),
    (" https://diplomacia.com.tr/party/5830 ", ("party", "5830")),
])
def test_baglanti_gecerli(m, beklenen):
    assert baglanti_ayristir(m) == beklenen


@pytest.mark.parametrize("m", [
    "http://diplomacia.com.tr/player/1", "https://www.diplomacia.com.tr/player/1",
    "https://diplomacia.com.tr.evil.com/player/1", "https://evil.com/diplomacia.com.tr/player/1",
    "https://diplomacia.com.tr@evil.com/player/1", "https://user@diplomacia.com.tr/player/1",
    "https://diplomacia.com.tr:8443/player/1", "https://diplomacia.com.tr/player/1/extra",
    "https://diplomacia.com.tr/player/1?x=1", "https://diplomacia.com.tr/player/1#a",
    "https://diplomacia.com.tr/player/abc", "https://diplomacia.com.tr/player/", "https://diplomacia.com.tr/war/1",
    "https://diplomacia.com.tr/player/../party/1", "72400", "", None, "javascript:alert(1)",
])
def test_baglanti_reddedilir(m):
    assert baglanti_ayristir(m) is None


# ---- kimlik yapılandırılmamış ----
@pytest.mark.parametrize("ortam", [
                                   {"DIPLOMACIA_AUTH_BASLIK": "Bad Header\r\nX"},
                                   {"DIPLOMACIA_AUTH_BASLIK": "Authorization", "DIPLOMACIA_AUTH_SEMA": "Be arer\r\n"}])
def test_kimlik_yapilandirilmamis_istek_atilmaz(tmp_path, ortam):
    s = Sahte(tam_yanitlar())
    k = kur(tmp_path, s, ortam=ortam)
    assert k.yapilandirildi is False and yapilandirildi_mi(k) is False
    with pytest.raises(KimlikYapilandirilmadi) as e:
        run(k.fetch_player("72400"))
    assert "invalid" in str(e.value)  # Tur A: varsayılan Bearer geçerli; yalnız GEÇERSİZ override yapılandırılmamış sayılır
    with pytest.raises(KimlikYapilandirilmadi):
        run(k.fetch_party("5830"))
    assert len(s.istekler) == 0
    assert GIZLI not in str(e.value)


def test_token_yok_istek_atilmaz_tokenyok(tmp_path):
    s = Sahte(tam_yanitlar())
    with pytest.raises(TokenYok):  # Tur A: TokenGecersiz (401) DEĞİL, yapılandırma sınıfı
        run(kur(tmp_path, s, token=None).fetch_player("72400"))
    assert s.istekler == []


def test_token_bozuk_karakter_istek_atilmaz(tmp_path):
    s = Sahte(tam_yanitlar())
    k = kur(tmp_path, s)
    (tmp_path / "token").write_bytes("abcçdef".encode("utf-8"))
    with pytest.raises(TokenYok):
        run(k.fetch_player("72400"))
    assert s.istekler == []


def test_yapilandirildi_true_kimlik_hazirsa(tmp_path):
    assert yapilandirildi_mi(kur(tmp_path, Sahte())) is True
    assert yapilandirildi_mi(SinirliKaynak(kur(tmp_path, Sahte()))) is True


# ---- durum kodu eşlemeleri ----
def test_401_tokengecersiz_dogrulandi(tmp_path):
    with pytest.raises(TokenGecersiz):
        run(kur(tmp_path, Sahte(varsayilan=httpx.Response(401, json={"error": "x"}))).fetch_player("1"))


def test_403_varsayim_erisim_reddedildi_tokengecersiz_degil(tmp_path):
    with pytest.raises(ErisimReddedildi):
        run(kur(tmp_path, Sahte(varsayilan=httpx.Response(403))).fetch_player("1"))


def test_404_varsayim_varlik_bulunamadi(tmp_path):
    with pytest.raises(VarlikBulunamadi):
        run(kur(tmp_path, Sahte(varsayilan=httpx.Response(404))).fetch_player("1"))
    with pytest.raises(VarlikBulunamadi):
        run(kur(tmp_path, Sahte(varsayilan=httpx.Response(404))).fetch_party("1"))


@pytest.mark.parametrize("baslik,beklenen", [("30", 30.0), ("999999", 300.0), ("Wed, 21 Oct 2026 07:28:00 GMT", None),
                                              ("-5", None), ("abc", None), (None, None)])
def test_429_varsayim_istek_siniri_retry_after(tmp_path, baslik, beklenen):
    h = {"Retry-After": baslik} if baslik is not None else {}
    with pytest.raises(IstekSiniri) as e:
        run(kur(tmp_path, Sahte(varsayilan=httpx.Response(429, headers=h))).fetch_player("1"))
    assert e.value.bekle_saniye == beklenen


@pytest.mark.parametrize("kod", [500, 502, 503, 504])
def test_5xx_site_erisilemez(tmp_path, kod):
    with pytest.raises(SiteErisilemez):
        run(kur(tmp_path, Sahte(varsayilan=httpx.Response(kod, text="<html>hata</html>"))).fetch_player("1"))


def test_baglanti_hatasi_site_erisilemez(tmp_path):
    with pytest.raises(SiteErisilemez):
        run(kur(tmp_path, Sahte(varsayilan=httpx.ConnectError("boom"))).fetch_player("1"))


def test_zaman_asimi_ag_zaman_asimi(tmp_path):
    with pytest.raises(AgZamanAsimi):
        run(kur(tmp_path, Sahte(varsayilan=httpx.ReadTimeout("t"))).fetch_player("1"))


@pytest.mark.parametrize("y", [
    httpx.Response(200, text="<html>giriş</html>"),
    httpx.Response(200, text=""),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"player": None}),
    httpx.Response(204),
    httpx.Response(302, headers={"Location": "https://diplomacia.com.tr/login"}),
    httpx.Response(400, json={"e": 1}),
])
def test_bozuk_veya_beklenmeyen_govde_eksikveri_oyuncu_yok_degil(tmp_path, y):
    with pytest.raises(EksikVeri) as e:
        run(kur(tmp_path, Sahte(varsayilan=y)).fetch_player("1"))
    assert not isinstance(e.value, VarlikBulunamadi)


def test_asiri_buyuk_govde_eksikveri(tmp_path):
    with pytest.raises(EksikVeri):
        run(kur(tmp_path, Sahte(varsayilan=httpx.Response(200, content=b"x" * (ist.MAKS_GOVDE_BAYT + 1)))).fetch_player("1"))


# ---- gizlilik ----
def test_token_log_hata_ve_repr_icinde_yok(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    s = Sahte(tam_yanitlar())
    k = kur(tmp_path, s)
    run(k.fetch_party("5830"))
    metinler = [caplog.text, repr(k), repr(k._kimlik)]
    for kod in (401, 403, 404, 429, 500):
        k2 = kur(tmp_path, Sahte(varsayilan=httpx.Response(kod)))
        with pytest.raises(Exception) as e:
            run(k2.fetch_player("1"))
        metinler += [str(e.value), repr(e.value)]
    k3 = kur(tmp_path, Sahte(varsayilan=httpx.ConnectError(f"Bearer {GIZLI}")))
    with pytest.raises(SiteErisilemez) as e:
        run(k3.fetch_player("1"))
    metinler += [str(e.value), repr(e.value), repr(e.value.__cause__), repr(e.value.__context__)]
    assert all(GIZLI not in m for m in metinler)


# ---- savaş ----
def test_savas_artik_gercek_uc_noktaya_gider_sahte_basari_yok(tmp_path):
    # /api/wars yanıtı yoksa (404) sahte liste DÖNMEZ; hata yukarı gider. Ayrıntılı savaş testleri: test_savas_baglanti.py
    s = Sahte(tam_yanitlar())
    k = kur(tmp_path, s)
    with pytest.raises(VarlikBulunamadi):
        run(k.list_active_wars())
    assert [str(r.url) for r in s.istekler] == ["https://diplomacia.com.tr/api/wars"]


# ---- SinirliKaynak ile sarma ----
def test_sinirli_kaynak_ile_sarilir(tmp_path):
    k = SinirliKaynak(kur(tmp_path, Sahte(tam_yanitlar())))
    assert run(k.fetch_player("72400")).ad == "Ayetullah Mutahhari"
    assert run(k.fetch_party("5830")).uyeler_tamam is True
