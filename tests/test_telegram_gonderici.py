"""Telegram gönderici: HTML kaçış, mesaj bölme, hata sınıflandırması, hız sınırı, token deposu.

MOCK testler: sahte Bot, ağ yok. Canlı Telegram doğrulaması DEĞİLDİR.
"""
import json
import logging
import os
import sys
from datetime import timedelta
from html.parser import HTMLParser

import pytest
from telegram.error import (
    BadRequest, ChatMigrated, Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut,
)

from conftest import G1, KURUCU, bildirimler, run
from tg_fakes import SahteBot, SahteZaman, hizli_gonderici

from dipbot import outbox
from dipbot.outbox import GonderimHatasi
from dipbot.tg import bicim
from dipbot.tg.gonderici import HizSiniri, TelegramGonderici, siniflandir
from dipbot.tg.gunluk import SirFiltresi, gunluk_kur
from dipbot.tg.token import TokenDeposu, YapilandirilmamisDogrulayici
from dipbot.zaman import iso

EVIL = '<b>Ali</b> & "Veli" \'x\' <a href="javascript:1">t</a>'


class _EtiketDenetci(HTMLParser):
    """Telegram'ın kabul ettiği etiketlerle sınırlı ve dengeli mi?"""

    IZINLI = {"a", "b", "i", "code"}

    def __init__(self):
        super().__init__()
        self.yigin, self.hatalar = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.IZINLI:
            self.hatalar.append(f"izinsiz etiket {tag}")
        if tag == "a":
            href = dict(attrs).get("href", "")
            if not href.startswith("https://diplomacia.com.tr/"):
                self.hatalar.append(f"beklenmeyen href {href}")
        self.yigin.append(tag)

    def handle_endtag(self, tag):
        if not self.yigin or self.yigin.pop() != tag:
            self.hatalar.append(f"dengesiz {tag}")


def gecerli_html(metin: str):
    d = _EtiketDenetci()
    d.feed(metin)
    d.close()
    assert not d.hatalar and not d.yigin, d.hatalar + d.yigin


# ---------------------------------------------------------------- HTML kaçış
def test_esc_ozel_karakterler():
    assert bicim.esc(EVIL) == "&lt;b&gt;Ali&lt;/b&gt; &amp; &quot;Veli&quot; &#x27;x&#x27; &lt;a href=&quot;javascript:1&quot;&gt;t&lt;/a&gt;"
    assert bicim.esc(None) == "" and bicim.esc(5) == "5"


def test_baglanti_ad_ve_kimlik_kacirilir():
    m = bicim.oyuncu_link('7"><script>', EVIL)
    assert "<script>" not in m and "<b>Ali</b>" not in m
    gecerli_html(m)
    assert bicim.oyuncu_url(72400) == "https://diplomacia.com.tr/player/72400"
    assert bicim.parti_url(5830) == "https://diplomacia.com.tr/party/5830"


def test_bildirim_metinleri_ozel_karakterli_adlarla_gecerli_html():
    tespit = "2026-10-19T12:00:00.000000Z"
    bildirimler_ = {
        "oyuncu_degisti": {"oyuncu_id": "72400", "ad": EVIL, "tespit": tespit,
                           "degisimler": [{"alan": "bolge", "eski": EVIL, "yeni": "<Y>"}]},
        "parti_ad_degisti": {"parti_id": "5830", "eski_ad": EVIL, "yeni_ad": "<Yeni>", "tespit": tespit},
        "parti_uyelik": {"parti_id": "5830", "parti_ad": EVIL, "tespit": tespit,
                         "katilanlar": [{"oyuncu_id": "1", "ad": EVIL}], "ayrilanlar": [{"oyuncu_id": "2", "ad": None}]},
        "yeni_savas": {"savas_id": "s1", "ad": EVIL, "tespit": tespit},
        "yetkisiz_deneme": {"kullanici_id": 77, "grup_id": -1003, "islem": "/x<y>", "zaman": tespit},
        "token_gecersiz": {"zaman": tespit},
        "grup_goc": {"eski": -1, "yeni": -2, "zaman": tespit},
        "bilinmeyen_tur<": {},
    }
    for tur, veri in bildirimler_.items():
        m = bicim.bildirim_metni(tur, veri)
        gecerli_html(m)
        assert "<script>" not in m and "<b>Ali</b>" not in m, tur


