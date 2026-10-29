"""Kural 11, 15-19 (MOCK kaynak/gönderici; canlı doğrulama DEĞİLDİR)."""
import asyncio
from datetime import datetime, timezone

import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte

from dipbot import config, db, outbox, yonetim
from dipbot.db import islem
from dipbot.kaynak import SavasDetay, SavasOzet, SavasTaraf, SiteErisilemez
from dipbot.takip import TakipServisi
from fakes import SahteGonderici


# ---- Savaş: kural 16, 17 -------------------------------------------------
def test_k16_ilk_savas_taramasi_bildirim_yagmuru_olusturmaz(conn, servis, src):
    src.savaslar = [SavasOzet(f"w{i}", f"Savas {i}") for i in range(30)]
    run(servis.savaslari_tara(G1, src))
    assert bildirimler(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 30
    run(servis.savaslari_tara(G1, src))
    assert bildirimler(conn) == []
    # yalnız YENİ kimlikli savaş bildirilir, tekrar bildirilmez
    src.savaslar.append(SavasOzet("w99", "Yeni"))
    run(servis.savaslari_tara(G1, src))
    run(servis.savaslari_tara(G1, src))
    b = bildirimler(conn, "yeni_savas")
    assert len(b) == 1 and b[0]["veri"]["savas_id"] == "w99"
    assert b[0]["grup_id"] == G1 and b[0]["hedef_sohbet"] == config.LOG_KANAL_ID  # takip bildirimleri log kanalına


def test_savas_baseline_hata_halinde_tamamlanmaz(conn, servis, src):
    src.hatalar[("savaslar", None)] = SiteErisilemez("503")
    r = run(servis.savaslari_tara(G1, src))
    assert not r.ok
    assert conn.execute("SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()[0] == 0


def test_k17_hasar_degisiklikleri_otomatik_mesaj_uretmez(conn, servis, src, saat):
    src.savaslar = [SavasOzet("w1", "S")]
    src.savas_detay["w1"] = SavasDetay("w1", "S", "aktif", (SavasTaraf("A", 10), SavasTaraf("B", None)))
    run(servis.savaslari_tara(G1, src))
    src.savas_detay["w1"] = SavasDetay("w1", "S", "aktif", (SavasTaraf("A", 99999), SavasTaraf("B", 5)))
    for _ in range(3):
        run(servis.savaslari_tara(G1, src))
    assert bildirimler(conn) == []
    assert src.cagri_say("savas") == 0  # tarama hasar için ayrıntı bile çekmez
    # Hasar yalnız kullanıcı isteğiyle gösterilir
    g = run(servis.savas_detayi("w1", src))
    assert g.guncel and g.detay.taraflar[0].guc == 99999
    assert bildirimler(conn) == []


def test_savas_detay_erisilemezse_tarihli_son_veri_gosterilir(conn, servis, src, saat):
    src.savas_detay["w1"] = SavasDetay("w1", "S", "aktif", (SavasTaraf("A", 10), SavasTaraf("B", None)))
    ilk = run(servis.savas_detayi("w1", src))
    saat.ilerle(3600)
    src.hatalar[("savas", "w1")] = SiteErisilemez("503")
    g = run(servis.savas_detayi("w1", src))
    assert not g.guncel and g.hata and g.zaman == ilk.zaman
    assert g.detay.taraflar[1].guc is None  # bilinmeyen alan 0'a çevrilmez
    # hiç veri yoksa "erişilemedi"
    src.hatalar[("savas", "w2")] = SiteErisilemez("503")
    g2 = run(servis.savas_detayi("w2", src))
    assert g2.detay is None and g2.hata


# ---- Grup ayrımı: kural 11 ----------------------------------------------
def test_k11_gruplarin_listeleri_ve_ayarlari_ayridir(conn, servis, src, saat):
    for o in "ab":
        src.oyuncu(o, o.upper())
    run(servis.oyuncu_ekle(G1, "a", src))
    run(servis.oyuncu_ekle(G1, "b", src))
    run(servis.oyuncu_ekle(G2, "a", src))
    src.parti("P", "Parti", [("a", "A")])
    run(servis.parti_ekle(G1, "P", src))
    yonetim.tarama_araligi_ayarla(conn, G1, 5)
    assert yonetim.tarama_araligi(conn, G2) == 10 and yonetim.tarama_araligi(conn, G1) == 5
    # G1'de silmek G2'yi etkilemez
    assert servis.oyuncu_sil(G1, "a")
    assert takipte(conn, G1) == {"b"} and takipte(conn, G2) == {"a"}
    assert conn.execute("SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", (G2,)).fetchone()[0] == 0
    # Bildirimler yalnız ilgili gruba: a yalnız G2'de takipli, değişir
    src.oyuncu("a", "A2")
    run(servis.oyuncular_tara(G1, src))
    run(servis.oyuncular_tara(G2, src))
    b = bildirimler(conn, "oyuncu_degisti")
    assert [x["grup_id"] for x in b] == [G2]
    assert {x["hedef_sohbet"] for x in b} == {config.LOG_KANAL_ID}
    # Aynı oyuncu iki grupta: her grup kendi bildirimini bir kez alır
    run(servis.oyuncu_ekle(G1, "a", src))
    src.oyuncu("a", "A3")
    run(servis.oyuncular_tara(G1, src))
    run(servis.oyuncular_tara(G2, src))
    b = bildirimler(conn, "oyuncu_degisti")
    assert sorted(x["grup_id"] for x in b) == sorted([G2, G1, G2])
    assert {x["hedef_sohbet"] for x in b} == {config.LOG_KANAL_ID}
    # Parti kaldırma başka grubu etkilemez
    run(servis.parti_ekle(G2, "P", src))
    o = run(servis.parti_kaldirma_hazirla(G1, "P", src))
    run(servis.parti_kaldirma_onayla(G1, "P", src, o.ozet_anahtari))
    assert "a" in takipte(conn, G2)
    assert conn.execute("SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", (G2,)).fetchone()[0] == 1


# ---- Outbox: kural 18, 19 -----------------------------------------------
def _kuyruk(conn, saat, grup=G1, anahtar="k1"):
    with islem(conn):
        return outbox.kuyruga_ekle(
            conn, hedef_sohbet=grup, grup_id=grup, tur="t", idem_anahtari=anahtar, veri={"x": 1}, simdi=saat()
        )


def test_k18_gecici_telegram_hatasinda_kontrollu_yeniden_deneme(conn, saat):
    _kuyruk(conn, saat)
    g = SahteGonderici(hata_sayisi=2)
    o = run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert (o.gonderilen, o.yeniden_denenecek) == (0, 1)
    row = conn.execute("SELECT durum, deneme FROM outbox").fetchone()
    assert (row["durum"], row["deneme"]) == ("bekliyor", 1)  # kaybolmadı
    o = run(outbox.bekleyenleri_gonder(conn, g, saat))  # bekleme süresi dolmadı: denenmez
    assert (o.gonderilen, o.yeniden_denenecek) == (0, 0) and g.kalan_hata == 1
    saat.ilerle(31)
    run(outbox.bekleyenleri_gonder(conn, g, saat))  # 2. hata
    assert conn.execute("SELECT deneme FROM outbox").fetchone()[0] == 2
    saat.ilerle(61)
    o = run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert o.gonderilen == 1 and len(g.gonderilen) == 1
    assert conn.execute("SELECT durum FROM outbox").fetchone()[0] == "gonderildi"
    saat.ilerle(3600)
    run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert len(g.gonderilen) == 1  # tekrar gönderilmedi


def test_kalici_hata_ve_azami_deneme_basarisiz_isaretlenir(conn, saat):
    _kuyruk(conn, saat)
    g = SahteGonderici(hata_sayisi=1, kalici=True)
    o = run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert o.basarisiz == 1
    assert conn.execute("SELECT durum FROM outbox").fetchone()[0] == "basarisiz"


def test_outbox_idempotency_ve_ayni_transaction(conn, saat):
    assert _kuyruk(conn, saat, anahtar="ayni") is True
    assert _kuyruk(conn, saat, anahtar="ayni") is False  # çift yazım yok
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    # Durum değişikliği + bildirim aynı transaction: hata olursa ikisi de geri alınır
    conn.execute(
        "INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi, baseline_tamam) VALUES(?,?,?,?,1)",
        (G1, "z", "Eski", "2026-01-01T00:00:00.000000Z"),
    )
    with pytest.raises(RuntimeError):
        with islem(conn):
            conn.execute("UPDATE takip_oyuncular SET ad='Yeni' WHERE oyuncu_id='z'")
            _kuyruk(conn, saat, anahtar="yeni")
            raise RuntimeError("gönderim öncesi çökme")
    assert conn.execute("SELECT ad FROM takip_oyuncular WHERE oyuncu_id='z'").fetchone()[0] == "Eski"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_k19_grup_onayi_kaldirilinca_bekleyen_mesajlar_gonderilmez(conn, saat):
    _kuyruk(conn, saat, G1, "a")
    _kuyruk(conn, saat, G2, "b")
    iptal = yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    assert iptal == 1
    g = SahteGonderici()
    run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert [x[0] for x in g.gonderilen] == [G2]  # yalnız onaylı grup
    # Onay doğrudan DB'den silinse bile (iptal adımı atlansa) gönderim anında yeniden denetlenir
    _kuyruk(conn, saat, G2, "c")
    conn.execute("DELETE FROM onayli_gruplar WHERE group_id=?", (G2,))
    run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert len(g.gonderilen) == 1
    assert conn.execute("SELECT durum FROM outbox WHERE idem_anahtari='c'").fetchone()[0] == "iptal"


def test_kurucuya_ozel_bildirim_grup_onayindan_bagimsiz_gonderilir(conn, saat):
    with islem(conn):
        outbox.kuyruga_ekle(conn, hedef_sohbet=KURUCU, grup_id=None, tur="token_gecersiz",
                            idem_anahtari="t", veri={}, simdi=saat())
    g = SahteGonderici()
    run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert g.gonderilen[0][0] == KURUCU


# ---- Kalıcılık: kural 15 --------------------------------------------------
def test_k15_yeniden_baslatmada_takipler_ve_onceki_bilgiler_korunur(tmp_path, saat, src):
    yol = tmp_path / "veri" / "dipbot.db"
    c1 = db.baglan(yol)
    yonetim.grup_onayla(c1, saat, KURUCU, KURUCU, G1)
    yonetim.yetki_ver(c1, saat, KURUCU, KURUCU, 55)
    yonetim.tarama_araligi_ayarla(c1, G1, 15)
    s1 = TakipServisi(c1, saat)
    for o in ("a", "b"):
        src.oyuncu(o, o.upper(), "Varna", "U1")
    src.parti("P", "Parti", [("a", "A"), ("b", "B")])
    src.savaslar = [SavasOzet("w1", "S1")]
    run(s1.parti_ekle(G1, "P", src))
    run(s1.savaslari_tara(G1, src))
    c1.close()

    # "Yeniden başlatma": yeni bağlantı, yeni servis nesnesi
    c2 = db.baglan(yol)
    s2 = TakipServisi(c2, saat)
    d = yonetim.durum_yukle(c2, KURUCU)
    assert 55 in d.yetkililer and G1 in d.onayli_gruplar
    assert yonetim.tarama_araligi(c2, G1) == 15
    assert takipte(c2, G1) == {"a", "b"}
    assert c2.execute("SELECT COUNT(*) FROM parti_uyeleri").fetchone()[0] == 2
    # İlk kurulum gibi davranmaz: baseline'lar bildirim üretmez, eskiden bilinen savaş yeniden bildirilmez
    assert run(s2.oyuncular_tara(G1, src)).ok
    assert run(s2.partiler_tara(G1, src)).ok
    run(s2.savaslari_tara(G1, src))
    assert bildirimler(c2) == []
    # Önceki gözlem korunduğu için değişiklik yeniden başlatma sonrası da yakalanır
    src.oyuncu("a", "A", "Istanbul", "U1")
    run(s2.oyuncular_tara(G1, src))
    b = bildirimler(c2, "oyuncu_degisti")
    assert len(b) == 1 and b[0]["veri"]["degisimler"][0]["eski"] == "Varna"
    # Yeni savaş yine bildirilir (baseline bayrağı kalıcı)
    src.savaslar.append(SavasOzet("w2", "S2"))
    run(s2.savaslari_tara(G1, src))
    assert [x["veri"]["savas_id"] for x in bildirimler(c2, "yeni_savas")] == ["w2"]
    c2.close()
