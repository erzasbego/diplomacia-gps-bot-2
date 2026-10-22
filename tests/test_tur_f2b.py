"""Tur F2b: D7 saat güvenliği (monotonic + duvar saati tutarlılığı, onay_donemi sayacı) ve
token .prev silme politikası. MOCK; gerçek saat/dosya sistemi/ağ davranışı DOĞRULANMADI."""
import asyncio
import os
import sqlite3
from datetime import timedelta

import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte
from tg_fakes import YETKILI, dugme, grup_dugme, grup_komut, onay_kimligi

from dipbot import config, db, outbox, yonetim
from dipbot.kaynak import SiteErisilemez, TokenGecersiz
from dipbot.tg import arayuz as ar
from dipbot.tg.arayuz import Arayuz
from dipbot.tg.token import TokenDeposu
from dipbot.zaman import (
    SURE_DOLDU, SURE_GECERLI, SURE_SAAT_DEGISTI, damga_al, sure_durumu,
)
from dipbot.zamanlayici import TaramaZamanlayici

JWT = "eyJFAKE01.FAKE02.FAKE03"


# ======================================================================= D7: süre denetimi (saf)
def test_sure_durumu_gecerli_doldu_saat_degisti(saat):
    omur = timedelta(minutes=10)
    d = damga_al(saat)
    assert sure_durumu(d, omur, saat) == SURE_GECERLI
    saat.ilerle(9 * 60)
    assert sure_durumu(d, omur, saat) == SURE_GECERLI
    saat.ilerle(2 * 60)
    assert sure_durumu(d, omur, saat) == SURE_DOLDU


@pytest.mark.parametrize("atlama", [-3600, -120, 120, 3600, 86400])
def test_sure_durumu_duvar_saati_atlarsa_saat_degisti(saat, atlama):
    d = damga_al(saat)
    saat.ilerle(60)
    saat.duvar_atla(atlama)
    assert sure_durumu(d, timedelta(minutes=10), saat) == SURE_SAAT_DEGISTI


def test_sure_durumu_kucuk_sapma_toleransi_icinde_gecerli(saat):
    d = damga_al(saat)
    saat.ilerle(60)
    saat.duvar_atla(config.SAAT_TUTARSIZLIK_TOLERANSI_SANIYE - 1)
    assert sure_durumu(d, timedelta(minutes=10), saat) == SURE_GECERLI


# ======================================================================= D7: onay butonları
@pytest.fixture
def ui(conn, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return Arayuz(conn, src, KURUCU, saat=saat)


def _silme_onayi(ui, src):
    src.oyuncu("5", "Bir")
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    return onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "5"))))


@pytest.mark.parametrize("atlama", [-3600, 3600])
def test_d7_onay_duvar_saati_atlarsa_islem_yapilmaz_ve_aciklama_verilir(ui, conn, src, saat, atlama):
    kid = _silme_onayi(ui, src)
    saat.ilerle(30)
    saat.duvar_atla(atlama)
    y = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert "clock changed" in (y.metin or "").lower() and "start the action again" in (y.metin or "").lower()
    assert "5" in takipte(conn, G1), "saat değiştiyse onayla işlem yapılamaz"
    assert conn.execute("SELECT COUNT(*) FROM tg_islemler").fetchone()[0] == 0, "geçersiz onay silinmeli"
    # kaldığı yerden de kullanılamaz (geri alınan saat eski onayı yeniden canlandırmaz)
    saat.duvar_atla(-atlama)
    y2 = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert y2.metin == ar.ONAY_BITTI and "5" in takipte(conn, G1)


def test_d7_iptal_dugmesi_de_saat_degisiminde_islem_tuketir_ama_islem_yapmaz(ui, conn, src, saat):
    kid = _silme_onayi(ui, src)
    saat.duvar_atla(-7200)
    run(ui.buton(grup_dugme(f"c:h:{kid}")))
    assert "5" in takipte(conn, G1)


