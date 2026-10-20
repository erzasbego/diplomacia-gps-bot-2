"""Kural 12-14 ve yetki/ayar kuralları (saf fonksiyonlar + DB; MOCK, canlı değil)."""
from datetime import timedelta

import pytest

from conftest import G1, G2, KURUCU, bildirimler

from dipbot import config, yetki, yonetim
from dipbot.yetki import YetkiDurumu

KULLANICI = 55
YABANCI = 77


def _d(yetkililer=(), gruplar=(G1,)):
    return YetkiDurumu(KURUCU, frozenset(yetkililer), frozenset(gruplar))


def test_k12_onaysiz_grup_veya_yetkisiz_kullanici_islem_yapamaz():
    d = _d(yetkililer=[KULLANICI])
    ok = yetki.karar_ver(d, sohbet_id=G1, sohbet_turu="supergroup", kullanici_id=KULLANICI)
    assert ok and ok.neden == "ok"
    # Onaylı grup + yetkisiz kullanıcı (grup onayı herkese yetki vermez)
    k = yetki.karar_ver(d, sohbet_id=G1, sohbet_turu="group", kullanici_id=YABANCI)
    assert not k and k.neden == "kullanici_yetkisiz"
    # Yetkili kullanıcı + onaysız grup
    k = yetki.karar_ver(d, sohbet_id=G2, sohbet_turu="group", kullanici_id=KULLANICI)
    assert not k and k.neden == "grup_onaysiz"
    # Kurucu bile onaysız grupta işlem yapamaz
    assert not yetki.karar_ver(d, sohbet_id=G2, sohbet_turu="group", kullanici_id=KURUCU)
    # Kullanıcı adı değil sayısal kimlik; None kimlik reddedilir
    assert not yetki.karar_ver(d, sohbet_id=G1, sohbet_turu="group", kullanici_id=None)
    # Kanal vb. desteklenmez
    assert not yetki.karar_ver(d, sohbet_id=G1, sohbet_turu="channel", kullanici_id=KURUCU)


def test_k13_ozel_sohbet_ve_token_yonetimi_yalniz_kurucu():
    d = _d(yetkililer=[KULLANICI])
    assert yetki.karar_ver(d, sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU)
    assert not yetki.karar_ver(d, sohbet_id=KULLANICI, sohbet_turu="private", kullanici_id=KULLANICI)
    assert not yetki.karar_ver(d, sohbet_id=YABANCI, sohbet_turu="private", kullanici_id=YABANCI)
    # Token yönetimi: yetkili bile olsa reddedilir; kurucu grupta bile reddedilir
    assert not yetki.token_yonetimi_izinli_mi(d, sohbet_id=KULLANICI, sohbet_turu="private", kullanici_id=KULLANICI)
    assert not yetki.token_yonetimi_izinli_mi(d, sohbet_id=G1, sohbet_turu="group", kullanici_id=KURUCU)
    assert yetki.token_yonetimi_izinli_mi(d, sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU)


def test_k14_yetkisi_alinan_kullanici_eski_butonla_islem_yapamaz(conn, saat):
    assert yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, KULLANICI)
    # buton basıldığında karar GÜNCEL DB durumundan verilir
    d1 = yonetim.durum_yukle(conn, KURUCU)
    assert yetki.karar_ver(d1, sohbet_id=G1, sohbet_turu="group", kullanici_id=KULLANICI)
    assert yonetim.yetki_al(conn, saat, KURUCU, KURUCU, KULLANICI)
    d2 = yonetim.durum_yukle(conn, KURUCU)  # eski buton aynı kullanıcıyla tekrar basılır
    k = yetki.karar_ver(d2, sohbet_id=G1, sohbet_turu="group", kullanici_id=KULLANICI)
    assert not k and k.neden == "kullanici_yetkisiz"
    # yönetim işlemleri de güncel yetkiyi yeniden denetler
    with pytest.raises(yonetim.YetkiHatasi):
        yonetim.yetki_ver(conn, saat, KURUCU, KULLANICI, YABANCI)
    with pytest.raises(yonetim.YetkiHatasi):
        yonetim.grup_onayla(conn, saat, KURUCU, KULLANICI, -999)


def test_kurucunun_yetkisi_alinamaz_ve_yetkililer_yonetebilir(conn, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, KULLANICI)
    with pytest.raises(yonetim.YetkiHatasi):
        yonetim.yetki_al(conn, saat, KURUCU, KULLANICI, KURUCU)
    assert yetki.yetkili_mi(yonetim.durum_yukle(conn, KURUCU), KURUCU)
    # yetkili kullanıcı normal yönetimi yapabilir (yetki ver / grup onayı)
    assert yonetim.yetki_ver(conn, saat, KURUCU, KULLANICI, YABANCI)
    assert yonetim.grup_onayla(conn, saat, KURUCU, KULLANICI, -555)


def test_tarama_araligi_yalniz_izinli_degerler(conn):
    assert yonetim.tarama_araligi(conn, G1) == config.DEFAULT_SCAN_INTERVAL_MINUTES == 10
    for dk in (5, 10, 15, 20):
        yonetim.tarama_araligi_ayarla(conn, G1, dk)
        assert yonetim.tarama_araligi(conn, G1) == dk
    for gecersiz in (0, 1, 7, 30, -5):
        with pytest.raises(ValueError):
            yonetim.tarama_araligi_ayarla(conn, G1, gecersiz)
    with pytest.raises(yonetim.YonetimHatasi):
        yonetim.tarama_araligi_ayarla(conn, -999, 5)  # onaysız grup


def test_yetkisiz_deneme_kurucuya_bildirilir_ve_hiz_sinirlidir(conn, saat):
    def dene(kullanici=YABANCI, grup=G2):
        return yonetim.yetkisiz_deneme_kaydet(
            conn, saat, KURUCU, kullanici_id=kullanici, grup_id=grup, islem_adi="/oyuncu_ekle"
        )

    assert dene() is True
    b = bildirimler(conn, "yetkisiz_deneme")
    assert len(b) == 1 and b[0]["hedef_sohbet"] == KURUCU and b[0]["grup_id"] is None
    assert b[0]["veri"]["kullanici_id"] == YABANCI and b[0]["veri"]["grup_id"] == G2
    for _ in range(5):  # aynı kullanıcı/grup tekrarı: yağmur yok
        saat.ilerle(10)
        assert dene() is False
    assert dene(kullanici=YABANCI, grup=-777) is False  # aynı kullanıcı, başka grup: yine sınırlı
    assert dene(kullanici=123, grup=G2) is False  # aynı grup, başka kullanıcı: sınırlı
    assert dene(kullanici=124, grup=-778) is True  # tamamen yeni
    saat.ilerle(config.UNAUTHORIZED_NOTIFY_INTERVAL_SECONDS + 1)
    assert dene() is True  # aralık doldu
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 10  # hepsi kayıtlı
    assert len(bildirimler(conn, "yetkisiz_deneme")) == 3


def test_bildirim_gerekli_mi_saf_fonksiyon(saat):
    a = timedelta(minutes=15)
    assert yetki.bildirim_gerekli_mi(None, saat(), a)
    assert not yetki.bildirim_gerekli_mi(saat(), saat(), a)
    assert yetki.bildirim_gerekli_mi(saat() - a, saat(), a)
