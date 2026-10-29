"""Tur B düzeltmeleri (O-c, O-d, D1-D10, A5). MOCK testler; canlı doğrulama değildir."""
from datetime import timedelta

import pytest
from telegram.error import RetryAfter

from conftest import G1, KURUCU, run
from tg_fakes import SahteBot, SahteZaman, hizli_gonderici

from dipbot import outbox
from dipbot.zaman import iso, iso_coz


def _ekle(conn, saat, sohbet, anahtar, grup=None):
    outbox.kuyruga_ekle(conn, hedef_sohbet=sohbet, grup_id=grup, tur="yeni_savas", idem_anahtari=anahtar,
                        veri={"war_number": 1, "ad": "x"}, simdi=saat())


# ------------------------------------------------------------------ O-c
def test_retry_after_bir_sohbeti_bekletir_diger_sohbet_ayni_turda_gonderilir(conn, saat):
    for k in ("a1", "a2"):
        _ekle(conn, saat, 5, k)
    _ekle(conn, saat, 6, "b1")
    bot = SahteBot()
    bot.hatalar.append(RetryAfter(600))  # ilk çağrı (sohbet 5) RetryAfter
    z = SahteZaman()
    from dipbot.tg.gonderici import HizSiniri, TelegramGonderici
    g = TelegramGonderici(bot, siniri=HizSiniri(saat=z, uyku=z.uyku), uyku=z.uyku)
    ozet = run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert z.t < 60, f"outbox uzun uyudu: {z.t}"
    assert [kw["chat_id"] for _, kw in bot.cagrilar] == [5, 6], "2. satır (sohbet 5) denenmemeli"
    d = {r["idem_anahtari"]: r for r in conn.execute("SELECT * FROM outbox")}
    assert d["b1"]["durum"] == "gonderildi" and ozet.gonderilen == 1
    for k in ("a1", "a2"):
        assert d[k]["durum"] == "bekliyor"
        assert iso_coz(d[k]["sonraki_deneme"]) >= saat() + timedelta(seconds=599)


def test_hiz_siniri_penceresi_uzunsa_uyumaz_bekle_saniyeli_hata_doner(conn, saat):
    _ekle(conn, saat, 5, "a1")
    _ekle(conn, saat, 6, "b1")
    bot = SahteBot()
    z = SahteZaman()
    from dipbot.tg.gonderici import HizSiniri, TelegramGonderici
    s = HizSiniri(saat=z, uyku=z.uyku)
    s.ertele(5, 600)  # önceki turda RetryAfter alınmış
    g = TelegramGonderici(bot, siniri=s, uyku=z.uyku)
    run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert z.t < 60
    assert [kw["chat_id"] for _, kw in bot.cagrilar] == [6]
    r = conn.execute("SELECT * FROM outbox WHERE idem_anahtari='a1'").fetchone()
    assert r["durum"] == "bekliyor" and r["deneme"] == 0
    assert iso_coz(r["sonraki_deneme"]) >= saat() + timedelta(seconds=500)


# ------------------------------------------------------------------ D3
def test_ucustaki_gonderim_grup_kaldirmanin_iptalini_ezmez(conn, saat):
    _ekle(conn, saat, G1, "x1", grup=G1)

    class G:
        async def gonder(self, sohbet, tur, veri):
            outbox.grup_bekleyenlerini_iptal_et(conn, G1)  # uçuş sırasında /grup_kaldir

    run(outbox.bekleyenleri_gonder(conn, G(), saat))
    assert conn.execute("SELECT durum FROM outbox").fetchone()[0] == "iptal"


# ------------------------------------------------------------------ O-d
def _ui_ve_zamanlayici(conn, saat, src):
    from dipbot.tg.arayuz import Arayuz
    from dipbot.zamanlayici import TaramaZamanlayici
    z = TaramaZamanlayici(conn, src, KURUCU, saat)
    ui = Arayuz(conn, z._kaynak, KURUCU, saat=saat, zamanlayici=z)
    return ui, z