def test_d7_normal_akista_onay_calisir_ve_kucuk_sapma_gecerli(ui, conn, src, saat):
    kid = _silme_onayi(ui, src)
    saat.ilerle(9 * 60)
    saat.duvar_atla(config.SAAT_TUTARSIZLIK_TOLERANSI_SANIYE - 1)
    y = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert "removed from tracking" in y.metin and "5" not in takipte(conn, G1)


def test_d7_yeniden_baslatmada_bekleyen_onay_gecersiz(ui, conn, src, saat):
    kid = _silme_onayi(ui, src)
    assert conn.execute("SELECT COUNT(*) FROM tg_islemler").fetchone()[0] == 1
    ui2 = Arayuz(conn, src, KURUCU, saat=saat)  # süreç yeniden başladı
    assert conn.execute("SELECT COUNT(*) FROM tg_islemler").fetchone()[0] == 0
    y = run(ui2.buton(grup_dugme(f"c:e:{kid}")))
    assert y.metin == ar.ONAY_BITTI
    assert "5" in takipte(conn, G1)


# ======================================================================= D7: token bekleme
def _token_ui(conn, src, saat, tmp_path):
    from test_telegram_arayuz import SahteDogrulayici, _token_ui as tui
    d = SahteDogrulayici()
    ui, depo, z = tui(conn, src, saat, tmp_path, d)
    return ui, depo, z, d


@pytest.mark.parametrize("atlama", [-3600, 3600])
def test_d7_token_bekleme_saat_degisirse_token_islenmez_ve_silinmeye_calisilir(conn, src, saat, tmp_path, atlama):
    from test_telegram_arayuz import _token_mesaji
    ui, depo, z, d = _token_ui(conn, src, saat, tmp_path)
    run(ui.buton(dugme("tn")))
    saat.ilerle(30)
    saat.duvar_atla(atlama)
    silinen = []
    y = _token_mesaji(ui, JWT, silinen)
    assert silinen == [1], "token olabilecek mesaj silinmeye çalışılmalı"
    assert "clock changed" in y.metin.lower() and "restart from the menu" in y.metin.lower() and JWT not in y.metin
    assert d.alinan == [] and depo.oku() == "eski-token-XYZ"
    # bekleme tüketildi: sonraki JWT biçimli mesaj da işlenmez
    y2 = _token_mesaji(ui, JWT)
    assert d.alinan == [] and "First choose Renew Token from the menu" in y2.metin


def test_d7_token_bekleme_normal_akista_calisir(conn, src, saat, tmp_path):
    from test_telegram_arayuz import _token_mesaji
    ui, depo, z, d = _token_ui(conn, src, saat, tmp_path)
    run(ui.buton(dugme("tn")))
    saat.ilerle(4 * 60)
    y = _token_mesaji(ui, JWT)
    assert d.alinan == [JWT] and "saved" in y.metin


def test_d7_token_bekleme_yeniden_baslatmada_gecersiz_ve_kalici_kayit_yok(conn, src, saat, tmp_path):
    from test_telegram_arayuz import _token_mesaji
    ui, depo, z, d = _token_ui(conn, src, saat, tmp_path)
    run(ui.buton(dugme("tn")))
    assert conn.execute("SELECT COUNT(*) FROM sistem_durumu WHERE anahtar='token_bekleme'").fetchone()[0] == 0, \
        "bekleme yalnız bellekte tutulur (yeniden başlatmada geçerli sayılamaz)"
    ui2 = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=z, token_deposu=depo, dogrulayici=d)
    silinen = []
    y = _token_mesaji(ui2, JWT, silinen)
    assert d.alinan == [] and "First choose Renew Token from the menu" in y.metin and silinen == [1]
    assert depo.oku() == "eski-token-XYZ"


