"""PTB bağlayıcısı (gerçek telegram.Update nesneleri, sahte Bot) + yapılandırma + yaşam döngüsü yardımcıları.

MOCK testler: ağ yok, getMe/polling yok. Canlı Telegram doğrulaması DEĞİLDİR.
"""
import asyncio

import pytest
from telegram import Update
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, MessageHandler,
)

from conftest import bildirimler, run
from tg_fakes import G1, G3, KURUCU, SahteBot, YABANCI, YETKILI, hizli_gonderici

from dipbot import yonetim
from dipbot.tg import arayuz as ar
from dipbot.tg.arayuz import ESKI_KOMUTLAR, TR_KOMUTLAR, Arayuz, KOMUTLAR
from dipbot.tg.calistir import gorevi_durdur, main
from dipbot.tg.kaynak_yok import YapilandirilmamisKaynak
from dipbot.tg.token import DogrulamaSonucu, TokenDeposu
from dipbot.tg.uygulama import IZINLI_GUNCELLEMELER, Baglayici, bot_komutlari, kayit_et
from dipbot.tg.yapilandirma import TelegramAyarlari, YapilandirmaHatasi

BOT_TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"


class Ctx:  # PTB bağlamı yerine
    error = None


def _kisi(uid):
    return {"id": uid, "is_bot": False, "first_name": "K"}


def _sohbet(cid, tur):
    return {"id": cid, "type": tur}


def update_komut(metin, uid, cid, tur, update_id=1):
    return Update.de_json(
        {"update_id": update_id, "message": {
            "message_id": 11, "date": 1760000000, "chat": _sohbet(cid, tur), "from": _kisi(uid), "text": metin,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(metin.split()[0])}]}}, None)


def update_metin(metin, uid, cid=None):
    return Update.de_json(
        {"update_id": 2, "message": {
            "message_id": 12, "date": 1760000000, "chat": _sohbet(cid or uid, "private"), "from": _kisi(uid), "text": metin}},
        None)


def update_dugme(veri, uid, cid, tur, mesaj_id=9):
    return Update.de_json(
        {"update_id": 3, "callback_query": {
            "id": "q1", "from": _kisi(uid), "chat_instance": "ci", "data": veri,
            "message": {"message_id": mesaj_id, "date": 1760000000, "chat": _sohbet(cid, tur), "text": "menü"}}}, None)


def update_uyelik(cid, uid, eski, yeni, tur="supergroup"):
    def uye(durum):
        d = {"status": durum, "user": {"id": 999, "is_bot": True, "first_name": "Bot", "username": "dipbot"}}
        if durum == "kicked":
            d["until_date"] = 0
        return d
    return Update.de_json(
        {"update_id": 4, "my_chat_member": {
            "chat": _sohbet(cid, tur), "from": _kisi(uid), "date": 1760000000,
            "old_chat_member": uye(eski), "new_chat_member": uye(yeni)}}, None)


@pytest.fixture
def bot():
    return SahteBot()


@pytest.fixture
def b(conn, saat, src, bot):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    arayuz = Arayuz(conn, src, KURUCU, saat=saat)
    return Baglayici(arayuz, hizli_gonderici(bot))


def komut_calistir(b, metin, uid, cid, tur):
    ad = metin.split()[0][1:].split("@")[0]
    run(b.komut_isle(update_komut(metin, uid, cid, tur), Ctx(), ad))


# ---------------------------------------------------------------- Update -> yanıt
def test_komut_update_html_ve_tuslarla_yanitlanir(b, bot):
    komut_calistir(b, "/help", KURUCU, KURUCU, "private")
    kw = bot.son("send_message")
    assert kw["chat_id"] == KURUCU and kw["parse_mode"] == "HTML" and "/addplayer" in kw["text"] and "/oyuncu_ekle" not in kw["text"]
    assert "/oyuncuekle" not in kw["text"]
    assert kw["reply_markup"] is not None


def test_komut_arguman_botadi_ile(b, bot, conn, src):
    """Bilinçli değişiklik: gruplar komut işlemez; @botadi'lı komut kurucunun ÖZEL sohbetinde (seçili liste G1) çalışır,
    aynı komut GRUPTA tamamen yok sayılır (yanıt yok, kayıt yok)."""
    src.oyuncu("72400", "Ali <b>")
    url = "https://diplomacia.com.tr/player/72400"
    komut_calistir(b, "/oyuncu_ekle@dipbot " + url, KURUCU, G1, "supergroup")  # grupta yok sayılır
    assert bot.say("send_message") == 0
    assert "72400" not in {r[0] for r in conn.execute("SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=?", (G1,))}
    b.arayuz._secili[KURUCU] = G1
    komut_calistir(b, "/oyuncu_ekle@dipbot " + url, KURUCU, KURUCU, "private")
    assert "72400" in {r[0] for r in conn.execute("SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=?", (G1,))}
    assert "Ali &lt;b&gt;" in bot.son("send_message")["text"]