def test_oyuncu_degisti_tek_alan_ve_zaman_istanbul():
    m = bicim.bildirim_metni("oyuncu_degisti", {
        "oyuncu_id": "72400", "ad": "Örnek Oyuncu", "tespit": "2026-10-19T12:20:00.000000Z",
        "degisimler": [{"alan": "bolge", "eski": "Varna", "yeni": "İstanbul"}]})
    assert "📍 Oyuncu bölge değiştirdi" in m
    assert "Önceki bölge: Varna" in m and "Yeni bölge: İstanbul" in m
    assert "Tespit: 19.10.2026 15:20" in m
    assert "kesin değişiklik zamanı değildir" in m
    assert 'href="https://diplomacia.com.tr/player/72400"' in m


def test_oyuncu_degisti_birden_fazla_alan_tek_mesaj():
    m = bicim.bildirim_metni("oyuncu_degisti", {
        "oyuncu_id": "1", "ad": "A", "tespit": "2026-10-19T12:20:00.000000Z",
        "degisimler": [{"alan": "bolge", "eski": "a", "yeni": "b"},
                       {"alan": "vatandaslik_ulkesi", "eski": "c", "yeni": "d"}]})
    assert "Oyuncu bilgileri değişti" in m
    assert "Önceki bölge: a" in m and "Yeni vatandaşlık ülkesi: d" in m


def test_onaysiz_ekleme_bildirimi_onay_komutunu_icerir():
    m = bicim.bildirim_metni("yetkisiz_deneme", {"kullanici_id": 77, "grup_id": -1003,
                                                  "islem": "onaysiz_gruba_eklendi", "zaman": "2026-10-19T12:00:00.000000Z"})
    assert "onaysız bir gruba eklendi" in m and "<code>/grup_onayla -1003</code>" in m


# ---------------------------------------------------------------- bölme
def test_bol_kisa_metin_tek_parca():
    assert bicim.bol("merhaba") == ["merhaba"]


def test_bol_satir_sinirinda_ve_sinir_asilmaz():
    satirlar = [f"Satır {i}: " + "ö" * 90 for i in range(200)]
    parcalar = bicim.bol("\n".join(satirlar))
    assert len(parcalar) > 1
    assert all(len(p) <= bicim.BOLME_HEDEFI for p in parcalar)
    assert all(len(p) <= bicim.MESAJ_AZAMI for p in parcalar)
    assert "\n".join(parcalar).split("\n") == satirlar, "içerik kayıpsız ve sıralı olmalı"


def test_bol_tek_dev_satir_etiket_ortasindan_bolunmez():
    dev = "<b>" + ("&lt;x&gt; " * 2000) + "</b>"
    parcalar = bicim.bol(dev)
    assert len(parcalar) > 1 and all(len(p) <= bicim.BOLME_HEDEFI for p in parcalar)
    for p in parcalar:
        gecerli_html(p)
        assert not p.rstrip().endswith("&") and "&lt" not in p.replace("&lt;", "")


def test_bol_gecersiz_sinir():
    with pytest.raises(ValueError):
        bicim.bol("x", 5)
    with pytest.raises(ValueError):
        bicim.bol("x", 5000)