def test_d7_eski_surumden_kalan_token_bekleme_satiri_acilista_silinir(conn, src, saat, tmp_path):
    conn.execute("INSERT INTO sistem_durumu(anahtar, deger) VALUES('token_bekleme', ?)", (f"{KURUCU}|2999-01-01T00:00:00.000000Z",))
    ui, depo, z, d = _token_ui(conn, src, saat, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM sistem_durumu WHERE anahtar='token_bekleme'").fetchone()[0] == 0
    assert ui._token_bekleyen == {}


# ======================================================================= D7: onay_donemi sayacı
def _donem(conn, g):
    r = conn.execute("SELECT donem FROM onayli_gruplar WHERE group_id=?", (g,)).fetchone()
    return None if r is None else r[0]


def _ekle(conn, saat, g, anahtar):
    with db.islem(conn):
        outbox.kuyruga_ekle(conn, hedef_sohbet=g, grup_id=g, tur="yeni_savas", idem_anahtari=anahtar,
                            veri={"savas_id": anahtar}, simdi=saat())


def test_d7_donem_sayaci_her_yeni_onayda_artar_ve_asla_geri_donmez(conn, saat):
    d1, d2 = _donem(conn, G1), _donem(conn, G2)
    assert d1 and d2 and d1 != d2
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    assert _donem(conn, G1) is None
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    assert _donem(conn, G1) > max(d1, d2)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)  # zaten onaylı: dönem DEĞİŞMEZ
    assert _donem(conn, G1) == _donem(conn, G1) and conn.execute(
        "SELECT COUNT(*) FROM onayli_gruplar WHERE group_id=?", (G1,)).fetchone()[0] == 1


def test_d7_outbox_satiri_olusturuldugu_donemi_saklar(conn, saat):
    _ekle(conn, saat, G1, "a")
    r = conn.execute("SELECT onay_donemi FROM outbox WHERE idem_anahtari='a'").fetchone()
    assert r[0] == _donem(conn, G1)


def test_d7_eski_donem_satiri_ayni_zaman_damgasinda_bile_gitmez(conn, saat, gonderici):
    """Eski davranış (`olusturuldu >= onaylandi`) AYNI damgada bayat satırı geçirirdi."""
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    _ekle(conn, saat, G1, "onaysiz-donem")  # onaysızken üretilmiş (dönem yok)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)  # aynı fake saat
    _ekle(conn, saat, G1, "yeni-donem")
    o = run(outbox.bekleyenleri_gonder(conn, gonderici, saat))
    assert [g[2]["savas_id"] for g in gonderici.gonderilen] == ["yeni-donem"]
    assert o.gonderilen == 1 and o.iptal == 1


def test_d7_duvar_saati_geri_giderse_yeni_donem_satiri_iptal_edilmez(conn, saat, gonderici):
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    saat.ilerle(60)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    saat.duvar_atla(-3600)  # saat geri atladı: eski kuralda taze satır bayat sayılırdı
    _ekle(conn, saat, G1, "taze")
    o = run(outbox.bekleyenleri_gonder(conn, gonderici, saat))
    assert [g[2]["savas_id"] for g in gonderici.gonderilen] == ["taze"] and o.iptal == 0


def test_d7_donem_degisince_gonderim_aninda_bayat_satir_iptal(conn, saat, gonderici):
    _ekle(conn, saat, G1, "eski")
    # onay kaldırılıp yeniden verilir; 'eski' satırı iptal edilmeden (ör. uçuşta) kalmış gibi geri açılır
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    conn.execute("UPDATE outbox SET durum='bekliyor' WHERE idem_anahtari='eski'")  # simülasyon: yarış
    o = run(outbox.bekleyenleri_gonder(conn, gonderici, saat))
    assert gonderici.gonderilen == [] and o.iptal == 1


def test_d7_donemi_bilinmeyen_satir_fail_closed(conn, saat, gonderici):
    _ekle(conn, saat, G1, "x")
    conn.execute("UPDATE outbox SET onay_donemi=NULL")
    o = run(outbox.bekleyenleri_gonder(conn, gonderici, saat))
    assert gonderici.gonderilen == [] and o.iptal == 1


def test_d7_kurucuya_ozel_bildirim_donemden_etkilenmez(conn, saat, gonderici):
    with db.islem(conn):
        outbox.kuyruga_ekle(conn, hedef_sohbet=KURUCU, grup_id=None, tur="denetim", idem_anahtari="k",
                            veri={}, simdi=saat())
    assert conn.execute("SELECT onay_donemi FROM outbox").fetchone()[0] is None
    o = run(outbox.bekleyenleri_gonder(conn, gonderici, saat))
    assert o.gonderilen == 1


