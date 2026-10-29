"""Merkezi takip mimarisi: eski gruplar TAMAMEN devre dışı; tek merkezi takip alanı; bildirim yalnız log kanalı.

Senaryolar: eski grupta /start yanıtsız; eski grup butonları işlenmez; eski grup yöneticileri/bot yöneticiliği yetki vermez;
grup kimliği olmadan tarama; özel sohbetten oyuncu/parti ekleme; bildirimler yalnız kanala; kanal kaybolunca bildirim kaybolmaz;
eski kuyruk satırları tekrar gönderilmez; kanal değişince takip geçmişi korunur. MOCK testler (ağ yok)."""
import asyncio
from types import SimpleNamespace

import pytest

from conftest import bildirimler, run
from fakes import SahteGonderici, SahteKaynak, SahteSaat
from tg_fakes import KURUCU, YABANCI, YETKILI, dugme, gercek_grup_dugme, komut, veriler

from dipbot import config, db, outbox
from dipbot.outbox import GonderimHatasi
from dipbot.tg.arayuz import Arayuz
from dipbot.tg.gonderici import TelegramGonderici
from dipbot.tg.kanal import KanalDogrulayici
from dipbot.zamanlayici import TaramaZamanlayici
from dipbot.zaman import iso
from test_kanal_bildirim import KanalBot, SahteBot

KANAL = config.LOG_KANAL_ID
M = config.MERKEZ_ID
ESKI = (-1004485206993, -1004375821471, -5571949121)  # eski gruplar (yalnız arşiv)


@pytest.fixture
def merkez(saat):
    """Merkezi mimari: HİÇ onaylı grup kaydı yok (grup kimliği gerekmez)."""
    c = db.baglan(":memory:")
    yield c
    c.close()


def _tracked(c, alan=M):
    return {r[0] for r in c.execute("SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=?", (alan,))}


# ---------------------------------------------------------------- 1-3: eski gruplar tamamen devre dışı
@pytest.mark.parametrize("grup", ESKI)
@pytest.mark.parametrize("tur", ["group", "supergroup"])
@pytest.mark.parametrize("kisi", [KURUCU, YETKILI, YABANCI])
def test_eski_grupta_komutlar_yanitsiz_ve_iz_birakmaz(merkez, saat, grup, tur, kisi):
    ui = Arayuz(merkez, SahteKaynak(), KURUCU, saat=saat)
    for ad, arg in (("start", ""), ("help", ""), ("status", ""), ("players", ""), ("addplayer", "1"), ("addparty", "5"),
                    ("scan", "5"), ("lists", ""), ("channel", ""), ("oyuncuekle", "1"), ("durum", "")):
        assert run(ui.komut(komut(ad, arg, sohbet=grup, tur=tur, kul=kisi))) is None, ad
    assert _tracked(merkez) == set() and _tracked(merkez, grup) == set()
    assert merkez.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0
    assert merkez.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert merkez.execute("SELECT COUNT(*) FROM onayli_gruplar").fetchone()[0] == 0, "grup onayı oluşmaz"


@pytest.mark.parametrize("grup", ESKI)
@pytest.mark.parametrize("veri", ["m", "ol:0", "pl:0", "sl:0", "du", "yo", "tk", "gs:-1001", "gk:-1001", "yk:55", "gl", "c:e:abc", "ta"])
def test_eski_grup_butonlari_islenmez(merkez, saat, grup, veri):
    ui = Arayuz(merkez, SahteKaynak(), KURUCU, saat=saat)
    y = run(ui.buton(gercek_grup_dugme(veri, grup=grup, kul=KURUCU)))
    assert y.sessiz and not y.metin and y.uyari is None and not y.duzenle
    assert merkez.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0


