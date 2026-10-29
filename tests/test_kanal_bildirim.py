"""Bildirimler YALNIZ log kanalına gider; eski gruplar komut işlemez/bildirim almaz; kanal Telegram API ile doğrulanır;
bekleyen eski grup mesajları kanala SIZMAZ; takip verisi silinmez/birleştirilmez; hangi listenin kullanılacağı raporlanır.

MOCK testler: sahte bot/kaynak, bellek içi SQLite, ağ yok. Gerçek Telegram kanalı doğrulaması DEĞİLDİR."""
import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

from conftest import bildirimler, run, takipte
from fakes import SahteGonderici, SahteSaat
from tg_fakes import G1, G2, G3, KURUCU, YABANCI, YETKILI, SahteBot, gercek_grup_dugme, gercek_grup_komut, grup_komut, komut

from dipbot import config, db, outbox, rapor, yetki, yonetim
from dipbot.outbox import GonderimHatasi
from dipbot.tg.arayuz import Arayuz
from dipbot.tg.gonderici import TelegramGonderici
from dipbot.tg.kanal import KanalDogrulayici
from dipbot.tg.yapilandirma import TelegramAyarlari, YapilandirmaHatasi
from dipbot.zamanlayici import TaramaZamanlayici
from dipbot.zaman import iso

KANAL = -1003998002227
BOT_TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"
ORTAM = {"BOT_TOKEN": BOT_TOKEN, "OWNER_TELEGRAM_ID": "1000", "DB_PATH": "/tmp/x/dipbot.db"}


# ---------------------------------------------------------------- yapılandırma
def test_varsayilan_kanal_ve_bos_aktif_liste():
    assert config.LOG_KANAL_ID == KANAL
    ta = TelegramAyarlari.from_env(ORTAM)
    assert ta.kanal_id == KANAL
    assert ta.aktif_takip_listeleri == frozenset(), "hiçbir liste kurucu seçmeden taranmaz"


def test_ortamdan_kanal_ve_liste_secimi():
    ta = TelegramAyarlari.from_env({**ORTAM, "LOG_CHANNEL_ID": "-1001234567890", "TRACKING_GROUP_IDS": "-1004375821471, -1004485206993"})
    assert ta.kanal_id == -1001234567890
    assert ta.aktif_takip_listeleri == frozenset({-1004375821471, -1004485206993})


@pytest.mark.parametrize("ad,deger", [("LOG_CHANNEL_ID", "12345"), ("LOG_CHANNEL_ID", "abc"), ("TRACKING_GROUP_IDS", "x"),
                                      ("TRACKING_GROUP_IDS", "-1001,0")])
def test_gecersiz_kanal_veya_liste_yapilandirmasi_reddedilir(ad, deger):
    with pytest.raises(YapilandirmaHatasi):
        TelegramAyarlari.from_env({**ORTAM, ad: deger})


# ---------------------------------------------------------------- bildirimler yalnız kanala
def test_takip_bildirimleri_yalniz_log_kanalina_kuyruga_alinir(conn, servis, src, saat):
    src.oyuncu("1", "Ali", "Varna", "Bulgaria")
    run(servis.oyuncu_ekle(G1, "1", src))
    run(servis.oyuncular_tara(G1, src))
    src.oyuncu("1", "Ali", "Sofia", "Bulgaria")
    run(servis.oyuncular_tara(G1, src))
    src.parti("5", "P", [("1", "Ali")])
    run(servis.parti_ekle(G1, "5", src))
    src.parti("5", "P2", [("1", "Ali"), ("2", "Bob")])
    run(servis.partiler_tara(G1, src))
    satirlar = bildirimler(conn)
    assert {s["tur"] for s in satirlar} >= {"oyuncu_degisti", "parti_ad_degisti", "parti_uyelik"}
    for s in satirlar:
        assert s["hedef_sohbet"] == KANAL, s["tur"]
        assert s["grup_id"] == G1, "grup_id yalnız takip LİSTESİNİN kimliği (onay dönemi / idempotency)"
    assert not [s for s in satirlar if s["hedef_sohbet"] in (G1, G2, G3)]


def test_yeni_savas_bildirimleri_de_kanala_gider(conn, servis, src, saat):
    from dipbot.kaynak import SavasOzet

    src.savaslar = [SavasOzet("w0", "War 0")]
    run(servis.savaslari_tara(G1, src))  # baseline
    src.savaslar += [SavasOzet(f"w{i}", f"War {i}") for i in range(1, 4)]
    run(servis.savaslari_tara(G1, src))
    s = bildirimler(conn, "yeni_savas")
    assert s and all(x["hedef_sohbet"] == KANAL for x in s)