def test_d7_migration_v3_den_donem_sutunlari_ve_degerleri(tmp_path):
    yol = tmp_path / "eski.db"
    c = sqlite3.connect(str(yol), isolation_level=None)
    c.executescript(
        """
        CREATE TABLE onayli_gruplar(group_id INTEGER PRIMARY KEY, onaylandi TEXT NOT NULL);
        CREATE TABLE outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT, hedef_sohbet INTEGER NOT NULL, grup_id INTEGER,
            tur TEXT NOT NULL, idem_anahtari TEXT NOT NULL, veri TEXT NOT NULL,
            durum TEXT NOT NULL DEFAULT 'bekliyor' CHECK(durum IN ('bekliyor','gonderildi','iptal','basarisiz')),
            deneme INTEGER NOT NULL DEFAULT 0, sonraki_deneme TEXT NOT NULL, son_hata TEXT,
            olusturuldu TEXT NOT NULL, gonderildi TEXT, UNIQUE(hedef_sohbet, idem_anahtari));
        INSERT INTO onayli_gruplar VALUES(-1001,'2026-10-19T12:00:00.000000Z');
        INSERT INTO onayli_gruplar VALUES(-1002,'2026-10-19T13:00:00.000000Z');
        INSERT INTO outbox(hedef_sohbet,grup_id,tur,idem_anahtari,veri,sonraki_deneme,olusturuldu)
            VALUES(-1001,-1001,'t','taze','{}','2026-10-19T12:30:00.000000Z','2026-10-19T12:30:00.000000Z');
        INSERT INTO outbox(hedef_sohbet,grup_id,tur,idem_anahtari,veri,sonraki_deneme,olusturuldu)
            VALUES(-1002,-1002,'t','bayat','{}','2026-10-19T12:30:00.000000Z','2026-10-19T12:30:00.000000Z');
        INSERT INTO outbox(hedef_sohbet,grup_id,tur,idem_anahtari,veri,sonraki_deneme,olusturuldu)
            VALUES(1000,NULL,'t','kurucu','{}','2026-10-19T12:30:00.000000Z','2026-10-19T12:30:00.000000Z');
        PRAGMA user_version=3;
        """
    )
    c.close()
    conn = db.baglan(yol)
    d1, d2 = _donem(conn, -1001), _donem(conn, -1002)
    assert d1 and d2 and d1 != d2
    satir = {r["idem_anahtari"]: r["onay_donemi"] for r in conn.execute("SELECT * FROM outbox")}
    assert satir["taze"] == d1, "onaydan sonra üretilmiş satır güncel dönemde"
    assert satir["bayat"] == 0, "onaydan ÖNCE üretilmiş satır bayat (dönem 0)"
    assert satir["kurucu"] is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SEMA_SURUMU >= 4
    # yeni onay, mevcut dönemlerin hiçbirini yeniden kullanmaz
    yonetim.grup_onayla(conn, lambda: __import__("fakes").SahteSaat()(), KURUCU, KURUCU, -1003)
    assert _donem(conn, -1003) > max(d1, d2)
    conn.close()


# ======================================================================= .prev politikası
def _prev_yap(tmp_path, saat, yas_sn=0):
    depo = TokenDeposu(tmp_path / "diplomacia.token")
    depo.yaz("tok-bir")
    depo.yaz("tok-iki")  # tok-bir -> .prev
    assert depo.yedek_yolu.exists()
    ts = saat().timestamp() - yas_sn
    os.utime(depo.yedek_yolu, (ts, ts))
    return depo


def _z(conn, src, saat, depo):
    return TaramaZamanlayici(conn, src, KURUCU, saat, token_deposu=depo)


def test_prev_sil_ve_yas(tmp_path, saat):
    depo = _prev_yap(tmp_path, saat, yas_sn=100)
    assert depo.prev_yasi(saat().timestamp()) == pytest.approx(100, abs=1)
    assert depo.prev_sil() is True and not depo.yedek_yolu.exists()
    assert depo.prev_sil() is False and depo.prev_yasi(saat().timestamp()) is None
    assert depo.oku() == "tok-iki", "asıl token dosyasına dokunulmaz"