@pytest.mark.parametrize("grup", ESKI)
def test_bot_yoneticiligi_ve_gruba_yeniden_ekleme_yetki_veya_takip_alani_olusturmaz(merkez, saat, grup):
    ui = Arayuz(merkez, SahteKaynak(), KURUCU, saat=saat)
    for eski, yeni in ((None, "member"), ("left", "administrator"), ("kicked", "administrator")):
        assert ui.bot_uyelik_degisti(grup, "supergroup", eski, yeni, YABANCI) == "onaysiz_ekleme"
    for t in ("onayli_gruplar", "grup_ayarlari", "takip_oyuncular", "takip_partiler", "savaslar", "tarama_isleri", "yetkili_kullanicilar"):
        assert merkez.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    # yönetici olan bot (ve yöneticisi) hâlâ hiçbir komut çalıştıramaz
    assert run(ui.komut(komut("addplayer", "1", sohbet=grup, tur="supergroup", kul=YABANCI))) is None
    assert run(ui.komut(komut("addplayer", "1", sohbet=grup, tur="supergroup", kul=KURUCU))) is None
    assert _tracked(merkez) == set()


def test_gruba_ekleme_bildirimi_yalniz_kurucuya_ozel_sohbette(merkez, saat):
    ui = Arayuz(merkez, SahteKaynak(), KURUCU, saat=saat)
    ui.bot_uyelik_degisti(ESKI[0], "supergroup", "left", "administrator", YABANCI)
    b = bildirimler(merkez)
    assert [(x["hedef_sohbet"], x["tur"]) for x in b] == [(KURUCU, "yetkisiz_deneme")]


def test_gruplarda_yetkilendirme_ve_onay_mekanizmasi_calisma_zamaninda_okunmaz(merkez, saat):
    """Arayuz._durum() veritabanından onaylı grup/yetkili listesi okumaz."""
    merkez.execute("INSERT INTO onayli_gruplar(group_id, onaylandi, donem) VALUES(?,?,1)", (ESKI[0], iso(saat())))
    merkez.execute("INSERT INTO yetkili_kullanicilar(user_id, eklendi) VALUES(?,?)", (YETKILI, iso(saat())))
    ui = Arayuz(merkez, SahteKaynak(), KURUCU, saat=saat)
    d = ui._durum()
    assert d.onayli_gruplar == frozenset() and d.yetkililer == frozenset() and d.kurucu_id == KURUCU
    assert run(ui.komut(komut("status", kul=YETKILI))).metin == "In private chat, this bot is available to the owner only."
    assert run(ui.komut(komut("addplayer", "1", sohbet=ESKI[0], tur="supergroup", kul=YETKILI))) is None


# ---------------------------------------------------------------- 4-5: grup kimliği olmadan tarama + özel sohbetten yönetim
def test_ozel_sohbetten_oyuncu_ve_parti_ekleme_silme_merkezi_alanda(merkez, saat):
    src = SahteKaynak()
    src.oyuncu("72400", "Ali", "Varna", "Bulgaria")
    src.parti("1244", "TEKNO", [("1", "Ann"), ("2", "Bob")])
    ui = Arayuz(merkez, src, KURUCU, saat=saat)
    y = run(ui.komut(komut("addplayer", "72400")))
    assert "tracking" in y.metin and _tracked(merkez) == {"72400"}
    y = run(ui.komut(komut("addparty", "1244")))
    assert "Party added" in y.metin
    assert merkez.execute("SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", (M,)).fetchone()[0] == 1
    assert {"1", "2"} <= _tracked(merkez)
    assert merkez.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE group_id<>?", (M,)).fetchone()[0] == 0
    y = run(ui.komut(komut("removeplayer", "72400")))
    kid = next(v for v in veriler(y) if v.startswith("c:e:")).split(":")[2]
    run(ui.buton(dugme(f"c:e:{kid}")))
    assert "72400" not in _tracked(merkez)
    y = run(ui.komut(komut("scan", "20")))
    assert merkez.execute("SELECT tarama_araligi_dk FROM grup_ayarlari WHERE group_id=?", (M,)).fetchone()[0] == 20
    assert "group" not in run(ui.komut(komut("status"))).metin.lower().replace("notification", "")