def test_kanal_hedefi_servis_parametresiyle_degistirilebilir(conn, src, saat):
    from dipbot.takip import TakipServisi

    sv = TakipServisi(conn, saat, kanal_id=-1009)
    src.oyuncu("1", "Ali", "Varna")
    run(sv.oyuncu_ekle(G1, "1", src))
    run(sv.oyuncular_tara(G1, src))
    src.oyuncu("1", "Ali", "Sofia")
    run(sv.oyuncular_tara(G1, src))
    assert {s["hedef_sohbet"] for s in bildirimler(conn)} == {-1009}


# ---------------------------------------------------------------- bekleyen eski grup mesajları kanala SIZMAZ
def _satir_ekle(conn, saat, hedef, tur="oyuncu_degisti", grup=G1, anahtar="a"):
    with db.islem(conn):
        outbox.kuyruga_ekle(conn, hedef_sohbet=hedef, grup_id=grup, tur=tur, idem_anahtari=anahtar,
                            veri={"oyuncu_id": "1", "ad": "Ali", "degisimler": [], "tespit": iso(saat())}, simdi=saat())


def test_eski_grup_hedefli_bekleyenler_iptal_edilir_silinmez_ve_kanala_gitmez(conn, saat):
    _satir_ekle(conn, saat, G1, anahtar="eski1")
    _satir_ekle(conn, saat, G2, anahtar="eski2", grup=G2)
    _satir_ekle(conn, saat, KANAL, anahtar="yeni")
    with db.islem(conn):
        outbox.kuyruga_ekle(conn, hedef_sohbet=KURUCU, grup_id=None, tur="token_gecersiz", idem_anahtari="t",
                            veri={"zaman": iso(saat())}, simdi=saat())
    toplam = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert outbox.izinsiz_hedefleri_iptal_et(conn, {KURUCU, KANAL}) == 2
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == toplam, "satırlar SİLİNMEZ"
    durumlar = {r["idem_anahtari"]: (r["durum"], r["son_hata"]) for r in conn.execute("SELECT * FROM outbox")}
    assert durumlar["eski1"][0] == "iptal" and durumlar["eski2"][0] == "iptal" and "eski grup" in durumlar["eski1"][1]
    assert durumlar["yeni"][0] == "bekliyor" and durumlar["t"][0] == "bekliyor"
    g = SahteGonderici()
    ozet = run(outbox.bekleyenleri_gonder(conn, g, saat, izinli_hedefler={KURUCU, KANAL}))
    assert sorted(h for h, _, _ in g.gonderilen) == sorted([KANAL, KURUCU]) and ozet.gonderilen == 2
    assert all(h not in (G1, G2) for h, _, _ in g.gonderilen)


def test_gonderim_aninda_savunma_izinsiz_hedefi_gondermez_iptal_eder(conn, saat):
    _satir_ekle(conn, saat, G1, anahtar="sizinti")  # temizlik atlanmış olsa bile
    g = SahteGonderici()
    ozet = run(outbox.bekleyenleri_gonder(conn, g, saat, izinli_hedefler={KURUCU, KANAL}))
    assert g.gonderilen == [] and ozet.iptal == 1
    r = conn.execute("SELECT durum FROM outbox WHERE idem_anahtari='sizinti'").fetchone()
    assert r["durum"] == "iptal"


def test_izinli_hedef_listesi_bos_olamaz(conn):
    with pytest.raises(ValueError):
        outbox.izinsiz_hedefleri_iptal_et(conn, set())


def test_telegram_gondericisi_izinsiz_hedefe_kalici_hata_verir_ve_bot_cagrilmaz():
    bot = SahteBot()
    g = TelegramGonderici(bot, izinli_hedefler={KURUCU, KANAL})
    with pytest.raises(GonderimHatasi) as e:
        run(g.gonder(G1, "token_gecersiz", {"zaman": "2026-10-21T21:30:00.000000Z"}))
    assert e.value.kalici
    assert bot.say("send_message") == 0
    run(g.gonder(KANAL, "token_gecersiz", {"zaman": "2026-10-21T21:30:00.000000Z"}))
    assert bot.son("send_message")["chat_id"] == KANAL