def test_prev_omru_dolunca_silinir_dolmadan_silinmez(conn, src, saat, tmp_path):
    depo = _prev_yap(tmp_path, saat, yas_sn=config.TOKEN_PREV_OMRU_SANIYE - 60)
    z = _z(conn, src, saat, depo)
    assert z.prev_politikasi_uygula() is False and depo.yedek_yolu.exists()
    saat.ilerle(120)
    assert z.prev_politikasi_uygula() is True and not depo.yedek_yolu.exists()
    assert depo.oku() == "tok-iki"


def test_prev_dosya_zamani_gelecekteyse_belirsiz_oldugu_icin_silinmez_uyari_verilir(conn, src, saat, tmp_path, caplog):
    """BİLİNÇLİ DEĞİŞİKLİK (QA-B Y2): yaşı belirsiz (saat geri gitmiş, ör. boot'ta hatalı RTC) .prev silinmez;
    silmek tek geri dönüş kopyasını yok edebilirdi. Saat düzelince yaş normal hesaplanır."""
    depo = _prev_yap(tmp_path, saat, yas_sn=-7200)  # 2 sa SONRA
    z = _z(conn, src, saat, depo)
    with caplog.at_level("WARNING"):
        assert z.prev_politikasi_uygula() is False
        assert z.prev_politikasi_uygula() is False
    assert depo.yedek_yolu.exists()
    assert sum("belirsiz" in r.getMessage() for r in caplog.records) == 1, "uyarı tekrarlanmaz"
    saat.ilerle(7200 + config.TOKEN_PREV_OMRU_SANIYE + 1)  # saat düzeldi / süre doldu
    assert z.prev_politikasi_uygula() is True


def test_prev_ana_token_yoksa_veya_bossa_yas_dolsa_da_silinmez(conn, src, saat, tmp_path):
    """Ana token dosyası yok/boşken .prev tek kopya olabilir: silinmez."""
    depo = _prev_yap(tmp_path, saat, yas_sn=config.TOKEN_PREV_OMRU_SANIYE + 10)
    z = _z(conn, src, saat, depo)
    depo.yol.unlink()
    assert z.prev_politikasi_uygula() is False and depo.yedek_yolu.exists()
    depo.yol.write_text("")
    assert z.prev_politikasi_uygula() is False and depo.yedek_yolu.exists()
    depo.yol.write_text("tok-uc")
    assert z.prev_politikasi_uygula() is True


def test_prev_yoksa_veya_depo_yoksa_sessiz(conn, src, saat, tmp_path):
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz("x")
    assert _z(conn, src, saat, depo).prev_politikasi_uygula() is False
    assert TaramaZamanlayici(conn, src, KURUCU, saat).prev_politikasi_uygula() is False


def test_prev_ilk_basarili_yetkili_taramada_silinir(conn, src, saat, servis, tmp_path):
    src.oyuncu("p1", "P1")
    run(servis.oyuncu_ekle(G1, "p1", src))
    depo = _prev_yap(tmp_path, saat)
    z = _z(conn, src, saat, depo)
    run(z.tick_ve_bekle())
    assert not depo.yedek_yolu.exists(), "yeni token ilk başarılı taramayla teyit edildi: .prev silinir"
    assert depo.oku() == "tok-iki"


@pytest.mark.parametrize("hata", [TokenGecersiz("401"), SiteErisilemez("ag")])
def test_prev_basarisiz_taramada_silinmez(conn, src, saat, servis, tmp_path, hata):
    src.oyuncu("p1", "P1")
    run(servis.oyuncu_ekle(G1, "p1", src))
    depo = _prev_yap(tmp_path, saat)
    src.hatalar[("*", "*")] = hata
    z = _z(conn, src, saat, depo)
    run(z.tick_ve_bekle())
    assert depo.yedek_yolu.exists(), "başarısız tarama yeni tokenı teyit etmez; geri dönüş yolu korunur"