def test_grup_kimligi_olmadan_merkezi_tarama_ve_eski_gruplar_taranmaz(merkez, saat):
    src = SahteKaynak()
    src.oyuncu("1", "Ali", "Varna")
    ui = Arayuz(merkez, src, KURUCU, saat=saat)
    run(ui.komut(komut("addplayer", "1")))
    # eski grup (arşiv) kaydı: onaylı ve takipli, ama taranmamalı
    merkez.execute("INSERT INTO onayli_gruplar(group_id, onaylandi, donem) VALUES(?,?,1)", (ESKI[0], iso(saat())))
    from dipbot.takip import TakipServisi
    run(TakipServisi(merkez, saat).oyuncu_ekle(ESKI[0], "1", src))
    z = TaramaZamanlayici(merkez, src, KURUCU, saat, kapsamlar=frozenset({M}))
    assert len(run(z.tick_ve_bekle())) == 1
    assert {r[0] for r in merkez.execute("SELECT DISTINCT group_id FROM tarama_isleri")} == {M}
    src.oyuncu("1", "Ali", "Sofia")
    saat.ilerle(700)
    run(z.tick_ve_bekle())
    b = bildirimler(merkez, "oyuncu_degisti")
    assert len(b) == 1 and b[0]["grup_id"] == M and b[0]["hedef_sohbet"] == KANAL
    saat.ilerle(700)
    run(z.tick_ve_bekle())
    assert len(bildirimler(merkez, "oyuncu_degisti")) == 1, "aynı olay iki kez gönderilmez"


# ---------------------------------------------------------------- 7: kanal kaybolunca bildirim kaybolmaz
def test_kanal_erisimi_kaybolunca_bildirim_kaybolmaz_tarama_duraklar_geri_gelince_tam_teslim(merkez, saat):
    src = SahteKaynak()
    src.oyuncu("1", "Ali", "Varna")
    kb = KanalBot()
    kanal = KanalDogrulayici(kb, KANAL, saat)
    z = TaramaZamanlayici(merkez, src, KURUCU, saat, kapsamlar=frozenset({M}), kanal=kanal)
    ui = Arayuz(merkez, src, KURUCU, saat=saat)
    run(ui.komut(komut("addplayer", "1")))
    run(z.tick_ve_bekle())  # baseline
    src.oyuncu("1", "Ali", "Sofia")
    saat.ilerle(700)
    run(z.tick_ve_bekle())
    assert len(bildirimler(merkez, "oyuncu_degisti")) == 1  # kuyruğa alındı (henüz gönderilmedi)
    # kanal erişimi KAYBOLUR
    kb.uye = "left"
    saat.ilerle(400)
    assert run(z._kanal_kontrol()) is False
    bot = SahteBot()
    g = TelegramGonderici(bot, izinli_hedefler={KURUCU, KANAL}, kanal=kanal)
    for _ in range(3):
        run(outbox.bekleyenleri_gonder(merkez, g, saat, izinli_hedefler={KURUCU, KANAL}, izinli_kapsamlar={M}))
        saat.ilerle(400)
    r = merkez.execute("SELECT durum, deneme FROM outbox WHERE tur='oyuncu_degisti'").fetchone()
    kanala = lambda: [kw["chat_id"] for a, kw in bot.cagrilar if a == "send_message"].count(KANAL)  # noqa: E731
    assert (r["durum"], r["deneme"]) == ("bekliyor", 0) and kanala() == 0, "kanal kapalıyken kanala hiçbir şey gitmedi"
    assert [h["hedef_sohbet"] for h in bildirimler(merkez, "kanal_durumu")] == [KURUCU]  # kurucu bilgilendirildi
    # kanal kapalıyken oyuncu yeniden değişir: tarama DURAKLI olduğundan yeni gözlem YAZILMAZ (değişiklik kaybolmaz)
    src.oyuncu("1", "Ali", "Ruse")
    assert run(z._kanal_kontrol()) is False
    onceki = merkez.execute("SELECT bolge FROM takip_oyuncular WHERE group_id=?", (M,)).fetchone()[0]
    assert onceki == "Sofia"
    # kanal GERİ GELİR
    kb.uye = "administrator"
    saat.ilerle(400)
    assert run(z._kanal_kontrol()) is True
    run(outbox.bekleyenleri_gonder(merkez, g, saat, izinli_hedefler={KURUCU, KANAL}, izinli_kapsamlar={M}))
    assert [kw["chat_id"] for a, kw in bot.cagrilar if a == "send_message"].count(KANAL) == 1
    saat.ilerle(700)
    run(z.tick_ve_bekle())  # Ruse değişikliği şimdi yakalanır
    assert len(bildirimler(merkez, "oyuncu_degisti")) == 2


