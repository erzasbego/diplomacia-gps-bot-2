"""Bağımsız QA bulgularının (QA-A: YÜKSEK-1, ORTA-2/3/4) hata-önce regresyon testleri. MOCK."""
import pytest
from conftest import G1, KURUCU, bildirimler, run
from tg_fakes import YETKILI

from dipbot import config, outbox, yonetim
from dipbot.kaynak import SavasOzet
from dipbot.takip import TakipServisi
from dipbot.tg.arayuz import Arayuz

from test_tur_d9 import YENI, _hazir, _tasi, ui  # noqa: F401


# ------------------------------------------------------------------ YÜKSEK-1
def _w(i):
    return SavasOzet(f"w{i}", f"S{i}", durum="active", war_number=i)


def test_yeni_grubun_ilk_savas_taramasi_tasimayi_engellemez(ui, conn, saat, src):
    """Yeni grup onaylanınca zamanlayıcı en geç 15 sn'de savaş taraması yapar (savaşlar + baseline yazılır).
    Taşıma yine de çalışmalı; savaş kayıtları birleşmeli, bildirim yağmuru olmamalı."""
    _hazir(ui, conn, saat)  # G1: w1, w2, baseline=1; YENI onaylı
    src.savaslar = [_w(2), _w(3)]  # yeni grup w3'ü kendi taramasında gördü (w2 ortak)
    sv = TakipServisi(conn, saat)
    run(sv.savaslari_tara(YENI, src))
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (YENI,)).fetchone()[0] == 2
    _tasi(ui)
    ids = {r[0] for r in conn.execute("SELECT savas_id FROM savaslar WHERE group_id=?", (YENI,))}
    assert ids == {"w1", "w2", "w3"}
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 0
    assert conn.execute("SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (YENI,)).fetchone()[0] == 1
    src.savaslar = [_w(1), _w(2), _w(3)]
    run(sv.savaslari_tara(YENI, src))
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor' AND tur LIKE '%savas%'",
                        (YENI,)).fetchone()[0] == 0


def test_yeni_grupta_oyuncu_verisi_varsa_hala_reddedilir(ui, conn, saat):
    _hazir(ui, conn, saat)
    conn.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, eklendi) VALUES(?, 'z', 't')", (YENI,))
    with pytest.raises(yonetim.TasimaHatasi):
        yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, YENI)


# ------------------------------------------------------------------ denetim bildirimi
@pytest.fixture
def yconn(conn, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return conn


def _ver(conn, saat, hedef, aktor=YETKILI):
    return yonetim.yetki_ver(conn, saat, KURUCU, aktor, hedef)


def _gonder_hepsi(conn, saat, gonderici):
    return run(outbox.bekleyenleri_gonder(conn, gonderici, saat))


def test_ozet_gonderildikten_sonra_yeni_degisiklikler_kaybolmaz(yconn, saat, gonderici):
    for h in range(300, 300 + config.AUDIT_NOTIFY_MAX_PER_HOUR + 3):
        _ver(yconn, saat, h)
    _gonder_hepsi(yconn, saat, gonderici)
    assert len(bildirimler(yconn, "denetim_ozet")) == 1
    n0 = sum(r["veri"]["sayi"] for r in bildirimler(yconn, "denetim_ozet"))
    for h in range(400, 403):
        _ver(yconn, saat, h)
    toplam = sum(r["veri"]["sayi"] for r in bildirimler(yconn, "denetim_ozet"))
    assert toplam == n0 + 3, "özet gönderildikten sonraki değişiklikler yeni/artan özetle sayılmalı"
    assert len(bildirimler(yconn, "denetim_ozet")) <= 6


def test_ters_eylem_dizisinde_son_bildirim_gercek_durumu_yansitir(yconn, saat):
    _ver(yconn, saat, 300)
    saat.ilerle(5)
    yonetim.yetki_al(yconn, saat, KURUCU, YETKILI, 300)
    saat.ilerle(5)
    _ver(yconn, saat, 300)
    eylemler = [b["veri"]["eylem"] for b in bildirimler(yconn, "denetim")]
    assert eylemler[-1] == "yetki_ver" and eylemler.count("yetki_ver") == 2
    # aynı eylemin gerçek tekrarı hâlâ pencerede bastırılır
    saat.ilerle(5)
    yonetim.yetki_al(yconn, saat, KURUCU, YETKILI, 300)
    saat.ilerle(5)
    yonetim.yetki_al(yconn, saat, KURUCU, YETKILI, 300)  # değişiklik yok -> zaten bildirim yok


def test_gecici_kuyruk_hatasi_dedup_isaretini_birakmaz(yconn, saat, monkeypatch):
    gercek = outbox.kuyruga_ekle
    n = {"i": 0}

    def hatali(*a, **kw):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("gecici")
        return gercek(*a, **kw)

    monkeypatch.setattr(outbox, "kuyruga_ekle", hatali)
    assert _ver(yconn, saat, 300) is True  # asıl işlem etkilenmez
    assert bildirimler(yconn, "denetim") == []
    yonetim.yetki_al(yconn, saat, KURUCU, YETKILI, 300)  # farklı eylem: bildirim gelir
    saat.ilerle(1)
    _ver(yconn, saat, 300)
    assert [b["veri"]["eylem"] for b in bildirimler(yconn, "denetim")] == ["yetki_al", "yetki_ver"]
    assert yconn.execute("SELECT COUNT(*) FROM bildirim_sinirlari WHERE anahtar LIKE 'denetim:yetki_ver:%'").fetchone()[0] == 1


# ------------------------------------------------------------------ Opus bulguları
def test_opus_cakisan_savasta_en_erken_ilk_gorulme_korunur_ve_sayi_gercek(ui, conn, saat):
    _hazir(ui, conn, saat)  # G1: w1, w2 (ilk_gorulme = şimdi)
    conn.execute("UPDATE savaslar SET ilk_gorulme='2026-01-01T00:00:00.000000Z' WHERE group_id=? AND savas_id='w1'", (G1,))
    conn.execute("INSERT INTO savaslar(group_id, savas_id, ad, ilk_gorulme) VALUES(?, 'w1', 'S1', '2026-10-20T00:00:00.000000Z')", (YENI,))
    sayilar = yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, YENI)
    r = conn.execute("SELECT ilk_gorulme FROM savaslar WHERE group_id=? AND savas_id='w1'", (YENI,)).fetchone()
    assert r[0] == "2026-01-01T00:00:00.000000Z"
    assert sayilar["savaslar"] == 1, "yalnız GERÇEKTEN taşınan satır (w2) sayılır; çakışan w1 sayılmaz"


def test_opus_token_duraklatilmisken_prev_yas_dolsa_da_silinmez(conn, src, saat, tmp_path):
    """Yeni token kanıtlanmadan taramalar 401 alıp duraklatıldıysa .prev (son çalışan token olabilir) korunur."""
    import os
    from dipbot.tg.token import TokenDeposu
    from dipbot.zamanlayici import TaramaZamanlayici
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz("tok-bir")
    depo.yaz("tok-iki")
    ts = saat().timestamp() - (config.TOKEN_PREV_OMRU_SANIYE + 10)
    os.utime(depo.yedek_yolu, (ts, ts))
    z = TaramaZamanlayici(conn, src, KURUCU, saat, token_deposu=depo)
    z.token_gecersiz_bildir()
    assert z.token_duraklatildi
    assert z.prev_politikasi_uygula() is False and depo.yedek_yolu.exists()
    z.token_devam_ettir()
    assert z.prev_politikasi_uygula() is True