def test_yetkisiz_komut_ve_kurucu_bildirimi(b, bot, conn):
    komut_calistir(b, "/oyuncular", YABANCI, YABANCI, "private")
    assert bot.son("send_message")["text"] == ar.KAPALI_OZEL
    assert len(bildirimler(conn, "yetkisiz_deneme", hedef=KURUCU)) == 1


def test_onaysiz_grupta_komut_yanitsiz(b, bot, conn):
    """Bilinçli değişiklik: onaysız grup artık yetkisiz-deneme kaydı da ÜRETMEZ (tüm gruplar sessizce yok sayılır)."""
    komut_calistir(b, "/oyuncular", KURUCU, G3, "supergroup")
    assert bot.say("send_message") == 0
    assert not bildirimler(conn, "yetkisiz_deneme")
    assert not conn.execute("SELECT 1 FROM yetkisiz_denemeler").fetchall()


def test_dugme_update_callback_yanitlanir_ve_mesaj_duzenlenir(b, bot):
    run(b.buton_isle(update_dugme("m", KURUCU, KURUCU, "private", mesaj_id=9), Ctx()))
    assert bot.say("answer_callback_query") == 1
    assert bot.son("answer_callback_query")["callback_query_id"] == "q1"
    e = bot.son("edit_message_text")
    assert e["message_id"] == 9 and e["chat_id"] == KURUCU and e["parse_mode"] == "HTML"
    assert bot.say("send_message") == 0


def test_yetkisiz_dugme_uyari_ile_yanitlanir_menu_acilmaz(b, bot, conn):
    """Bilinçli değişiklik: gruptaki düğme (yetkisiz kullanıcı DAHİL) uyarısız yanıtlanır: Baglayici callback'i
    metinsiz kapatır (spinner durur), mesaj/menü yok, yetkisiz kaydı/bildirimi yok. Özel sohbette yabancı kullanıcı
    eskisi gibi uyarı alır ve kayıt düşer."""
    run(b.buton_isle(update_dugme("ol:0", YABANCI, G1, "supergroup"), Ctx()))
    assert bot.say("answer_callback_query") == 1
    a = bot.son("answer_callback_query")
    assert a["show_alert"] is False and a["text"] is None
    assert bot.say("edit_message_text") == 0 and bot.say("send_message") == 0
    assert not bildirimler(conn, "yetkisiz_deneme")
    bot.cagrilar.clear()
    run(b.buton_isle(update_dugme("ol:0", YABANCI, YABANCI, "private"), Ctx()))  # özel sohbet: değişmedi
    a = bot.son("answer_callback_query")
    assert a["show_alert"] is True and a["text"]
    assert bot.say("edit_message_text") == 0 and bot.say("send_message") == 0
    assert bildirimler(conn, "yetkisiz_deneme")


def test_eski_dugme_anlasilir_cevap(b, bot):
    run(b.buton_isle(update_dugme("eskiBirSey:1", KURUCU, KURUCU, "private"), Ctx()))
    a = bot.son("answer_callback_query")
    assert a["text"] == ar.ESKI_DUGME and a["show_alert"] is True and bot.say("edit_message_text") == 0


def test_her_callbackte_tam_bir_answer(b, bot, src, conn):
    src.oyuncu("1", "A")
    b.arayuz._secili[KURUCU] = G1  # yönetim yalnız kurucunun özel sohbetinde; takip listesi = G1
    komut_calistir(b, "/oyuncu_ekle 1", KURUCU, KURUCU, "private")
    for veri in ("m", "ol:0", "od:1", "du", "yo", "ta", "tv:15", "sl:0", "yd", "nop", "yok", "pl:0", "os:1"):
        bot.cagrilar.clear()
        run(b.buton_isle(update_dugme(veri, KURUCU, KURUCU, "private"), Ctx()))
        assert bot.say("answer_callback_query") == 1, veri
    for veri in ("m", "ol:0", "sl:0", "yok"):  # gruptaki düğme: yine tam bir (metinsiz) answer, mesaj yok
        bot.cagrilar.clear()
        run(b.buton_isle(update_dugme(veri, KURUCU, G1, "supergroup"), Ctx()))
        assert bot.say("answer_callback_query") == 1, veri
        assert bot.say("edit_message_text") == 0 and bot.say("send_message") == 0, veri