def test_prev_tarama_sirasinda_token_degisirse_o_tarama_teyit_sayilmaz(conn, src, saat, servis, tmp_path):
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G2)  # yalnız G1 taranır (G2'nin hızlı taraması sonucu bulandırmasın)
    src.oyuncu("p1", "P1")
    run(servis.oyuncu_ekle(G1, "p1", src))
    depo = _prev_yap(tmp_path, saat)
    z = _z(conn, src, saat, depo)

    async def sen():
        kapi = asyncio.Event()
        src.kapilar[("oyuncu", "p1")] = kapi
        gorevler = z.tick()
        await asyncio.sleep(0.01)
        depo.yaz("tok-uc")  # tarama uçuşta iken token yenilendi (yeni .prev oluşur)
        mt = depo.son_degisiklik()
        os.utime(depo.yol, (mt + 5, mt + 5))  # mtime kesin farklı olsun (dosya sistemi çözünürlüğüne bağlı kalma)
        kapi.set()
        await asyncio.gather(*gorevler)

    run(sen())
    assert depo.yedek_yolu.exists(), "eski tokenla başlayan tarama yeni tokenı teyit edemez"
    src.kapilar.clear()
    saat.ilerle(config.DEFAULT_SCAN_INTERVAL_MINUTES * 60 + 1)
    run(z.tick_ve_bekle())
    assert not depo.yedek_yolu.exists(), "yeni tokenla başlayıp biten sonraki tarama teyit eder"


def test_prev_zamanlayici_dongusu_politikayi_uygular(conn, src, saat, tmp_path, gonderici):
    depo = _prev_yap(tmp_path, saat, yas_sn=config.TOKEN_PREV_OMRU_SANIYE + 10)
    z = _z(conn, src, saat, depo)

    async def sen():
        durdur = asyncio.Event()
        g = asyncio.create_task(z.calis(gonderici, durdur))
        await asyncio.sleep(0.05)
        durdur.set()
        await g

    run(sen())
    assert not depo.yedek_yolu.exists()


def test_prev_politikasi_arayuz_mesajinda_ve_belgelerde(conn, src, saat, tmp_path):
    from test_telegram_arayuz import SahteDogrulayici, _token_mesaji, _token_ui as tui
    ui, depo, z = tui(conn, src, saat, tmp_path, SahteDogrulayici("gecerli"))
    run(ui.buton(dugme("tn")))
    y = _token_mesaji(ui, JWT)
    assert ".prev" in y.metin and "deleted automatically" in y.metin and "24" in y.metin
    kok = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    deploy = open(os.path.join(kok, "deploy", "README-DEPLOY.md"), encoding="utf-8").read()
    assert ".prev" in deploy and "TOKEN_PREV_OMRU" in deploy and "geri dön" in deploy.lower()


# ======================================================================= deploy tutarlılığı (dosya düzeyi)
KOK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _birim():
    return open(os.path.join(KOK, "deploy", "dipbot.service"), encoding="utf-8").read()


def test_deploy_birim_pythonpath_src_tanimli():
    """Paket src/ altında ve pip ile kurulmaz: PYTHONPATH olmadan `python -m dipbot` modül bulamaz."""
    satirlar = [l.strip() for l in _birim().splitlines()]
    assert "Environment=PYTHONPATH=/opt/dipbot/src" in satirlar
    assert any(l.startswith("ExecStart=/opt/dipbot/.venv/bin/python -m dipbot") for l in satirlar)