def test_kanal_hic_erisilemezse_eski_gruplara_donulmez(merkez, saat):
    kb = KanalBot(hata_chat=__import__("telegram.error", fromlist=["BadRequest"]).BadRequest("Chat not found"))
    kanal = KanalDogrulayici(kb, KANAL, saat)
    run(kanal.dogrula())
    bot = SahteBot()
    g = TelegramGonderici(bot, izinli_hedefler={KURUCU, KANAL}, kanal=kanal)
    outbox_ = outbox
    merkez.execute("INSERT INTO outbox(hedef_sohbet, grup_id, tur, idem_anahtari, veri, sonraki_deneme, olusturuldu, onay_donemi)"
                   " VALUES(?,?,?,?,?,?,?,1)", (KANAL, M, "oyuncu_degisti", "k", '{"oyuncu_id":"1","ad":"A","degisimler":[],"tespit":"2026-10-21T21:30:00.000000Z"}', iso(saat()), iso(saat())))
    run(outbox_.bekleyenleri_gonder(merkez, g, saat, izinli_hedefler={KURUCU, KANAL}, izinli_kapsamlar={M}))
    assert bot.say("send_message") == 0
    with pytest.raises(GonderimHatasi):
        run(g.gonder(ESKI[0], "oyuncu_degisti", {}))  # eski gruba dönüş yok


# ---------------------------------------------------------------- 6/11: eski kuyruk satırları asla gönderilmez
def _satir(c, saat, hedef, grup, anahtar, durum="bekliyor"):
    c.execute("INSERT INTO outbox(hedef_sohbet, grup_id, tur, idem_anahtari, veri, durum, sonraki_deneme, olusturuldu, onay_donemi)"
              " VALUES(?,?,?,?,?,?,?,?,1)", (hedef, grup, "oyuncu_degisti", anahtar,
                                              '{"oyuncu_id":"1","ad":"A","degisimler":[],"tespit":"2026-10-21T21:30:00.000000Z"}',
                                              durum, iso(saat()), iso(saat())))