# ---------------------------------------------------------------- kanal doğrulama (Telegram API, mesaj göndermeden)
class KanalBot:
    def __init__(self, *, tur="channel", uye="administrator", can_post=True, hata_chat=None, hata_uye=None):
        self.tur, self.uye, self.can_post = tur, uye, can_post
        self.hata_chat, self.hata_uye = hata_chat, hata_uye
        self.cagrilar: list[str] = []

    async def get_chat(self, chat_id):
        self.cagrilar.append(f"get_chat:{chat_id}")
        if self.hata_chat:
            raise self.hata_chat
        return SimpleNamespace(id=chat_id, type=self.tur, title="Log")

    async def get_me(self):
        self.cagrilar.append("get_me")
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id, user_id):
        self.cagrilar.append(f"get_chat_member:{chat_id}:{user_id}")
        if self.hata_uye:
            raise self.hata_uye
        return SimpleNamespace(status=self.uye, can_post_messages=self.can_post)

    async def send_message(self, **kw):  # DOĞRULAMA mesaj göndermemeli
        self.cagrilar.append("send_message")


def _dogrula(bot, saat=None):
    d = KanalDogrulayici(bot, KANAL, saat or SahteSaat())
    return d, run(d.dogrula())


def test_kanal_hazir_yonetici_ve_gonderme_yetkisi_var():
    bot = KanalBot()
    d, s = _dogrula(bot)
    assert s.hazir and s.neden == "ok" and s.baslik == "Log" and d.hazir_mi()
    assert bot.cagrilar == [f"get_chat:{KANAL}", "get_me", f"get_chat_member:{KANAL}:999"], "yalnız okuma çağrıları"
    assert "send_message" not in bot.cagrilar


def test_kanal_sahibi_hazir_sayilir():
    assert _dogrula(KanalBot(uye="creator", can_post=None))[1].hazir


@pytest.mark.parametrize(
    "bot,parca",
    [
        (KanalBot(hata_chat=BadRequest("Chat not found")), "not found"),
        (KanalBot(hata_chat=Forbidden("bot was kicked")), "no access"),
        (KanalBot(tur="supergroup"), "not a channel"),
        (KanalBot(uye="member"), "not an administrator"),
        (KanalBot(uye="left"), "not an administrator"),
        (KanalBot(can_post=False), "Post messages"),
        (KanalBot(can_post=None), "Post messages"),
        (KanalBot(hata_uye=BadRequest("user not found")), "membership"),
    ],
)
def test_kanal_hazir_degil_nedenler(bot, parca):
    d, s = _dogrula(bot)
    assert not s.hazir and parca in s.neden and not d.hazir_mi()
    assert "send_message" not in bot.cagrilar
    assert BOT_TOKEN not in s.neden


@pytest.mark.parametrize("hata", [TimedOut("t"), NetworkError("n")])
def test_ag_hatasi_gecici_ve_hazir_degil(hata):
    s = _dogrula(KanalBot(hata_chat=hata))[1]
    assert not s.hazir and s.gecici


def test_beklenmeyen_hata_hazir_degil_sayilir_ve_firlatmaz():
    class Bozuk(KanalBot):
        async def get_chat(self, chat_id):
            raise RuntimeError("boom")

    s = _dogrula(Bozuk())[1]
    assert not s.hazir and s.gecici and "RuntimeError" in s.neden


def test_yeniden_dogrulama_araligi_hazirken_6_saat_degilken_5_dakika():
    saat = SahteSaat()
    bot = KanalBot()
    d = KanalDogrulayici(bot, KANAL, saat)
    assert run(d.hazirla()) is True
    n = len(bot.cagrilar)
    saat.ilerle(3600)
    assert run(d.hazirla()) and len(bot.cagrilar) == n, "hazırken 6 saatten önce yeniden sorulmaz"
    saat.ilerle(6 * 3600)
    assert run(d.hazirla()) and len(bot.cagrilar) > n
    bot2 = KanalBot(uye="member")
    d2 = KanalDogrulayici(bot2, KANAL, saat)
    assert run(d2.hazirla()) is False
    n2 = len(bot2.cagrilar)
    saat.ilerle(60)
    assert run(d2.hazirla()) is False and len(bot2.cagrilar) == n2, "5 dakikadan önce tekrar denenmez"
    bot2.uye = "administrator"
    saat.ilerle(300)
    assert run(d2.hazirla()) is True


