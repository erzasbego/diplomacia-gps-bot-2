"""Öncelik 4: O7, O4, D5, D1. MOCK."""
import asyncio
import logging
from datetime import timedelta

import pytest

from conftest import G1, KURUCU, bildirimler, run

from dipbot import config, outbox, yonetim
from dipbot.config import Ayarlar
from dipbot.kaynak import OyuncuAnlik
from dipbot.zamanlayici import TaramaZamanlayici

G9 = -1009


# ---- O7 ----
def test_o7_onaysizken_yazilan_bayat_satir_yeniden_onayda_gitmez(conn, saat, gonderici):
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    # Uçuştaki tarama/eski satır: onay yokken kuyruğa girmiş bekleyen satır
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="bayat",
                        veri={"savas_id": "s1", "ad": "x"}, simdi=saat())
    saat.ilerle(60)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    saat.ilerle(60)
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="taze",
                        veri={"savas_id": "s2", "ad": "y"}, simdi=saat())
    o = run(outbox.bekleyenleri_gonder(conn, gonderici, saat))
    assert o.gonderilen == 1 and o.iptal == 1
    assert [g[2]["savas_id"] for g in gonderici.gonderilen] == ["s2"]


def test_o7_tarama_gecis_sirasinda_onay_kalkarsa_kuyruga_yazilmaz(conn, saat, servis, src):
    src.oyuncu("p1", "A", "Roma")
    run(servis.oyuncu_ekle(G1, "p1", src))
    src.oyuncu("p1", "A", "Paris")  # değişiklik
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    run(servis.oyuncular_tara(G1, src))  # uçuştaki tarama gibi
    assert bildirimler(conn, "oyuncu_degisti") == []


# ---- O4 ----
def _dene(conn, saat, i):
    return yonetim.yetkisiz_deneme_kaydet(conn, saat, KURUCU, kullanici_id=5000 + i, grup_id=-7000 - i, islem_adi="x")


def test_o4_toplam_saatlik_sinir_ve_tek_ozet(conn, saat):
    sonuclar = [_dene(conn, saat, i) for i in range(config.UNAUTHORIZED_NOTIFY_MAX_PER_HOUR + 15)]
    assert sum(sonuclar) == config.UNAUTHORIZED_NOTIFY_MAX_PER_HOUR
    assert len(bildirimler(conn, "yetkisiz_deneme")) == config.UNAUTHORIZED_NOTIFY_MAX_PER_HOUR
    oz = bildirimler(conn, "yetkisiz_ozet")
    assert len(oz) == 1 and oz[0]["veri"]["sayi"] == 15 and oz[0]["hedef_sohbet"] == KURUCU
    # tüm denemeler yine de kaydedildi
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == config.UNAUTHORIZED_NOTIFY_MAX_PER_HOUR + 15


def test_o4_saat_gecince_bildirim_yeniden_acilir(conn, saat):
    for i in range(config.UNAUTHORIZED_NOTIFY_MAX_PER_HOUR + 3):
        _dene(conn, saat, i)
    saat.ilerle(3700)
    assert _dene(conn, saat, 999) is True


def test_o4_saklama_suresi_ve_satir_ust_siniri(conn, saat, monkeypatch):
    for i in range(5):
        _dene(conn, saat, i)
    saat.ilerle(config.UNAUTHORIZED_RETENTION_DAYS * 86400 + 10)
    _dene(conn, saat, 100)
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 1
    monkeypatch.setattr(config, "UNAUTHORIZED_TABLE_MAX_ROWS", 20)
    for i in range(60):
        _dene(conn, saat, 200 + i)
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] <= 20


# ---- D5 ----
def test_d5_tick_istisnasi_dongu_oldurmez_maskeli_log_ve_geri_cekilme(conn, src, saat, gonderici, monkeypatch, caplog):
    monkeypatch.setattr(config, "TICK_SECONDS", 0.01)
    z = TaramaZamanlayici(conn, src, KURUCU, saat)
    cagri = []

    def patla():
        cagri.append(1)
        raise RuntimeError("Authorization: Bearer SIRRIN-DEGERI-123 ve 123456789:AAAbbbCCCdddEEEfffGGGhhhIII")

    z.tick = patla
    caplog.set_level(logging.ERROR)

    async def sen():
        durdur = asyncio.Event()
        gorev = asyncio.create_task(z.calis(gonderici, durdur))
        await asyncio.sleep(0.4)
        assert not gorev.done()  # döngü öldürülmedi
        durdur.set()
        await asyncio.wait_for(gorev, 2)

    run(sen())
    assert 2 <= len(cagri) < 12  # geri çekilme: 0.4 sn'de ~40 değil
    metin = caplog.text
    assert "SIRRIN-DEGERI" not in metin and "AAAbbbCCC" not in metin and "RuntimeError" in metin


# ---- D1 ----
@pytest.mark.parametrize("deger", ["0", "-5", "-1001"])
def test_d1_owner_id_pozitif_olmali(deger):
    with pytest.raises(ValueError):
        Ayarlar.from_env({"OWNER_TELEGRAM_ID": deger})
    assert Ayarlar.from_env({"OWNER_TELEGRAM_ID": "42"}).owner_telegram_id == 42


def test_d1_grup_onayla_pozitif_id_reddedilir(conn, saat):
    with pytest.raises(yonetim.YonetimHatasi):
        yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, 12345)
    with pytest.raises(yonetim.YonetimHatasi):
        yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, 0)
    assert yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G9) is True


def test_d1_yetki_ver_negatif_id_reddedilir(conn, saat):
    with pytest.raises(yonetim.YonetimHatasi):
        yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, -55)
    with pytest.raises(yonetim.YonetimHatasi):
        yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, 0)
    assert yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, 55) is True
