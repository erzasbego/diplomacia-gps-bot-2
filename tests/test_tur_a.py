"""Tur A (kimlik doğrulama, doğrulayıcı, 403 sınıfı, numara kanonikleştirme). MOCK; canlı DEĞİL.

Kimlik biçimi (Authorization: Bearer + X-Client-Platform: web) kullanıcı tarafından DevTools'ta
doğrulandı; bu testler yalnız o biçimin kodda uygulandığını sınar."""
import json
import logging
import os
from pathlib import Path

import httpx
import pytest

from conftest import G1, G2, KURUCU, bildirimler, run
from fakes import SahteKaynak
from tg_fakes import grup_komut

from dipbot.diplomacia.dogrulayici import OyuncuDogrulayici
from dipbot.diplomacia.istemci import GecersizNumara, IstemciKaynagi, OrtamKimlik, numara_dogrula
from dipbot.kaynak import (
    ErisimReddedildi, OyuncuAnlik, SinirliKaynak, SiteErisilemez, TokenGecersiz, TokenYok,
)
from dipbot.tg import calistir as c
from dipbot.tg.arayuz import Arayuz, kimlik_coz
from dipbot.tg.kaynak_yok import YapilandirilmamisKaynak
from dipbot.tg.modeller import Olay
from dipbot.tg.token import TokenDeposu
from dipbot.zamanlayici import TaramaZamanlayici

FX = json.loads((Path(__file__).parent / "fixtures" / "diplomacia_ornek.json").read_text(encoding="utf-8"))
GIZLI = "SAHTE-TOKEN-DEGERI"
ESKI = "SAHTE-ESKI-TOKEN"
OYUNCU_200 = httpx.Response(200, json=FX["player_response"])


def sayac(yanit):
    istekler = []

    def h(req):
        istekler.append(req)
        if isinstance(yanit, Exception):
            raise yanit
        return yanit

    return h, istekler


# ---------------- A1: başlıklar ----------------
def test_varsayilan_bearer_ve_platform_basligi_ortam_bos(tmp_path):
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz(GIZLI)
    h, ist = sayac(OYUNCU_200)
    k = IstemciKaynagi(OrtamKimlik(depo, {}), transport=httpx.MockTransport(h))
    run(k.fetch_player("72400"))
    assert ist[0].headers["Authorization"] == f"Bearer {GIZLI}"
    assert ist[0].headers["X-Client-Platform"] == "web"


def test_ortam_override_ve_platform_yine_gider(tmp_path):
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz(GIZLI)
    h, ist = sayac(OYUNCU_200)
    ortam = {"DIPLOMACIA_AUTH_BASLIK": "X-Token", "DIPLOMACIA_AUTH_SEMA": ""}
    run(IstemciKaynagi(OrtamKimlik(depo, ortam), transport=httpx.MockTransport(h)).fetch_player("72400"))
    assert ist[0].headers["X-Token"] == GIZLI and ist[0].headers["X-Client-Platform"] == "web"


@pytest.mark.parametrize("icerik", [None, "", "   \n", "iki kelime", "çğü"])
def test_token_yok_bos_veya_bozuksa_istek_atilmaz_ve_TokenYok(tmp_path, icerik):
    depo = TokenDeposu(tmp_path / "t")
    if icerik is not None:
        (tmp_path / "t").write_text(icerik, encoding="utf-8")
    h, ist = sayac(OYUNCU_200)
    k = IstemciKaynagi(OrtamKimlik(depo, {}), transport=httpx.MockTransport(h))
    with pytest.raises(TokenYok) as e:
        run(k.fetch_player("72400"))
    assert not isinstance(e.value, TokenGecersiz) and ist == []


def test_401_TokenGecersiz(tmp_path):
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz(GIZLI)
    h, _ = sayac(httpx.Response(401))
    k = IstemciKaynagi(OrtamKimlik(depo, {}), transport=httpx.MockTransport(h))
    with pytest.raises(TokenGecersiz):
        run(k.fetch_player("72400"))