def test_kanal_hazir_degilken_gonderici_bekletir_hata_saymaz():
    bot = SahteBot()
    d, _ = _dogrula(KanalBot(uye="member"))
    g = TelegramGonderici(bot, izinli_hedefler={KURUCU, KANAL}, kanal=d)
    with pytest.raises(GonderimHatasi) as e:
        run(g.gonder(KANAL, "token_gecersiz", {"zaman": "2026-10-21T21:30:00.000000Z"}))
    assert e.value.bekle_saniye == 300.0 and not e.value.kalici
    assert bot.say("send_message") == 0
    run(g.gonder(KURUCU, "token_gecersiz", {"zaman": "2026-10-21T21:30:00.000000Z"}))  # kurucuya özel mesaj etkilenmez
    assert bot.say("send_message") == 1


def test_kanal_hazir_degilken_outbox_satiri_hata_veya_deneme_sayilmaz(conn, saat):
    _satir_ekle(conn, saat, KANAL, anahtar="k")
    d, _ = _dogrula(KanalBot(can_post=False))
    g = TelegramGonderici(SahteBot(), izinli_hedefler={KURUCU, KANAL}, kanal=d)
    for _ in range(3):
        run(outbox.bekleyenleri_gonder(conn, g, saat))
        saat.ilerle(400)
    r = conn.execute("SELECT durum, deneme FROM outbox WHERE idem_anahtari='k'").fetchone()
    assert (r["durum"], r["deneme"]) == ("bekliyor", 0)


# ---------------------------------------------------------------- zamanlayıcı: seçili liste + kanal hazır değilse duraklama
def _onayli_veri(conn, saat, src, gruplar):
    for g in gruplar:
        if g not in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar:
            yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, g)


def _tick(z):
    return run(z.tick_ve_bekle())


def test_secili_olmayan_liste_taranmaz_bildirim_uretmez_veri_korunur(conn, saat, src):
    src.oyuncu("1", "Ali", "Varna")
    sv = TaramaZamanlayici(conn, src, KURUCU, saat).takip
    run(sv.oyuncu_ekle(G1, "1", src))
    run(sv.oyuncu_ekle(G2, "1", src))
    once = (takipte(conn, G1), takipte(conn, G2))
    z_bos = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset())
    assert _tick(z_bos) == []
    assert conn.execute("SELECT COUNT(*) FROM tarama_isleri").fetchone()[0] == 0
    z_g1 = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset({G1}))
    sonuc = _tick(z_g1)
    assert len(sonuc) == 1
    taranan = {r["group_id"] for r in conn.execute("SELECT DISTINCT group_id FROM tarama_isleri")}
    assert taranan == {G1}, "yalnız seçili liste taranır"
    assert (takipte(conn, G1), takipte(conn, G2)) == once, "hiçbir takip kaydı silinmedi/birleştirilmedi"
    z_hepsi = TaramaZamanlayici(conn, src, KURUCU, saat)  # None = kısıtsız (yalnız testler)
    saat.ilerle(3600)
    assert len(_tick(z_hepsi)) == 2


def test_kanal_hazir_degilken_kontrol_false_kurucuya_tek_bildirim_sonra_bir_toparlanma_bildirimi(conn, saat, src):
    bot = KanalBot(uye="member")
    d = KanalDogrulayici(bot, KANAL, saat)
    z = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset({G1}), kanal=d)
    for _ in range(3):
        assert run(z._kanal_kontrol()) is False
        saat.ilerle(301)
    b = bildirimler(conn, "kanal_durumu")
    assert len(b) == 1 and b[0]["hedef_sohbet"] == KURUCU and b[0]["veri"]["hazir"] is False
    assert b[0]["veri"]["kanal_id"] == KANAL and "administrator" in b[0]["veri"]["neden"]
    bot.uye = "administrator"
    saat.ilerle(301)
    assert run(z._kanal_kontrol()) is True
    assert run(z._kanal_kontrol()) is True
    b = bildirimler(conn, "kanal_durumu")
    assert [x["veri"]["hazir"] for x in b] == [False, True]


def test_kanal_hazir_ve_ilk_kontrolde_bildirim_yok(conn, saat, src):
    z = TaramaZamanlayici(conn, src, KURUCU, saat, kanal=KanalDogrulayici(KanalBot(), KANAL, saat))
    assert run(z._kanal_kontrol()) is True
    assert bildirimler(conn, "kanal_durumu") == []