def test_arayuz_ve_zamanlayici_ayni_sinirli_kaynagi_paylasir(conn, saat, src):
    ui, z = _ui_ve_zamanlayici(conn, saat, src)
    assert getattr(ui.kaynak, "_sinirli", None) is z.kaynak


def test_arayuz_token_duraklatmasinda_aga_cikmaz_ve_acik_mesaj_verir(conn, saat, src):
    from tg_fakes import grup_komut
    src.oyuncu("5")
    ui, z = _ui_ve_zamanlayici(conn, saat, src)
    z._token_duraklat()
    y = run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    assert src.cagri_say("oyuncu") == 0
    assert "owner must renew it" in y.metin


def test_arayuz_istek_siniri_penceresinde_aga_cikmaz(conn, saat, src):
    from tg_fakes import grup_komut
    from dipbot.kaynak import IstekSiniri
    src.oyuncu("5")
    ui, z = _ui_ve_zamanlayici(conn, saat, src)
    z._istek_siniri(IstekSiniri("x", 120))
    y = run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    assert src.cagri_say("oyuncu") == 0
    assert "rate limit" in y.metin and "try again in" in y.metin


def test_arayuzde_yakalanan_token_gecersiz_zamanlayiciyi_duraklatir_tek_bildirim(conn, saat, src):
    from tg_fakes import grup_komut
    from conftest import bildirimler
    from dipbot.kaynak import TokenGecersiz
    src.oyuncu("5")
    src.hatalar[("oyuncu", "5")] = TokenGecersiz("x")
    ui, z = _ui_ve_zamanlayici(conn, saat, src)
    y = run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    assert z.token_duraklatildi
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    assert src.cagri_say("oyuncu") == 1, "duraklatma sonrası ağa çıkılmamalı"
    assert len(bildirimler(conn, tur="token_gecersiz")) == 1


def test_arayuzde_erisim_reddi_sonrasi_geri_cekilme_penceresinde_aga_cikmaz(conn, saat, src):
    from tg_fakes import grup_komut
    from dipbot.kaynak import ErisimReddedildi
    src.oyuncu("5")
    src.hatalar[("oyuncu", "5")] = ErisimReddedildi("403")
    ui, z = _ui_ve_zamanlayici(conn, saat, src)
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    y = run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    assert src.cagri_say("oyuncu") == 1
    assert "Diplomacia connection unavailable" in y.metin and not z.token_duraklatildi


# ------------------------------------------------------------------ D6 / D10
@pytest.mark.parametrize("kimlik", [1087968824, 136817688])
def test_yetki_ver_ozel_telegram_kimliklerini_reddeder(conn, saat, kimlik):
    from dipbot import yonetim
    with pytest.raises(yonetim.YonetimHatasi):
        yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, kimlik)
    assert conn.execute("SELECT COUNT(*) FROM yetkili_kullanicilar WHERE user_id=?", (kimlik,)).fetchone()[0] == 0


def test_yetki_ver_komutu_ozel_kimlikte_anlasilir_mesaj_verir(conn, saat, src):
    from tg_fakes import komut
    ui, _ = _ui_ve_zamanlayici(conn, saat, src)
    # DELIBERATE CHANGE: /grant (yetki_ver) was removed from Telegram, so the command is not dispatched at all (None);
    # the service layer still rejects special Telegram IDs (see the test above) and nothing is granted.
    assert run(ui.komut(komut("yetki_ver", "1087968824"))) is None
    assert run(ui.komut(komut("grant", "1087968824"))) is None
    assert conn.execute("SELECT COUNT(*) FROM yetkili_kullanicilar WHERE user_id=?", (1087968824,)).fetchone()[0] == 0


def test_tam_sayi_asiri_buyuk_degeri_reddeder():
    from dipbot.tg.arayuz import _tam_sayi
    assert _tam_sayi("99999999999999999999") is None
    assert _tam_sayi("-99999999999999999999") is None
    assert _tam_sayi(str(2**63 - 1)) == 2**63 - 1
    assert _tam_sayi("123") == 123