# ---------------- A2: kaynak_olustur token dosyasına bağlı değil ----------------
def test_kaynak_olustur_token_yokken_gercek_kaynak_sonra_dosya_yazilinca_istek_atar(tmp_path):
    yol = tmp_path / "diplomacia.token"
    k = c.kaynak_olustur(yol, {})
    assert isinstance(k, IstemciKaynagi)
    h, ist = sayac(OYUNCU_200)
    k._http = httpx.AsyncClient(base_url="https://diplomacia.com.tr", transport=httpx.MockTransport(h))
    with pytest.raises(TokenYok):
        run(k.fetch_player("72400"))
    assert ist == []
    TokenDeposu(yol).yaz(GIZLI)  # kurucu menüden token verdi; yeniden başlatma YOK
    assert run(k.fetch_player("72400")).oyuncu_id == "72400"
    assert len(ist) == 1 and ist[0].headers["Authorization"] == f"Bearer {GIZLI}"


def test_gecersiz_override_yapilandirilmamis_kalir(tmp_path):
    assert isinstance(c.kaynak_olustur(tmp_path / "t", {"DIPLOMACIA_AUTH_BASLIK": "kötü başlık"}),
                      YapilandirilmamisKaynak)


# ---------------- zamanlayıcı yardımcıları ----------------
def takipli(servis):
    s = SahteKaynak()
    s.oyuncu("72400", ad="A")
    s.oyuncu("72401", ad="B")
    run(servis.oyuncu_ekle(G1, "72400", s))
    run(servis.oyuncu_ekle(G1, "72401", s))


def zam_kur(conn, saat, tmp_path, yanit, token):
    depo = TokenDeposu(tmp_path / "tok")
    if token:
        depo.yaz(token)
    h, ist = sayac(yanit)
    k = IstemciKaynagi(OrtamKimlik(depo, {}), transport=httpx.MockTransport(h))
    return TaramaZamanlayici(conn, k, KURUCU, saat, token_deposu=depo), ist, k, depo


def turlar(z, saat, n=3):
    for _ in range(n):
        run(z.tick_ve_bekle())
        saat.ilerle(3600 * 24)