def test_gonder_uzun_mesaj_birden_cok_send_message_ve_html():
    bot = SahteBot()
    g = hizli_gonderici(bot)
    kat = [{"oyuncu_id": str(i), "ad": f"Oyuncu <{i}> " + "ü" * 40} for i in range(300)]
    run(g.gonder(G1, "parti_uyelik", {"parti_id": "1", "parti_ad": "P", "katilanlar": kat, "ayrilanlar": [],
                                      "tespit": "2026-10-19T12:00:00.000000Z"}))
    assert bot.say("send_message") >= 2
    for _, kw in bot.cagrilar:
        assert kw["parse_mode"] == "HTML" and kw["chat_id"] == G1
        assert len(kw["text"]) <= 4096
        gecerli_html(kw["text"])
        assert kw["link_preview_options"].is_disabled


# ---------------------------------------------------------------- hata sınıflandırması
@pytest.mark.parametrize(
    "hata, tur, kalici",
    [
        (RetryAfter(5), "retry_after", False),
        (TimedOut(), "gecici", False),
        (NetworkError("x"), "gecici", False),
        (BadRequest("Chat not found"), "kalici", True),  # NetworkError alt sınıfı olsa da KALICI
        (Forbidden("bot was blocked by the user"), "kalici", True),
        (ChatMigrated(-100123), "goc", True),
        (InvalidToken("x"), "fatal", True),
        (Conflict("x"), "fatal", True),
        (TelegramError("bilinmeyen"), "gecici", False),
        (ValueError("alakasız"), "gecici", False),
    ],
)
def test_hata_siniflandirma_istisna_sinifina_gore(hata, tur, kalici):
    s = siniflandir(hata)
    assert (s.tur, s.kalici) == (tur, kalici)


def test_hata_siniflandirma_metne_bakmaz():
    # Aynı sınıf, tamamen farklı metin -> aynı karar
    assert siniflandir(Forbidden("herhangi bir şey")).kalici
    assert siniflandir(Forbidden("bot was kicked from the group chat")).kalici
    assert not siniflandir(TimedOut("chat not found")).kalici  # metin 'kalıcı' çağrıştırsa da geçici
    assert siniflandir(BadRequest("timed out")).kalici


def test_retry_after_saniye_ve_timedelta():
    assert siniflandir(RetryAfter(7)).bekle_saniye == 7
    assert siniflandir(RetryAfter(timedelta(seconds=9))).bekle_saniye == 9
    assert siniflandir(ChatMigrated(-1009)).yeni_sohbet_id == -1009


@pytest.mark.parametrize(
    "hata, kalici, bekle",
    [(RetryAfter(3), False, 3), (TimedOut(), False, None), (Forbidden("x"), True, None), (BadRequest("x"), True, None)],
)
def test_gonder_hatalari_gonderim_hatasina_cevirir(hata, kalici, bekle):
    bot = SahteBot()
    bot.hatalar.append(hata)
    g = hizli_gonderici(bot)
    with pytest.raises(GonderimHatasi) as ei:
        run(g.gonder(G1, "yeni_savas", {"savas_id": "1", "tespit": "2026-10-19T12:00:00.000000Z"}))
    assert ei.value.kalici is kalici and ei.value.bekle_saniye == bekle


def test_chat_migrated_geri_cagri_ve_kalici():
    bot, gorulen = SahteBot(), []
    bot.hatalar.append(ChatMigrated(-100777))
    g = hizli_gonderici(bot, goc_geri_cagrisi=lambda eski, yeni: gorulen.append((eski, yeni)))
    with pytest.raises(GonderimHatasi) as ei:
        run(g.gonder(G1, "yeni_savas", {"savas_id": "1"}))
    assert ei.value.kalici and gorulen == [(G1, -100777)]