def test_calis_dongusu_kanal_hazir_degilken_tarama_baslatmaz_ama_kurucu_bildirimleri_gider(conn, saat, src, monkeypatch):
    src.oyuncu("1", "Ali", "Varna")
    sv = TaramaZamanlayici(conn, src, KURUCU, saat).takip
    run(sv.oyuncu_ekle(G1, "1", src))
    d = KanalDogrulayici(KanalBot(uye="member"), KANAL, saat)
    z = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset({G1}), kanal=d)
    g = SahteGonderici()

    async def dongu():
        durdur = asyncio.Event()

        async def durdurucu():
            await asyncio.sleep(0.2)
            durdur.set()

        monkeypatch.setattr(config, "TICK_SECONDS", 0.05)
        await asyncio.gather(z.calis(g, durdur), durdurucu())

    asyncio.run(dongu())
    assert conn.execute("SELECT COUNT(*) FROM tarama_isleri").fetchone()[0] == 0, "kanal hazır değil: tarama yok"
    assert [t for h, t, _ in g.gonderilen] == ["kanal_durumu"] and g.gonderilen[0][0] == KURUCU


def test_calis_dongusu_kanal_hazirsa_secili_liste_taranir_ve_bildirimler_yalniz_kanala_gider(conn, saat, src, monkeypatch):
    src.oyuncu("1", "Ali", "Varna")
    sv = TaramaZamanlayici(conn, src, KURUCU, saat).takip
    run(sv.oyuncu_ekle(G1, "1", src))
    run(sv.oyuncu_ekle(G2, "1", src))
    d = KanalDogrulayici(KanalBot(), KANAL, saat)
    z = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset({G1}), kanal=d)
    run(z.tick_ve_bekle())  # baseline
    src.oyuncu("1", "Ali", "Sofia")
    saat.ilerle(700)
    g = SahteGonderici()

    async def dongu():
        durdur = asyncio.Event()

        async def durdurucu():
            await asyncio.sleep(0.3)
            durdur.set()

        monkeypatch.setattr(config, "TICK_SECONDS", 0.05)
        await asyncio.gather(z.calis(g, durdur), durdurucu())

    asyncio.run(dongu())
    assert {h for h, _, _ in g.gonderilen} == {KANAL}
    assert [t for _, t, _ in g.gonderilen] == ["oyuncu_degisti"], "yalnız seçili liste (G1) için tek bildirim"
    assert {r["group_id"] for r in conn.execute("SELECT DISTINCT group_id FROM tarama_isleri")} == {G1}


# ---------------------------------------------------------------- gruplar komut işlemez / bildirim almaz
@pytest.mark.parametrize("sohbet_turu", ["group", "supergroup"])
@pytest.mark.parametrize("kisi", [KURUCU, YETKILI, YABANCI])
@pytest.mark.parametrize("grup", [G1, G3])
def test_gruplar_hicbir_komut_veya_dugmeyi_islemez_ve_iz_birakmaz(conn, src, saat, sohbet_turu, kisi, grup):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    ui = Arayuz(conn, src, KURUCU, saat=saat)
    d = yetki.karar_ver(yonetim.durum_yukle(conn, KURUCU), sohbet_id=grup, sohbet_turu=sohbet_turu, kullanici_id=kisi)
    assert not d and d.neden == "grup_komutlari_kapali"
    src.oyuncu("1", "Ali")
    for ad, arg in (("addplayer", "1"), ("oyuncuekle", "1"), ("status", ""), ("help", ""), ("grant", "5"), ("approve_group", "-1009"),
                    ("remove_group", str(G1)), ("start", "")):
        o = komut(ad, arg, sohbet=grup, tur=sohbet_turu, kul=kisi)
        assert run(ui.komut(o)) is None, ad
    y = run(ui.buton(gercek_grup_dugme("ol:0", grup=grup, kul=kisi)))
    assert y.sessiz and not y.metin and y.uyari is None
    assert takipte(conn, G1) == set() and takipte(conn, grup) == set()
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert yonetim.durum_yukle(conn, KURUCU).onayli_gruplar == frozenset({G1, G2})


def test_ozel_sohbet_yonetimi_yalniz_kurucuda_ve_calisir(conn, src, saat):
    ui = Arayuz(conn, src, KURUCU, saat=saat)
    src.oyuncu("1", "Ali")
    y = run(ui.komut(grup_komut("addplayer", "1", grup=G1)))
    assert y is not None and "tracking" in y.metin and takipte(conn, G1) == {"1"}
    assert run(ui.komut(komut("status", kul=YABANCI))).metin == "In private chat, this bot is available to the owner only."
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 1


