"""Öncelik 1: gerçek adaptörün calistir'a bağlanması + savaş şeması hatasının zamanlayıcıda ele alınması.

MOCK (httpx.MockTransport / sahte kaynak); canlı doğrulama DEĞİLDİR.
"""
import asyncio
import logging

import httpx

from conftest import G1, KURUCU, bildirimler, run
from fakes import SahteKaynak
from tg_fakes import grup_komut

from dipbot.diplomacia.istemci import IstemciKaynagi, SavasSemasiDogrulanmadi
from dipbot.kaynak import SinirliKaynak
from dipbot.tg import calistir as c
from dipbot.tg.arayuz import Arayuz
from dipbot.tg.kaynak_yok import YapilandirilmamisKaynak, yapilandirildi_mi
from dipbot.tg.token import TokenDeposu
from dipbot.zamanlayici import TaramaZamanlayici

ORTAM = {"DIPLOMACIA_AUTH_BASLIK": "Authorization", "DIPLOMACIA_AUTH_SEMA": "Bearer"}


def test_kaynak_olustur_ortam_ve_token_varsa_gercek_adaptor(tmp_path):
    yol = tmp_path / "diplomacia.token"
    TokenDeposu(yol).yaz("T0KEN")
    k = c.kaynak_olustur(yol, ORTAM)
    assert isinstance(k, IstemciKaynagi) and yapilandirildi_mi(k)
    assert not isinstance(k, SinirliKaynak)  # sarma tüketicide, TEK kez


def test_kaynak_olustur_token_dosyasina_bagli_degil_yalniz_gecersiz_override_yapilandirilmamis(tmp_path):
    # Tur A: kaynak her zaman gerçek istemci (token her istekte okunur); dosya sonradan oluşabilir.
    yol = tmp_path / "diplomacia.token"
    assert isinstance(c.kaynak_olustur(yol, ORTAM), IstemciKaynagi)  # dosya yok
    TokenDeposu(yol).yaz("T0KEN")
    assert isinstance(c.kaynak_olustur(yol, {}), IstemciKaynagi)  # varsayılan Bearer
    assert isinstance(c.kaynak_olustur(yol, {"DIPLOMACIA_AUTH_BASLIK": "X-Token"}), IstemciKaynagi)
    assert isinstance(c.kaynak_olustur(yol, {"DIPLOMACIA_AUTH_SEMA": "Bearer"}), IstemciKaynagi)
    assert isinstance(c.kaynak_olustur(yol, {"DIPLOMACIA_AUTH_BASLIK": "Bad Header"}), YapilandirilmamisKaynak)


def test_durum_metni_gercek_adaptor_sabit_dogrulanmadi_notu_yok(conn, saat, tmp_path):
    """DELIBERATE CHANGE: the status screen no longer prints the fixed 'configured (identity format verified in DevTools,
    live connection not tested)' boilerplate; a healthy configured adapter adds no connection line at all."""
    yol = tmp_path / "t"
    TokenDeposu(yol).yaz("T0KEN")
    k = c.kaynak_olustur(yol, ORTAM)
    ui = Arayuz(conn, SinirliKaynak(k), KURUCU, saat=saat)
    y = run(ui.komut(grup_komut("durum"))).metin
    assert "Diplomacia connection" not in y
    assert "canlı bağlantı test edilmedi" not in y and "DevTools" not in y and "configured (" not in y
    assert "<b>Bot Status</b>" in y
    ui2 = Arayuz(conn, YapilandirilmamisKaynak(), KURUCU, saat=saat)
    assert "Diplomacia connection: not configured" in run(ui2.komut(grup_komut("durum"))).metin


def test_calistir_kapanista_istemciyi_kapatir(monkeypatch, tmp_path):
    from telegram.error import Conflict

    from test_telegram_uygulama import Ctx, _SahteUygulama, _calistir_hazirla

    # DELIBERATE CHANGE (multi-account): calistir artık tek 'kaynak' almaz; her hesabın istemcisi HesapYoneticisi.kapat() ile kapanır.
    kapandi = {"n": 0}
    from dipbot.hesaplar import HesapYoneticisi

    gercek_kapat = HesapYoneticisi.kapat

    async def kapat(self):
        kapandi["n"] += 1
        await gercek_kapat(self)

    monkeypatch.setattr(HesapYoneticisi, "kapat", kapat)

    async def polling(app):
        ctx = Ctx()
        ctx.error = Conflict("x")
        await app.hata_isleyici(None, ctx)

    app = _SahteUygulama(polling_sirasinda=polling)
    cm, ta, _ = _calistir_hazirla(monkeypatch, tmp_path, app)
    assert asyncio.run(asyncio.wait_for(cm.calistir(ta), 20)) == 3
    assert kapandi["n"] == 1


# ---- savaş şeması doğrulanmadı: zamanlayıcıda yapılandırma/geçici hata ----
def _kur(conn, saat, servis):
    src = SahteKaynak()
    src.oyuncu("p1", "Ayse")
    run(servis.oyuncu_ekle(G1, "p1", src))
    src.hatalar[("savaslar", None)] = SavasSemasiDogrulanmadi("savaş şeması doğrulanmadı")
    return src, TaramaZamanlayici(conn, src, KURUCU, saat)


def test_savas_semasi_oyuncu_parti_taramasini_etkilemez_ve_baseline_yok(conn, saat, servis, caplog):
    src, z = _kur(conn, saat, servis)
    src.oyuncular["p1"] = src.oyuncular["p1"].__class__("p1", "Ayse", "Roma", "Ulke1")  # değişiklik
    caplog.set_level(logging.DEBUG)
    run(z.tick_ve_bekle())
    sonuc = {r["is_tipi"]: r for r in conn.execute("SELECT * FROM tarama_isleri WHERE group_id=?", (G1,))}
    assert sonuc["oyuncular"]["son_basari"] and sonuc["partiler"]["son_basari"]
    assert sonuc["savaslar"]["son_basari"] is None and sonuc["savaslar"]["son_hata"]
    a = conn.execute("SELECT savas_baseline_tamam, ardisik_hata FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()
    assert a["savas_baseline_tamam"] == 0
    assert a["ardisik_hata"] == 0  # yapılandırma hatası oyuncu/parti ritmini geri çekmez
    assert bildirimler(conn, "yeni_savas") == []
    assert not z.token_duraklatildi


def test_savas_semasi_hata_logu_yagmuru_yok(conn, saat, servis, caplog):
    src, z = _kur(conn, saat, servis)
    caplog.set_level(logging.DEBUG)
    for _ in range(4):
        run(z.tick_ve_bekle())
        saat.ilerle(7200)
    uyarilar = [r for r in caplog.records if r.levelno >= logging.WARNING and "savaş" in r.getMessage().lower()]
    assert len(uyarilar) == 1
    assert bildirimler(conn, tur="token_gecersiz") == []
    # kurucuya/gruba hata mesajı kuyruğa girmedi
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