def test_outbox_entegrasyonu_retry_after_beklenir_forbidden_kalici(conn, saat):
    for anahtar in ("a", "b", "c"):
        # b ayrı sohbette (RetryAfter alan G1'deki satırlar aynı turda ertelenir; diğer sohbetler etkilenmez)
        outbox.kuyruga_ekle(conn, hedef_sohbet={"a": G1, "b": 99, "c": -1002}[anahtar],
                            grup_id={"a": G1, "b": None, "c": -1002}[anahtar],
                            tur="yeni_savas", idem_anahtari=anahtar,
                            veri={"savas_id": anahtar, "tespit": iso(saat())}, simdi=saat())
    bot = SahteBot()
    bot.hatalar += [RetryAfter(42), Forbidden("x"), TimedOut()]
    ozet = run(outbox.bekleyenleri_gonder(conn, hizli_gonderici(bot), saat))
    assert (ozet.gonderilen, ozet.yeniden_denenecek, ozet.basarisiz) == (0, 2, 1)
    satirlar = {r["idem_anahtari"]: r for r in conn.execute("SELECT * FROM outbox")}
    assert satirlar["a"]["durum"] == "bekliyor" and satirlar["a"]["sonraki_deneme"] == iso(saat() + timedelta(seconds=42))
    assert satirlar["b"]["durum"] == "basarisiz" and "Forbidden" in satirlar["b"]["son_hata"]
    assert satirlar["c"]["durum"] == "bekliyor" and "TimedOut" in satirlar["c"]["son_hata"]


def test_outbox_hata_metninde_token_yok(conn, saat):
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="x",
                        veri={"savas_id": "1"}, simdi=saat())
    bot = SahteBot()
    bot.hatalar.append(BadRequest("hata https://api.telegram.org/bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/sendMessage"))
    run(outbox.bekleyenleri_gonder(conn, hizli_gonderici(bot), saat))
    hata = conn.execute("SELECT son_hata FROM outbox").fetchone()[0]
    assert "AAAAAAAAAAAAAAAAAAAA" not in hata and "<GIZLI>" in hata


# ---------------------------------------------------------------- hız sınırı
def test_hiz_siniri_sohbette_saniyede_bir():
    z = SahteZaman()
    s = HizSiniri(saat=z, uyku=z.uyku)

    async def hepsi():
        for _ in range(4):
            await s.bekle(5)  # özel sohbet (pozitif)

    run(hepsi())
    assert z.t == pytest.approx(3.0)  # ilk anında, sonrakiler 1 sn arayla


def test_hiz_siniri_grupta_dakikada_20():
    z = SahteZaman()
    s = HizSiniri(saat=z, uyku=z.uyku)

    async def hepsi():
        zamanlar = []
        for _ in range(21):
            await s.bekle(-1001)
            zamanlar.append(z.t)
        return zamanlar

    zamanlar = run(hepsi())
    assert zamanlar[19] == pytest.approx(19.0)  # ilk 20: 1 sn aralıkla
    assert zamanlar[20] >= zamanlar[0] + 60.0, "21. mesaj ilk mesajdan 60 sn sonra"
    # herhangi 60 sn'lik pencerede <=20
    for i in range(len(zamanlar) - 20):
        assert zamanlar[i + 20] - zamanlar[i] >= 60.0


def test_hiz_siniri_retry_after_sohbeti_erteler():
    z = SahteZaman()
    s = HizSiniri(saat=z, uyku=z.uyku)
    s.ertele(-1001, 30)
    run(s.bekle(-1001))
    assert z.t >= 30
    z0 = z.t
    run(s.bekle(-1002))  # başka sohbet etkilenmez
    assert z.t == z0


def test_etkilesimli_yanit_kisa_retry_afterda_bir_kez_yeniden_dener():
    bot = SahteBot()
    bot.hatalar.append(RetryAfter(2))
    g = hizli_gonderici(bot)
    run(g.yanit_gonder(G1, "merhaba"))
    assert bot.say("send_message") == 2 and bot.cagrilar[-1][1]["text"] == "merhaba"
    # uzun RetryAfter etkileşimde bekletmez, hata fırlatır
    bot2 = SahteBot()
    bot2.hatalar.append(RetryAfter(500))
    with pytest.raises(GonderimHatasi):
        run(hizli_gonderici(bot2).yanit_gonder(G1, "x"))