def test_gruba_eklenme_olayi_kanal_icin_sahte_alarm_uretmez(conn, src, saat):
    ui = Arayuz(conn, src, KURUCU, saat=saat)
    assert ui.bot_uyelik_degisti(KANAL, "channel", "left", "administrator", KURUCU) == "diger"
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0


# ---------------------------------------------------------------- durum / kanal / listeler komutları (kurucu, özel sohbet)
def test_status_kanal_ve_liste_durumunu_gosterir(conn, src, saat):
    d = KanalDogrulayici(KanalBot(), KANAL, saat)
    z = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset({G1}), kanal=d)
    ui = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=z, kanal=d)
    t = run(ui.komut(grup_komut("status", grup=G1))).metin
    assert f"Notification channel: <code>{KANAL}</code> (not verified yet)" in t
    assert "Tracking list status: ACTIVE (scanned)" in t
    run(d.dogrula())
    assert f"Notification channel: <code>{KANAL}</code> — ready" in run(ui.komut(grup_komut("status", grup=G1))).metin
    t2 = run(ui.komut(grup_komut("status", grup=G2))).metin
    assert "Tracking list status: not active (not scanned)" in t2
    d.durum = type(d.durum)(False, "the bot is not an administrator of the channel (status: member)")
    assert "NOT READY: the bot is not an administrator" in run(ui.komut(grup_komut("status", grup=G1))).metin


def test_status_hicbir_liste_aktif_degilse_uyarir(conn, src, saat):
    z = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset())
    ui = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=z)
    assert "No tracking list is active" in run(ui.komut(grup_komut("status", grup=G1))).metin
    assert "No tracking list is active" in run(ui.komut(komut("status"))).metin


def test_kanal_komutu_yeniden_dogrular_ve_mesaj_gondermez(conn, src, saat):
    bot = KanalBot(can_post=False)
    d = KanalDogrulayici(bot, KANAL, saat)
    ui = Arayuz(conn, src, KURUCU, saat=saat, kanal=d)
    t = run(ui.komut(komut("channel"))).metin
    assert "Not ready" in t and "Post messages" in t and "send_message" not in bot.cagrilar
    bot.can_post = True
    assert "✅" in run(ui.komut(komut("kanal"))).metin  # eski/Türkçe ad da çalışır
    assert run(ui.komut(komut("channel", kul=YABANCI))).metin == "In private chat, this bot is available to the owner only."


def test_listeler_komutu_aktif_listeyi_isaretler(conn, src, saat):
    src.oyuncu("1", "Ali")
    z = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset({G2}))
    run(z.takip.oyuncu_ekle(G1, "1", src))
    ui = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=z)
    t = run(ui.komut(komut("lists"))).metin
    assert f"<code>{G1}</code> — not active" in t and f"<code>{G2}</code> — ✅ ACTIVE (scanned)" in t
    assert "players 1" in t
    ui_bos = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset()))
    assert "No list is active" in run(ui_bos.komut(komut("lists"))).metin


def test_grup_secimi_aktif_listeyi_isaretler(conn, src, saat):
    z = TaramaZamanlayici(conn, src, KURUCU, saat, aktif_listeler=frozenset({G2}))
    ui = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=z)
    y = run(ui.komut(komut("start")))
    y = run(ui.buton(__import__("tg_fakes").dugme("gl")))
    etiketler = [d.etiket for satir in y.klavye for d in satir]
    assert f"Group {G2} ✅ active" in etiketler and f"Group {G1}" in etiketler


# ---------------------------------------------------------------- rapor (salt okunur)
def _rapor_verisi(conn, saat, src):
    src.oyuncu("1", "Ali")
    src.oyuncu("2", "Bob")
    src.oyuncu("3", "Cem")
    sv = TaramaZamanlayici(conn, src, KURUCU, saat).takip
    for g, ids in ((G1, ("1", "2")), (G2, ("2", "3"))):
        for i in ids:
            run(sv.oyuncu_ekle(g, i, src))
    src.parti("5", "P", [("1", "Ali")])
    run(sv.parti_ekle(G1, "5", src))
    run(sv.parti_ekle(G2, "5", src))
    _satir_ekle(conn, saat, G1, anahtar="eski")
    _satir_ekle(conn, saat, KANAL, anahtar="yeni")