# ---------------- A1/A2: token yok = yapılandırma sınıfı ----------------
def test_token_yok_zamanlayici_duraklatmaz_istek_atmaz_geri_cekilme_ve_log_yagmuru_yok(conn, saat, servis, tmp_path, caplog):
    takipli(servis)
    z, ist, _, _ = zam_kur(conn, saat, tmp_path, OYUNCU_200, token=None)
    caplog.set_level(logging.WARNING)
    turlar(z, saat, 4)
    assert ist == []
    assert not z.token_duraklatildi and bildirimler(conn, "token_gecersiz") == []
    assert conn.execute("SELECT ardisik_hata FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()[0] == 0
    uyar = [r.getMessage() for r in caplog.records if "kullanılamıyor" in r.getMessage()]
    assert 1 <= len(uyar) <= 3 and len({m.split()[0] for m in uyar}) == len(uyar)  # iş tipi başına bir kez


def test_token_yok_durum_ekrani(conn, saat, tmp_path):
    z, _, k, depo = zam_kur(conn, saat, tmp_path, OYUNCU_200, token=None)
    ui = Arayuz(conn, SinirliKaynak(k), KURUCU, saat=saat, token_deposu=depo)
    assert "Diplomacia bağlantısı: token dosyası yok" in run(ui.komut(grup_komut("durum"))).metin
    depo.yaz(GIZLI)
    assert "token dosyası yok" not in run(ui.komut(grup_komut("durum"))).metin


def test_401_zamanlayici_tek_bildirim_ve_duraklatir(conn, saat, servis, tmp_path):
    takipli(servis)
    z, ist, _, _ = zam_kur(conn, saat, tmp_path, httpx.Response(401), token=GIZLI)
    turlar(z, saat, 3)
    assert z.token_duraklatildi and len(bildirimler(conn, "token_gecersiz")) == 1
    assert len(ist) <= 3  # duraklatma sonrası yağmur yok


# ---------------- A4: 403 ayrı sınıf ----------------
def test_403_ErisimReddedildi_TokenGecersiz_degil(tmp_path):
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz(GIZLI)
    h, _ = sayac(httpx.Response(403))
    k = IstemciKaynagi(OrtamKimlik(depo, {}), transport=httpx.MockTransport(h))
    with pytest.raises(ErisimReddedildi) as e:
        run(k.fetch_player("72400"))
    assert not isinstance(e.value, TokenGecersiz) and isinstance(e.value, SiteErisilemez)


def test_403_zamanlayici_geri_cekilme_var_duraklatma_ve_token_bildirimi_yok(conn, saat, servis, tmp_path):
    takipli(servis)
    z, ist, k, depo = zam_kur(conn, saat, tmp_path, httpx.Response(403), token=GIZLI)
    run(z.tick_ve_bekle())
    assert not z.token_duraklatildi and bildirimler(conn, "token_gecersiz") == []
    assert conn.execute("SELECT ardisik_hata FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()[0] == 1
    assert len([r for r in ist if "/players/" in r.url.path]) == 1  # G1: 403 sonrası kalan istekler atılmaz
    hata = conn.execute(
        "SELECT son_hata FROM tarama_isleri WHERE group_id=? AND son_hata IS NOT NULL", (G1,)).fetchone()[0]
    assert "erişim reddedildi (403)" in hata and "kimlik biçimi" in hata
    ui = Arayuz(conn, SinirliKaynak(k), KURUCU, saat=saat, token_deposu=depo, zamanlayici=z)
    assert "erişim reddedildi (403)" in run(ui.komut(grup_komut("durum"))).metin


# ---------------- A3: aday doğrulama ----------------
def dog_kur(tmp_path, yanit, ilk=ESKI):
    depo = TokenDeposu(tmp_path / "t")
    if ilk:
        depo.yaz(ilk)
    h, ist = sayac(yanit)
    return OyuncuDogrulayici(OrtamKimlik(depo, {}), transport=httpx.MockTransport(h)), depo, ist


def test_aday_200_gecerli_oyuncu_istegi_ve_dosya_degismez(tmp_path):
    d, depo, ist = dog_kur(tmp_path, OYUNCU_200)
    s = run(d.dogrula(GIZLI))
    assert s.gecerli
    assert str(ist[0].url) == "https://diplomacia.com.tr/api/players/72400"
    assert ist[0].headers["Authorization"] == f"Bearer {GIZLI}" and ist[0].headers["X-Client-Platform"] == "web"
    assert depo.oku() == ESKI and not (tmp_path / "t.prev").exists()
    assert "doğrulandı" not in s.aciklama


@pytest.mark.parametrize("yanit", [
    httpx.Response(200, text="<html><body>giriş</body></html>"),
    httpx.Response(200, json={}),
    httpx.Response(200, text=""),
    httpx.Response(200, json=[1, 2]),
    httpx.Response(200, json={"player": {"id": "x"}}),
    httpx.Response(200, json={"baska": 1}),
])
def test_200_ama_gecerli_oyuncu_degil_reddedilir(tmp_path, yanit):
    d, depo, _ = dog_kur(tmp_path, yanit)
    s = run(d.dogrula(GIZLI))
    assert not s.gecerli and depo.oku() == ESKI


def test_401_gecersiz_403_5xx_ag_hatasi_dogrulanamadi(tmp_path):
    d, _, _ = dog_kur(tmp_path, httpx.Response(401))
    assert run(d.dogrula(GIZLI)).durum == "gecersiz"
    d, _, _ = dog_kur(tmp_path, httpx.Response(403))
    s = run(d.dogrula(GIZLI))
    assert s.durum == "hata" and "doğrulanamadı" in s.aciklama
    for kod in (500, 503, 404, 429, 302):
        d, _, _ = dog_kur(tmp_path, httpx.Response(kod))
        assert run(d.dogrula(GIZLI)).durum == "hata"
    for hata in (httpx.ReadTimeout("t"), httpx.ConnectError(f"x {GIZLI}")):
        d, _, _ = dog_kur(tmp_path, hata)
        s = run(d.dogrula(GIZLI))
        assert s.durum == "hata" and GIZLI not in s.aciklama


def test_dogrulayici_token_loga_repr_e_girmez(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    d, _, _ = dog_kur(tmp_path, httpx.Response(401, text=GIZLI))
    s = run(d.dogrula(GIZLI))
    assert GIZLI not in s.aciklama + caplog.text + repr(d)


# ---------------- A3: yedek (.prev) ve arayüz akışı ----------------
def ui_kur(conn, saat, d, depo):
    return Arayuz(conn, YapilandirilmamisKaynak(), KURUCU, saat=saat, dogrulayici=d, token_deposu=depo)


def tok_olay(m):
    return Olay(sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU, metin=m)


def test_basarida_prev_olusur_0600_ve_mesaj_durust(conn, saat, tmp_path):
    d, depo, _ = dog_kur(tmp_path, OYUNCU_200)
    y = run(ui_kur(conn, saat, d, depo)._token_isle(tok_olay(GIZLI)))
    prev = tmp_path / "t.prev"
    assert depo.oku() == GIZLI and prev.read_text(encoding="utf-8") == ESKI
    if os.name == "posix":
        assert (prev.stat().st_mode & 0o777) == 0o600
    assert "kaydedildi" in y.metin and "oyuncu isteği başarılı" in y.metin and "doğrulandı" not in y.metin
    assert GIZLI not in y.metin


def test_ilk_token_prev_yok(conn, saat, tmp_path):
    d, depo, _ = dog_kur(tmp_path, OYUNCU_200, ilk=None)
    run(ui_kur(conn, saat, d, depo)._token_isle(tok_olay(GIZLI)))
    assert depo.oku() == GIZLI and not (tmp_path / "t.prev").exists()


@pytest.mark.parametrize("yanit", [httpx.Response(200, text="<html>"), httpx.Response(200, json={}),
                                   httpx.Response(401), httpx.Response(403), httpx.Response(500),
                                   httpx.ReadTimeout("t")])
def test_red_durumunda_dosya_ve_prev_degismez(conn, saat, tmp_path, yanit):
    d, depo, _ = dog_kur(tmp_path, yanit)
    prev = tmp_path / "t.prev"
    prev.write_text("ONCEKI-PREV", encoding="utf-8")
    y = run(ui_kur(conn, saat, d, depo)._token_isle(tok_olay(GIZLI)))
    assert "DEĞİŞTİRİLMEDİ" in y.metin and depo.oku() == ESKI
    assert prev.read_text(encoding="utf-8") == "ONCEKI-PREV"


# ---------------- A5: numara kanonikleştirme ----------------
@pytest.mark.parametrize("kotu", ["072400", "0072400", "0", "00", "", " 72400", "7 2400", "72400a", "abc",
                                  "1234567890123", "٧٢٤٠٠", "56b0a187-62fa-4e63-a459-f379a7a9a563"])
def test_numara_dogrula_reddeder(kotu):
    with pytest.raises(GecersizNumara):
        numara_dogrula(kotu)


def test_numara_dogrula_kabul():
    assert numara_dogrula("72400") == "72400" and numara_dogrula("1") == "1"
    assert numara_dogrula("123456789012") == "123456789012"


@pytest.mark.parametrize("kotu", ["072400", "0072400", "56b0a187-62fa-4e63-a459-f379a7a9a563", "abc", "72 400"])
def test_kimlik_coz_reddeder(kotu):
    assert kimlik_coz(kotu, "player") is None and kimlik_coz(kotu, "party") is None
    assert kimlik_coz(f"https://diplomacia.com.tr/player/{kotu}", "player") is None


def test_kimlik_coz_kabul():
    assert kimlik_coz("72400", "player") == "72400"
    assert kimlik_coz("https://diplomacia.com.tr/player/72400", "player") == "72400"
    assert kimlik_coz("https://diplomacia.com.tr/party/5830/", "party") == "5830"


UID1 = "56b0a187-62fa-4e63-a459-f379a7a9a563"


def test_ayni_uid_baska_numarayla_mukerrer_uretmez(conn, servis):
    s = SahteKaynak()
    s.oyuncular["72400"] = OyuncuAnlik("72400", "A", "B", "U", UID1)
    s.oyuncular["99"] = OyuncuAnlik("99", "A", "B", "U", UID1)
    assert run(servis.oyuncu_ekle(G1, "72400", s)).durum == "eklendi"
    r = run(servis.oyuncu_ekle(G1, "99", s))
    assert r.durum == "zaten_takipte"
    assert conn.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=?", (G1,)).fetchone()[0] == 1
    assert run(servis.oyuncu_ekle(G2, "99", s)).durum == "eklendi"  # başka grup bağımsız


def test_parti_uyesi_ayni_uid_baska_numarada_mukerrer_uretmez(conn, servis):
    s = SahteKaynak()
    s.oyuncular["72400"] = OyuncuAnlik("72400", "A", "B", "U", UID1)
    s.oyuncular["99"] = OyuncuAnlik("99", "A", "B", "U", UID1)
    s.parti("5", "P", [("99", "A")])
    run(servis.oyuncu_ekle(G1, "72400", s))
    run(servis.parti_ekle(G1, "5", s))
    run(servis.oyuncular_tara(G1, s))  # C2: ayrıntı/uid ilk taramada okunur; mükerrer orada çözülür
    ids = {r[0] for r in conn.execute("SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=?", (G1,))}
    assert ids == {"72400"}