def test_duzenleme_basarisizsa_sessiz_gecer_callback_yanitlanir():
    bot = SahteBot()
    bot.hatalar.append(BadRequest("message is not modified"))
    g = hizli_gonderici(bot)
    run(g.yanit_gonder(G1, "x", duzenle_mesaj_id=5))  # fırlatmaz
    assert bot.say("edit_message_text") == 1 and bot.say("send_message") == 0
    bot.hatalar.append(TimedOut())
    assert run(g.callback_yanitla("q1", "x" * 500, True)) is False  # hata yutulur
    assert run(g.callback_yanitla("q1", "x" * 500, True)) is True
    assert len(bot.son("answer_callback_query")["text"]) == 200


# ---------------------------------------------------------------- token deposu
def test_token_deposu_atomik_yazim_ve_izin(tmp_path):
    d = TokenDeposu(tmp_path / "alt" / "diplomacia.token")
    assert not d.var_mi() and d.oku() is None
    d.yaz("birinci")
    assert d.oku() == "birinci"
    d.yaz("ikinci")
    assert d.oku() == "ikinci"
    # Tur A: yazmadan önce mevcut dosya 0600 .prev olarak yedeklenir; geçici dosya kalmamalı
    assert sorted(p.name for p in d.yol.parent.iterdir()) == ["diplomacia.token", "diplomacia.token.prev"]
    assert d.yedek_yolu.read_text(encoding="utf-8") == "birinci"
    if sys.platform != "win32":
        assert (os.stat(d.yol).st_mode & 0o777) == 0o600
    with pytest.raises(ValueError):
        d.yaz("içinde boşluk")
    with pytest.raises(ValueError):
        d.yaz("")
    assert d.oku() == "ikinci"


def test_token_deposu_yazim_hatasinda_eski_dosya_korunur(tmp_path, monkeypatch):
    d = TokenDeposu(tmp_path / "t.token")
    d.yaz("eski")

    def boz(src, dst):
        raise OSError("disk dolu")

    monkeypatch.setattr(os, "replace", boz)
    with pytest.raises(OSError):
        d.yaz("yeni")
    monkeypatch.undo()
    assert d.oku() == "eski"
    assert [p.name for p in tmp_path.iterdir()] == ["t.token"]


def test_varsayilan_dogrulayici_reddeder():
    s = run(YapilandirilmamisDogrulayici().dogrula("herhangi"))
    assert not s.gecerli and s.durum == "yapilandirilmadi"


# ---------------------------------------------------------------- günlük maskeleme
class _Toplayici(logging.Handler):
    def __init__(self):
        super().__init__()
        self.cikti = []

    def emit(self, record):
        self.cikti.append(self.format(record))


SAHTE_BOT_TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"


def test_log_suzgeci_token_maskeler_url_ve_traceback():
    t = _Toplayici()
    t.addFilter(SirFiltresi(SAHTE_BOT_TOKEN))
    t.setFormatter(logging.Formatter("%(name)s %(message)s"))
    lg = logging.getLogger("test.sir")
    lg.handlers[:] = [t]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    lg.info("HTTP Request: POST https://api.telegram.org/bot%s/getUpdates", SAHTE_BOT_TOKEN)
    lg.info("ham token: %s", SAHTE_BOT_TOKEN)
    try:
        raise RuntimeError(f"istek hatası https://api.telegram.org/bot{SAHTE_BOT_TOKEN}/sendMessage")
    except RuntimeError:
        lg.exception("patladı")
    tum = "\n".join(t.cikti)
    assert "AAAbbbCCC" not in tum and SAHTE_BOT_TOKEN not in tum
    assert "bot<GIZLI>/getUpdates" in tum and "patladı" in tum and "RuntimeError" in tum


def test_gunluk_kur_httpx_seviyesini_yukseltir():
    kok = logging.getLogger()
    eski_h, eski_s = list(kok.handlers), kok.level
    try:
        gunluk_kur(SAHTE_BOT_TOKEN)
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert any(isinstance(f, SirFiltresi) for h in kok.handlers for f in h.filters)
    finally:
        kok.handlers[:] = eski_h
        kok.setLevel(eski_s)