def test_rapor_sayilar_cakisma_ve_bekleyen_hedefler(conn, saat, src):
    _rapor_verisi(conn, saat, src)
    ozet = {o["liste_id"]: o for o in rapor.liste_ozeti(conn, frozenset({G2}))}
    # G1: oyuncular {1,2}; G2: {2,3} + parti üyesi 1 = {1,2,3}
    assert ozet[G1]["oyuncu"] == 2 and ozet[G1]["parti"] == 1 and ozet[G1]["aktif"] is False and ozet[G1]["onayli"]
    assert ozet[G2]["oyuncu"] == 3 and ozet[G2]["aktif"] is True and ozet[G1]["oyuncu_elle"] == 2
    c = rapor.cakisma(conn)
    assert len(c) == 1 and (c[0]["a"], c[0]["b"]) == (G2, G1)  # kimliğe göre sıralı: -1002 < -1001
    ortak = c[0]["oyuncu"]
    assert (ortak["ortak"], ortak["yalniz_a"], ortak["yalniz_b"]) == (2, 1, 0)
    assert c[0]["parti"]["ortak"] == 1
    assert dict(rapor.bekleyen_hedefler(conn)) == {G1: 1, KANAL: 1}
    t = rapor.metin(conn, frozenset({G2}), kanal_id=KANAL)
    assert "nothing was changed" in t and "ACTIVE (scanned)" in t and "not active" in t
    assert f"{G1}: 1 pending -> OLD GROUP/OTHER" in t and f"{KANAL}: 1 pending -> log channel" in t
    assert "TRACKING_GROUP_IDS" in t and "NOT merged automatically" in t
    assert "Ali" not in t and "Bob" not in t, "varsayılan raporda oyuncu adı yok"


def test_rapor_hicbir_sey_degistirmez_cli_salt_okunur(tmp_path, conn, saat, src, capsys):
    yol = tmp_path / "dipbot.db"
    c = db.baglan(yol)
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G1)
    src.oyuncu("1", "Ali")
    run(TaramaZamanlayici(c, src, KURUCU, saat).takip.oyuncu_ekle(G1, "1", src))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    once = hashlib.sha256(yol.read_bytes()).hexdigest()
    assert rapor.main([str(yol), "--aktif", str(G1)]) == 0
    out = capsys.readouterr().out
    assert f"List {G1}: ACTIVE (scanned)" in out and "players: 1" in out
    assert hashlib.sha256(yol.read_bytes()).hexdigest() == once, "rapor DB'yi değiştirmedi"
    assert rapor.main([str(tmp_path / "yok.db")]) == 2
    assert rapor.main([str(yol), "--aktif", "abc"]) == 2