def test_eski_grup_kuyruk_satirlari_kanala_tasinmaz_iptal_edilir_merkezi_satirlar_korunur(merkez, saat):
    for i, g in enumerate(ESKI):
        _satir(merkez, saat, g, g, f"eski-grup-{i}")            # eski hedef + eski kapsam
        _satir(merkez, saat, KANAL, g, f"eski-kanal-{i}")       # kanala yönelik AMA eski grup kapsamlı (önceki ara sürüm)
    _satir(merkez, saat, KANAL, M, "merkez-kanal")              # meşru merkezi bildirim
    _satir(merkez, saat, -1009999, M, "merkez-eski-kanal")      # kanal değişmeden önce kuyruğa alınmış merkezi bildirim
    _satir(merkez, saat, KURUCU, None, "kurucu-uyari")          # operasyonel uyarı
    _satir(merkez, saat, ESKI[0], None, "artik-hedef")          # kapsamsız ama hedefi kurucu/kanal değil
    toplam = merkez.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    h = outbox.bekleyenleri_hizala(merkez, KURUCU, KANAL)
    assert h.eski_kapsam_iptal == 6 and h.kanala_yonlendirilen == 1 and h.hedef_iptal == 1
    assert merkez.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == toplam, "hiçbir satır silinmedi"
    d = {r["idem_anahtari"]: (r["durum"], r["hedef_sohbet"]) for r in merkez.execute("SELECT * FROM outbox")}
    for i in range(3):
        assert d[f"eski-grup-{i}"][0] == "iptal" and d[f"eski-kanal-{i}"][0] == "iptal"
    assert d["merkez-kanal"] == ("bekliyor", KANAL)
    assert d["merkez-eski-kanal"] == ("bekliyor", KANAL), "kanal değişiminde merkezi bildirim yeni kanala alındı (kaybolmadı)"
    assert d["kurucu-uyari"] == ("bekliyor", KURUCU) and d["artik-hedef"][0] == "iptal"
    g = SahteGonderici()
    run(outbox.bekleyenleri_gonder(merkez, g, saat, izinli_hedefler={KURUCU, KANAL}, izinli_kapsamlar={M}))
    assert sorted((h_, ) for h_, _, _ in g.gonderilen) == sorted([(KANAL,), (KANAL,), (KURUCU,)])
    run(outbox.bekleyenleri_gonder(merkez, g, saat, izinli_hedefler={KURUCU, KANAL}, izinli_kapsamlar={M}))
    assert len(g.gonderilen) == 3, "ikinci tur hiçbir şeyi tekrar göndermedi"


def test_hizalama_idempotent_ve_cakisan_merkezi_satir_tekrar_gonderilmez(merkez, saat):
    _satir(merkez, saat, KANAL, M, "ayni")
    _satir(merkez, saat, -1009999, M, "ayni")  # aynı olay eski kanala da kuyruğa alınmıştı
    h = outbox.bekleyenleri_hizala(merkez, KURUCU, KANAL)
    assert h.kanala_yonlendirilen == 0 and h.hedef_iptal == 1, "çakışan (mükerrer) satır iptal edildi"
    assert outbox.bekleyenleri_hizala(merkez, KURUCU, KANAL) == outbox.HizalamaOzeti()
    g = SahteGonderici()
    run(outbox.bekleyenleri_gonder(merkez, g, saat))
    assert len(g.gonderilen) == 1, "aynı olay iki kez gönderilmez"


def test_gonderim_aninda_eski_kapsam_kanal_hedefli_olsa_bile_gonderilmez(merkez, saat):
    _satir(merkez, saat, KANAL, ESKI[1], "sizinti")
    g = SahteGonderici()
    o = run(outbox.bekleyenleri_gonder(merkez, g, saat, izinli_hedefler={KURUCU, KANAL}, izinli_kapsamlar={M}))
    assert g.gonderilen == [] and o.iptal == 1


# ---------------------------------------------------------------- 12: kanal değişince takip geçmişi korunur
def test_kanal_degisince_takip_gecmisi_sifirlanmaz(merkez, saat):
    src = SahteKaynak()
    src.oyuncu("1", "Ali", "Varna")
    ui = Arayuz(merkez, src, KURUCU, saat=saat)
    run(ui.komut(komut("addplayer", "1")))
    z = TaramaZamanlayici(merkez, src, KURUCU, saat, kapsamlar=frozenset({M}))
    run(z.tick_ve_bekle())
    once = [tuple(r) for r in merkez.execute("SELECT * FROM takip_oyuncular")]
    YENI = -1007777777
    h = outbox.bekleyenleri_hizala(merkez, KURUCU, YENI)  # kanal değişti (başlangıç hizalaması)
    assert [tuple(r) for r in merkez.execute("SELECT * FROM takip_oyuncular")] == once
    z2 = TaramaZamanlayici(merkez, src, KURUCU, saat, kapsamlar=frozenset({M}), kanal_id=YENI)
    from dipbot.takip import TakipServisi
    sv = TakipServisi(merkez, saat, kanal_id=YENI)
    src.oyuncu("1", "Ali", "Sofia")
    run(sv.oyuncular_tara(M, src))
    b = bildirimler(merkez, "oyuncu_degisti")
    assert len(b) == 1 and b[0]["hedef_sohbet"] == YENI, "yeni kanala bildirim; geçmiş (bölge) korunduğu için tek gerçek değişiklik"