def test_ag_cagrisi_oncesi_erken_ack_tek_answer(b, bot, src):
    # savaşlar kaynak çağrısı yapar: callback önce hemen yanıtlanır, sonra tekrar yanıtlanmaz
    b.arayuz._secili[KURUCU] = G1
    run(b.buton_isle(update_dugme("sl:0", KURUCU, KURUCU, "private"), Ctx()))
    assert bot.say("answer_callback_query") == 1
    sira = [a for a, _ in bot.cagrilar]
    assert sira.index("answer_callback_query") < sira.index("edit_message_text")


def test_token_mesaji_silinmeye_calisilir_ve_reddedilir(conn, saat, src, bot, tmp_path):
    depo = TokenDeposu(tmp_path / "t.token")
    depo.yaz("eski")
    arayuz = Arayuz(conn, src, KURUCU, saat=saat, token_deposu=depo)  # varsayılan doğrulayıcı: yapılandırılmadı
    b = Baglayici(arayuz, hizli_gonderici(bot))
    run(b.buton_isle(update_dugme("tn", KURUCU, KURUCU, "private"), Ctx()))
    bot.cagrilar.clear()
    run(b.metin_isle(update_metin("eyJGIZLI1.YENI02.TOKEN3", KURUCU), Ctx()))
    assert bot.say("delete_message") == 1 and bot.son("delete_message") == {"chat_id": KURUCU, "message_id": 12}
    yanit = bot.son("send_message")["text"]
    assert "NOT CHANGED" in yanit and "eyJGIZLI1.YENI02.TOKEN3" not in yanit
    assert depo.oku() == "eski"


def test_token_dogrulayici_basarili_akis_update_uzerinden(conn, saat, src, bot, tmp_path):
    class Gecerli:
        async def dogrula(self, token):
            return DogrulamaSonucu("gecerli")

    depo = TokenDeposu(tmp_path / "t.token")
    arayuz = Arayuz(conn, src, KURUCU, saat=saat, token_deposu=depo, dogrulayici=Gecerli())
    b = Baglayici(arayuz, hizli_gonderici(bot))
    run(b.buton_isle(update_dugme("tn", KURUCU, KURUCU, "private"), Ctx()))
    run(b.metin_isle(update_metin("eyJGECER1.YENI02.TOKEN3", KURUCU), Ctx()))
    assert depo.oku() == "eyJGECER1.YENI02.TOKEN3"
    assert "eyJGECER1.YENI02.TOKEN3" not in bot.son("send_message")["text"]


def test_yetkisiz_kisinin_metni_yoksayilir(b, bot):
    run(b.metin_isle(update_metin("merhaba", YABANCI), Ctx()))
    assert bot.say("send_message") == 0 and bot.say("delete_message") == 0


def test_my_chat_member_update_onaysiz_ekleme(b, conn):
    run(b.uyelik_isle(update_uyelik(G3, YABANCI, "left", "member"), Ctx()))
    v = bildirimler(conn, "yetkisiz_deneme", hedef=KURUCU)[0]["veri"]
    assert v["islem"] == "onaysiz_gruba_eklendi" and v["grup_id"] == G3 and v["kullanici_id"] == YABANCI


def test_my_chat_member_onayli_grup_bildirim_yok(b, conn):
    run(b.uyelik_isle(update_uyelik(G1, YABANCI, "kicked", "member"), Ctx()))
    assert not bildirimler(conn, "yetkisiz_deneme")


def test_migrate_mesaji_yeni_kimligi_yetkili_saymaz(b, conn, bot):
    yeni = -1009876543210
    u_eski = Update.de_json({"update_id": 5, "message": {
        "message_id": 1, "date": 1760000000, "chat": _sohbet(G1, "group"), "migrate_to_chat_id": yeni}}, None)
    u_yeni = Update.de_json({"update_id": 6, "message": {
        "message_id": 1, "date": 1760000000, "chat": _sohbet(yeni, "supergroup"), "migrate_from_chat_id": G1}}, None)
    run(b.goc_isle(u_eski, Ctx()))
    run(b.goc_isle(u_yeni, Ctx()))
    assert len(bildirimler(conn, "grup_goc", hedef=KURUCU)) == 1  # iki servis mesajı, tek bildirim
    assert yeni not in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    # Bilinçli değişiklik: yeni kimlik artık reddedilmez-kaydedilmez; gruplar tamamen sessizce yok sayılır.
    bot.cagrilar.clear()
    komut_calistir(b, "/oyuncular", KURUCU, yeni, "supergroup")
    assert bot.say("send_message") == 0
    assert not bildirimler(conn, "yetkisiz_deneme")