# ---------------------------------------------------------------- calistir: başlangıçta eski grup satırları iptal, liste seçilmeden tarama yok
def test_calistir_baslangicta_eski_grup_bekleyenlerini_iptal_eder_kanal_dogrular_ve_listesiz_taramaz(monkeypatch, tmp_path):
    from test_telegram_uygulama import Ctx, _SahteUygulama, _calistir_hazirla
    from telegram.error import Conflict

    class KanalliUygulama(_SahteUygulama):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.kanal_bot = KanalBot()
            self.mesajlar = []

        async def get_chat(self, chat_id):
            return await self.kanal_bot.get_chat(chat_id)

        async def get_me(self):
            return await self.kanal_bot.get_me()

        async def get_chat_member(self, chat_id, user_id):
            return await self.kanal_bot.get_chat_member(chat_id, user_id)

        async def send_message(self, **kw):
            self.mesajlar.append(kw)
            return SimpleNamespace(message_id=1)

    async def polling(app):
        async def sonra():
            await asyncio.sleep(0.4)  # zamanlayıcı döngüsü en az bir tur çalışsın
            ctx = Ctx()
            ctx.error = Conflict("terminated by other getUpdates request")
            await app.hata_isleyici(None, ctx)

        app.gorev = asyncio.get_running_loop().create_task(sonra())  # polling hemen döner; durdurma gecikmeli

    app = KanalliUygulama(polling_sirasinda=polling)
    c, ta, baglantilar = _calistir_hazirla(monkeypatch, tmp_path, app)
    saat = SahteSaat()
    yol = ta.ayarlar.db_path
    ilk = db.baglan(yol)
    yonetim.grup_onayla(ilk, saat, 1000, 1000, G1)
    ilk.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi) VALUES(?,?,?,?)", (G1, "1", "Ali", iso(saat())))
    ilk.execute("INSERT INTO takip_partiler(group_id, parti_id, ad, eklendi) VALUES(?,?,?,?)", (G1, "5", "P", iso(saat())))
    ilk.execute("INSERT INTO savaslar(group_id, savas_id, ad, ilk_gorulme) VALUES(?,?,?,?)", (G1, "w1", "W", iso(saat())))
    _satir_ekle(ilk, saat, G1, anahtar="eski-grup")
    _satir_ekle(ilk, saat, KANAL, anahtar="kanal-satiri")
    ilk.close()
    monkeypatch.setattr(c.config if hasattr(c, "config") else config, "TICK_SECONDS", 0.05)
    kod = asyncio.run(asyncio.wait_for(c.calistir(ta), 20))
    assert kod == 3
    conn2 = sqlite3.connect(yol)
    conn2.row_factory = sqlite3.Row
    durum = {r["idem_anahtari"]: r["durum"] for r in conn2.execute("SELECT idem_anahtari, durum FROM outbox")}
    assert durum["eski-grup"] == "iptal", "eski grup bildirimi iptal; kanala da gitmedi"
    assert all(m["chat_id"] != G1 for m in app.mesajlar), "eski gruba hiçbir mesaj gitmedi"
    assert [m["chat_id"] for m in app.mesajlar] == [KANAL] and durum["kanal-satiri"] == "gonderildi"
    # veri silinmedi, hiçbir liste seçilmediği için taranmadı
    assert conn2.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 1
    assert conn2.execute("SELECT COUNT(*) FROM takip_partiler").fetchone()[0] == 1
    assert conn2.execute("SELECT COUNT(*) FROM savaslar").fetchone()[0] == 1
    assert conn2.execute("SELECT COUNT(*) FROM tarama_isleri").fetchone()[0] == 0, "TRACKING_GROUP_IDS boş: tarama yok"
    conn2.close()
    assert ("set_my_commands", len(__import__("dipbot.tg.arayuz", fromlist=["KOMUTLAR"]).KOMUTLAR)) in app.olaylar


# ---------------------------------------------------------------- deploy/onkontrol.sh: kanal ve liste seçimi
def _onkontrol(tmp_path, env_icerik):
    import os
    import shutil
    import subprocess

    if not shutil.which("bash"):
        pytest.skip("bash yok")
    envf = tmp_path / "dipbot.env"
    envf.write_text(env_icerik)
    envf.chmod(0o600)
    kok = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, ENV_FILE=str(envf), APP_DIR=str(tmp_path / "yok"), DATA_DIR=str(tmp_path / "yokdata"),
               UNIT_FILE=str(tmp_path / "yok.service"), APP_USER="nobody")
    return subprocess.run(["bash", os.path.join(kok, "deploy", "onkontrol.sh")], env=env, capture_output=True, text=True, timeout=60).stdout


def test_onkontrol_liste_secilmediyse_uyarir_tarama_yok(tmp_path):
    out = _onkontrol(tmp_path, "BOT_TOKEN=1:x\nOWNER_TELEGRAM_ID=1000\nDB_PATH=/x/dipbot.db\n")
    assert "UYARI : TRACKING_GROUP_IDS bos/yok: HICBIR takip listesi taranmaz" in out
    assert "TAMAM : LOG_CHANNEL_ID tanimli degil: varsayilan bildirim kanali" in out


def test_onkontrol_gecerli_kanal_ve_liste_tamam(tmp_path):
    out = _onkontrol(tmp_path, "LOG_CHANNEL_ID=-1003998002227\nTRACKING_GROUP_IDS=-1004375821471\nBOT_TOKEN=1:x\n")
    assert "TAMAM : LOG_CHANNEL_ID tanimli (negatif sayi)" in out and "TAMAM : TRACKING_GROUP_IDS tanimli" in out
    assert "TRACKING_GROUP_IDS bos/yok" not in out


@pytest.mark.parametrize("satir,beklenen", [("LOG_CHANNEL_ID=12345", "HATA  : LOG_CHANNEL_ID negatif"),
                                           ("TRACKING_GROUP_IDS=abc", "HATA  : TRACKING_GROUP_IDS virgulle")])
def test_onkontrol_gecersiz_kanal_veya_liste_hata_verir(tmp_path, satir, beklenen):
    assert beklenen in _onkontrol(tmp_path, f"BOT_TOKEN=1:x\n{satir}\n")