# ---------------------------------------------------------------- calistir: başlangıç hizalaması, merkezi tarama, TRACKING_GROUP_IDS yok
def test_calistir_merkezi_alani_tarar_eski_kuyrugu_iptal_eder_kanali_dogrular(monkeypatch, tmp_path):
    from telegram.error import Conflict

    from test_telegram_uygulama import Ctx, _SahteUygulama, _calistir_hazirla

    class Uyg(_SahteUygulama):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.kb = KanalBot()
            self.mesajlar = []

        async def get_chat(self, chat_id):
            return await self.kb.get_chat(chat_id)

        async def get_me(self):
            return await self.kb.get_me()

        async def get_chat_member(self, chat_id, user_id):
            return await self.kb.get_chat_member(chat_id, user_id)

        async def send_message(self, **kw):
            self.mesajlar.append(kw)
            return SimpleNamespace(message_id=1)

    async def polling(app):
        async def sonra():
            await asyncio.sleep(0.5)
            ctx = Ctx()
            ctx.error = Conflict("terminated by other getUpdates request")
            await app.hata_isleyici(None, ctx)

        app.gorev = asyncio.get_running_loop().create_task(sonra())

    app = Uyg(polling_sirasinda=polling)
    c, ta, _ = _calistir_hazirla(monkeypatch, tmp_path, app)
    assert not hasattr(ta, "aktif_takip_listeleri")
    saat = SahteSaat()
    ilk = db.baglan(ta.ayarlar.db_path)
    for g in ESKI:  # eski gruplar: onaylı + takipli (arşiv) + bekleyen bildirim
        ilk.execute("INSERT INTO onayli_gruplar(group_id, onaylandi, donem) VALUES(?,?,1)", (g, iso(saat())))
        ilk.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi) VALUES(?,?,?,?)", (g, "1", "Ali", iso(saat())))
        _satir(ilk, saat, g, g, f"eski-{g}")
    ilk.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, bolge, baseline_tamam, eklendi) VALUES(?,?,?,?,1,?)",
                (M, "5", "Merkez", "Varna", iso(saat())))
    _satir(ilk, saat, -1009999, M, "merkezi-eski-kanal")
    ilk.commit()
    ilk.close()
    monkeypatch.setattr(config, "TICK_SECONDS", 0.05)
    assert asyncio.run(asyncio.wait_for(c.calistir(ta), 20)) == 3
    import sqlite3
    c2 = sqlite3.connect(ta.ayarlar.db_path)
    c2.row_factory = sqlite3.Row
    durum = {r["idem_anahtari"]: r["durum"] for r in c2.execute("SELECT * FROM outbox")}
    for g in ESKI:
        assert durum[f"eski-{g}"] == "iptal"
    assert durum["merkezi-eski-kanal"] == "gonderildi", "merkezi satır kanala alındı ve bir kez gönderildi"
    assert [m["chat_id"] for m in app.mesajlar] == [KANAL] and not [m for m in app.mesajlar if m["chat_id"] in ESKI]
    # merkezi alan tarandı (grup kimliği/onayı gerekmeden); eski gruplar taranmadı
    assert {r[0] for r in c2.execute("SELECT DISTINCT group_id FROM tarama_isleri")} <= {M}
    assert c2.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 4, "hiçbir takip kaydı silinmedi"
    c2.close()