def test_deploy_pythonpath_ile_modul_yuklenir_ve_bos_ortamda_cikis_2():
    import subprocess
    import sys
    yol = [l.split("=", 2)[2] for l in _birim().splitlines() if l.startswith("Environment=PYTHONPATH=")][0]
    gercek = os.path.join(KOK, os.path.relpath(yol, "/opt/dipbot"))  # /opt/dipbot/src -> <kök>/src
    env = {"PYTHONPATH": gercek}  # BOT_TOKEN/OWNER_TELEGRAM_ID yok: ağa çıkmadan yapılandırma hatası
    r = subprocess.run([sys.executable, "-m", "dipbot"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "BOT_TOKEN" in r.stderr
    # PYTHONPATH'siz modül bulunamaz (birimde neden zorunlu olduğunun kanıtı)
    r2 = subprocess.run([sys.executable, "-I", "-m", "dipbot"], env={}, capture_output=True, text=True, timeout=30,
                        cwd="/")
    assert r2.returncode == 1 and "No module named dipbot" in r2.stderr


def test_deploy_betikleri_ve_belgeler_mevcut_ve_tutarli():
    for ad in ("backup.sh", "restore.sh", "onkontrol.sh"):
        assert os.path.isfile(os.path.join(KOK, "deploy", ad))
    kok_readme = open(os.path.join(KOK, "README.md"), encoding="utf-8").read()
    for anahtar in ("BOT_TOKEN", "OWNER_TELEGRAM_ID", ".prev", "aralıklı", "CANLI-DOGRULAMA-PLANI", "Doğrulanmadı",
                    "/grup_tasi", "systemd"):
        assert anahtar in kok_readme, anahtar
    gizli = ("eyJ",)  # gerçek JWT başlangıcı belgelerde bulunmamalı
    for ad in ("README.md", os.path.join("deploy", "README-DEPLOY.md"), os.path.join("deploy", "CANLI-DOGRULAMA-PLANI.md"),
               ".env.example"):
        icerik = open(os.path.join(KOK, ad), encoding="utf-8").read()
        assert not any(g in icerik for g in gizli), ad


# ----------------------------------------------------------------- QA-B O1: teyit gerçek bir oyuncu/parti gözlemi ister
def test_prev_takipsiz_grup_taramasi_yeni_tokeni_teyit_etmez(conn, src, saat, tmp_path):
    """Takip edilen oyuncu/parti yoksa tarama yalnız /api/wars isteği atar; bu uç noktanın kimlik
    gerektirdiği canlıda doğrulanmadı -> .prev silinmez."""
    depo = _prev_yap(tmp_path, saat)
    z = _z(conn, src, saat, depo)
    run(z.tick_ve_bekle())
    assert depo.yedek_yolu.exists()


def test_prev_savas_yapilandirma_hatasi_veya_onay_kalkmasi_teyit_saymaz(conn, src, saat, servis, tmp_path):
    from dipbot.kaynak import OzellikUygulanmadi
    src.oyuncu("p1", "P1")
    run(servis.oyuncu_ekle(G1, "p1", src))
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G2)
    depo = _prev_yap(tmp_path, saat)
    z = _z(conn, src, saat, depo)
    src.hatalar[("savaslar", None)] = OzellikUygulanmadi("savas yok")
    run(z.tick_ve_bekle())
    assert depo.yedek_yolu.exists()


# ----------------------------------------------------------------- QA-B K1: .env.example boş AUTH satırları
def _env_ornegi():
    env = {}
    for satir in open(os.path.join(KOK, ".env.example"), encoding="utf-8"):
        satir = satir.strip()
        if satir and not satir.startswith("#") and "=" in satir:
            k, v = satir.split("=", 1)
            env[k] = v
    return env


def test_env_ornegi_systemd_gibi_yuklenince_bearer_oneki_korunur():
    """systemd EnvironmentFile'da `KEY=` boş dize tanımlar; kod 'ayarlı ama boş' şemayı önek YOK sayar.
    .env.example doğrudan kurulduğundan, isteğe bağlı override satırları yorum olmalı."""
    from dipbot.diplomacia.istemci import OrtamKimlik
    env = _env_ornegi()
    assert "DIPLOMACIA_AUTH_SEMA" not in env and "DIPLOMACIA_AUTH_BASLIK" not in env
    kimlik = OrtamKimlik(TokenDeposu(os.path.join(KOK, "yok-token-dosyasi")), env)
    assert kimlik.yapilandirildi
    basliklar = {k.lower(): v for k, v in kimlik.basliklar_icin("aaa.bbb.ccc").items()}  # uydurma değer
    assert basliklar["authorization"] == "Bearer aaa.bbb.ccc"