# ---------------------------------------------------------------- kayıt / allowed_updates
def test_handlerlar_kayit_edilir_ve_allowed_updates():
    app = Application.builder().token(BOT_TOKEN).build()
    b = Baglayici(Arayuz.__new__(Arayuz), None)
    kayit_et(app, b)
    isleyiciler = [h for grup in app.handlers.values() for h in grup]
    komutlar = {c for h in isleyiciler if isinstance(h, CommandHandler) for c in h.commands}
    # Legacy underscore names and the short Turkish names stay registered (backward compatibility) but are not in the menu.
    assert komutlar == set(KOMUTLAR) | set(ESKI_KOMUTLAR) | set(TR_KOMUTLAR)
    assert {"oyuncu_ekle", "oyuncuekle", "yardim", "grup_onayla", "addplayer", "help"} <= komutlar
    assert any(isinstance(h, CallbackQueryHandler) for h in isleyiciler)
    assert any(isinstance(h, ChatMemberHandler) and h.chat_member_types == ChatMemberHandler.MY_CHAT_MEMBER
               for h in isleyiciler)
    assert sum(isinstance(h, MessageHandler) for h in isleyiciler) == 2  # metin + migrate
    assert "my_chat_member" in IZINLI_GUNCELLEMELER and "callback_query" in IZINLI_GUNCELLEMELER
    assert app.error_handlers
    assert [c.command for c in bot_komutlari()] == list(KOMUTLAR)


# ---------------------------------------------------------------- yapılandırma / main
ORTAM = {"BOT_TOKEN": BOT_TOKEN, "OWNER_TELEGRAM_ID": "1000", "DB_PATH": "/var/lib/dipbot/dipbot.db"}


def test_yapilandirma_ortamdan(tmp_path):
    ta = TelegramAyarlari.from_env(ORTAM)
    assert ta.bot_token == BOT_TOKEN and ta.ayarlar.owner_telegram_id == 1000
    assert str(ta.ayarlar.db_path).replace("\\", "/") == "/var/lib/dipbot/dipbot.db"
    assert ta.token_dosyasi.name == "diplomacia.token" and ta.token_dosyasi.parent == ta.ayarlar.db_path.parent
    assert BOT_TOKEN not in repr(ta)
    ta2 = TelegramAyarlari.from_env({**ORTAM, "DIPLOMACIA_TOKEN_DOSYASI": str(tmp_path / "x")})
    assert ta2.token_dosyasi == tmp_path / "x"


@pytest.mark.parametrize("eksik", ["BOT_TOKEN", "OWNER_TELEGRAM_ID"])
def test_eksik_ortam_anlasilir_hata(eksik, capsys):
    env = {k: v for k, v in ORTAM.items() if k != eksik}
    with pytest.raises(YapilandirmaHatasi) as ei:
        TelegramAyarlari.from_env(env)
    assert eksik in str(ei.value)
    assert main(env) == 2
    err = capsys.readouterr().err
    assert eksik in err and "HATA" in err and BOT_TOKEN not in err


def test_gecersiz_bot_token_degeri_gosterilmez(capsys):
    kotu = "sahte-token-degeri-123"
    assert main({**ORTAM, "BOT_TOKEN": kotu}) == 2
    err = capsys.readouterr().err
    assert "biçimi geçersiz" in err and kotu not in err
    assert main({**ORTAM, "OWNER_TELEGRAM_ID": "abc"}) == 2


# ---------------------------------------------------------------- graceful kapanış yardımcısı
def test_gorevi_durdur_isbirlikci_gorev_iptal_edilmez():
    async def sen():
        olay = asyncio.Event()

        async def is_():
            await olay.wait()
            return "bitti"

        gorev = asyncio.create_task(is_())
        iptal = await gorevi_durdur(gorev, olay, 1.0)
        return iptal, gorev.result()

    assert run(sen()) == (False, "bitti")


def test_gorevi_durdur_takilan_gorev_iptal_edilir():
    async def sen():
        olay = asyncio.Event()

        async def takili():
            await asyncio.sleep(3600)

        gorev = asyncio.create_task(takili())
        iptal = await gorevi_durdur(gorev, olay, 0.05)
        return iptal, gorev.cancelled(), olay.is_set()

    assert run(sen()) == (True, True, True)
    assert run(gorevi_durdur(None, asyncio.Event(), 0.1)) is False


# ---------------------------------------------------------------- süreç yaşam döngüsü (sahte Application)
class _SahteUpdater:
    def __init__(self, app):
        self.app = app

    async def start_polling(self, **kw):
        self.app.olaylar.append(("start_polling", kw["allowed_updates"], kw["drop_pending_updates"]))
        if self.app.polling_sirasinda is not None:
            await self.app.polling_sirasinda(self.app)

    async def stop(self):
        self.app.olaylar.append("updater.stop")


class _SahteUygulama:
    """telegram.ext.Application yerine geçer; ağ yok."""

    def __init__(self, initialize_hatasi=None, polling_sirasinda=None):
        self.olaylar, self.hata_isleyici = [], None
        self.initialize_hatasi, self.polling_sirasinda = initialize_hatasi, polling_sirasinda
        self.updater = _SahteUpdater(self)
        self.bot = self
        self.username, self.can_read_all_group_messages = "dipbot", False

    async def set_my_commands(self, komutlar):
        self.olaylar.append(("set_my_commands", len(komutlar)))

    def add_handler(self, h, group=0):
        pass

    def add_error_handler(self, h):
        self.hata_isleyici = h

    async def initialize(self):
        self.olaylar.append("initialize")
        if self.initialize_hatasi:
            raise self.initialize_hatasi

    async def start(self):
        self.olaylar.append("start")

    async def stop(self):
        self.olaylar.append("stop")

    async def shutdown(self):
        self.olaylar.append("shutdown")


def _calistir_hazirla(monkeypatch, tmp_path, app):
    from dipbot import db
    from dipbot.tg import calistir as c

    class _Builder:
        def token(self, t):
            assert t == BOT_TOKEN
            return self

        def build(self):
            return app

    monkeypatch.setattr(c.Application, "builder", staticmethod(lambda: _Builder()))
    monkeypatch.setattr(c, "sinyalleri_bagla", lambda loop, olay: None)
    baglantilar = []
    gercek = db.baglan
    monkeypatch.setattr(c.db, "baglan", lambda yol: baglantilar.append(gercek(yol)) or baglantilar[-1])
    ta = TelegramAyarlari.from_env({**ORTAM, "OWNER_TELEGRAM_ID": "1000", "DB_PATH": str(tmp_path / "d" / "dipbot.db")})
    return c, ta, baglantilar


def test_calistir_conflict_ile_duzenli_kapanis(monkeypatch, tmp_path):
    from telegram.error import Conflict

    async def polling(app):
        ctx = Ctx()
        ctx.error = Conflict("terminated by other getUpdates request")
        await app.hata_isleyici(None, ctx)  # PTB hata işleyicisi -> fatal -> durdurma

    app = _SahteUygulama(polling_sirasinda=polling)
    c, ta, baglantilar = _calistir_hazirla(monkeypatch, tmp_path, app)
    kod = asyncio.run(asyncio.wait_for(c.calistir(ta), 20))
    assert kod == 3
    o = app.olaylar
    assert o[0] == "initialize" and o[1] == "start" and o[2][0] == "set_my_commands" and o[2][1] == len(KOMUTLAR)
    assert o[3] == ("start_polling", ["message", "callback_query", "my_chat_member"], False)
    assert o[-3:] == ["updater.stop", "stop", "shutdown"], o
    with pytest.raises(Exception):  # DB kapatılmış olmalı
        baglantilar[0].execute("SELECT 1")
    # yalnız DB_PATH dizinine yazıldı
    assert {p.name for p in (tmp_path / "d").iterdir()} <= {"dipbot.db", "dipbot.db-wal", "dipbot.db-shm"}


def test_calistir_gecersiz_bot_token_cikis_kodu_3(monkeypatch, tmp_path):
    from telegram.error import InvalidToken

    app = _SahteUygulama(initialize_hatasi=InvalidToken("x"))
    c, ta, baglantilar = _calistir_hazirla(monkeypatch, tmp_path, app)
    assert asyncio.run(asyncio.wait_for(c.calistir(ta), 20)) == 3
    assert "start_polling" not in [x if isinstance(x, str) else x[0] for x in app.olaylar]
    assert app.olaylar[-1] == "shutdown"
    with pytest.raises(Exception):
        baglantilar[0].execute("SELECT 1")
