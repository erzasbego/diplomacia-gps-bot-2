"""English-only Telegram UI and Europe/Istanbul presentation timezone (MOCK tests, no network).

Covers: English command menu + legacy aliases, English responses/buttons/callback alerts for every screen,
English notification texts, UTC -> Europe/Istanbul conversion (24-hour clock, midnight crossing, server-timezone
independence), the simplified /status output, and unchanged authorization / group isolation / UTC storage.
"""
import html
import os
import re
import time

import pytest

from conftest import bildirimler, run, takipte
from fakes import SahteSaat
from tg_fakes import G1, G2, G3, KURUCU, YABANCI, YETKILI, dugme, grup_dugme, grup_komut, komut, veriler

from dipbot import config, yonetim
from dipbot.tg import arayuz as ar
from dipbot.tg import bicim
from dipbot.tg.arayuz import ESKI_KOMUTLAR, EN_KOMUTLAR, KOMUTLAR, TR_KOMUTLAR, Arayuz
from dipbot.tg.uygulama import bot_komutlari
from dipbot.zaman import iso, iso_coz, yerel_goster, yerel_saat

# ---------------------------------------------------------------- Turkish-leftover detector
_TR_HARF = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
_TR_SOZCUK = re.compile(
    r"\b(ve|veya|için|yok|değil|bir|bu|işlem|oyuncu|oyuncular|parti|partiler|savaş|savaşlar|tarama|grup|yetki|"
    r"komut|hata|bulunamadı|eklendi|silindi|takip|takibe|takipten|bildirim|zaman|durum|kimlik|henüz|yalnız|"
    r"önce|sonra|kurucu|onaylı|onayla|vazgeç|menü|yenile|sonraki|önceki|kullanım|geçersiz|süresi|doldu|"
    r"tespit|aralık|dakika|evet|hayır|bilinmiyor|güç|saldıran|savunan|hazırlık|aktif)\b",
    re.IGNORECASE,
)
_ETIKET = re.compile(r"<[^>]+>")


def tr_kalinti(metin) -> list[str]:
    """Turkish letters/words left in a user-visible text (HTML tags and URLs are ignored)."""
    if not metin:
        return []
    duz = html.unescape(_ETIKET.sub(" ", str(metin)))
    duz = re.sub(r"https?://\S+", " ", duz)
    return _TR_HARF.findall(duz) + _TR_SOZCUK.findall(duz)


def ingilizce_mi(metin):
    assert not tr_kalinti(metin), f"Turkish text left: {tr_kalinti(metin)} in {metin!r}"


def yanit_ingilizce(y):
    """Every user-visible part of a Yanit: text, callback alert and button labels."""
    if y is None:
        return
    ingilizce_mi(y.metin)
    ingilizce_mi(y.uyari)
    for satir in y.klavye or []:
        for d in satir:
            ingilizce_mi(d.etiket)


# AM/PM must never be displayed anywhere
_AMPM = re.compile(r"\b(am|pm)\b", re.IGNORECASE)
_TARIH_SAAT = re.compile(r"\b\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}\b")


@pytest.fixture
def ui(conn, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return Arayuz(conn, src, KURUCU, saat=saat)


# ---------------------------------------------------------------- 1. command menu + aliases
def test_telegram_command_menu_is_english():
    adlar = [c.command for c in bot_komutlari()]
    assert adlar == list(KOMUTLAR)
    assert set(adlar) == {
        "start", "help", "addplayer", "players", "removeplayer", "addparty", "parties", "removeparty",
        "wars", "scan", "status", "grant", "revoke", "approve_group", "remove_group", "move_group",
    }
    for c in bot_komutlari():
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.command)
        assert 3 <= len(c.description) <= 256
        ingilizce_mi(c.description)


def test_every_english_command_maps_to_a_legacy_handler():
    for en, ic in EN_KOMUTLAR.items():
        assert en in KOMUTLAR
        assert hasattr(Arayuz, f"_k_{ic}"), (en, ic)
        assert ar.komut_ic_adi(en) == ic
        assert ar.komut_goster(ic) == en


def test_legacy_names_still_resolve():
    for eski, yeni in ESKI_KOMUTLAR.items():
        assert ar.komut_ic_adi(eski) == yeni
    for tr in TR_KOMUTLAR:
        assert ar.komut_ic_adi(tr) == tr
    assert ar.komut_ic_adi("nonexistent") is None


# (command, argument) pairs run as the owner inside approved group G1
_KOMUT_ORNEKLERI = [
    ("start", ""), ("help", ""), ("players", ""), ("parties", ""), ("wars", ""), ("status", ""),
    ("scan", ""), ("scan", "15"), ("scan", "7"), ("scan", "abc"),
    ("addplayer", ""), ("addplayer", "abc"), ("addplayer", "72400"), ("addplayer", "72400"),
    ("addplayer", "https://diplomacia.com.tr/party/1244"),
    ("removeplayer", ""), ("removeplayer", "99999"), ("removeplayer", "72400"),
    ("addparty", ""), ("addparty", "zzz"), ("addparty", "1244"), ("addparty", "9999"),
    ("removeparty", ""), ("removeparty", "1244"), ("removeparty", "5555"),
    ("grant", ""), ("grant", "abc"), ("grant", "4242"), ("revoke", "4242"), ("revoke", "999"),
    ("approve_group", ""), ("approve_group", "abc"), ("approve_group", "-1005"),
    ("remove_group", ""), ("remove_group", "-1005"), ("remove_group", "-1002"),
    ("move_group", ""), ("move_group", "-1001 -1009"),
]
# the same commands under their legacy Turkish names
_ESKI_ESLESME = {v: k for k, v in EN_KOMUTLAR.items()}


def _kaynak_hazirla(src):
    src.oyuncu("72400", "Ali", "Varna", "Bulgaria")
    src.parti("1244", "TEKNO", [("1", "Ann"), ("2", "Bob"), ("3", "Cem")])


@pytest.mark.parametrize("ad,arg", _KOMUT_ORNEKLERI)
@pytest.mark.parametrize("eski", [False, True])
def test_every_command_answers_in_english(ui, src, ad, arg, eski):
    _kaynak_hazirla(src)
    cmd = EN_KOMUTLAR[ad] if eski else ad  # eski: the legacy Turkish alias of the same command
    y = run(ui.komut(grup_komut(cmd, arg)))
    assert y is not None, cmd
    yanit_ingilizce(y)
    assert not _AMPM.search(y.metin or "")


@pytest.mark.parametrize("ad", ["oyuncu_ekle", "oyuncu_sil", "parti_ekle", "parti_sil"])
def test_old_underscore_aliases_answer_in_english(ui, src, ad):
    _kaynak_hazirla(src)
    arg = "72400" if ad.startswith("oyuncu") else "1244"
    y = run(ui.komut(grup_komut(ad, arg)))
    assert y is not None
    yanit_ingilizce(y)


def test_english_and_legacy_commands_do_the_same_work(conn, src, saat):
    _kaynak_hazirla(src)
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    ui = Arayuz(conn, src, KURUCU, saat=saat)
    run(ui.komut(grup_komut("addplayer", "72400")))
    assert "72400" in takipte(conn, G1)
    run(ui.komut(grup_komut("oyuncusil", "72400")))  # legacy removal -> confirmation, same path
    # both spellings reach the same handler and the same confirmation flow
    y1 = run(ui.komut(grup_komut("removeplayer", "72400")))
    y2 = run(ui.komut(grup_komut("oyuncu_sil", "72400")))
    assert y1.metin == y2.metin and "72400" in y1.metin


def test_help_lists_english_commands_only(ui):
    y = run(ui.komut(grup_komut("help")))
    for ad in KOMUTLAR:
        if ad != "start":
            assert f"/{ad}" in y.metin
    for eski in list(ESKI_KOMUTLAR) + list(TR_KOMUTLAR):
        assert f"/{eski}" not in y.metin
    yanit_ingilizce(y)
    assert "Europe/Istanbul" in y.metin


# ---------------------------------------------------------------- 2. screens, buttons, callbacks
def _tikla(ui, veri, kul=KURUCU):
    return run(ui.buton(grup_dugme(veri, kul=kul)))


def test_every_reachable_screen_button_and_alert_is_english(ui, src):
    _kaynak_hazirla(src)
    run(ui.komut(grup_komut("addplayer", "72400")))
    run(ui.komut(grup_komut("addparty", "1244")))
    kuyruk = ["m", "ol:0", "pl:0", "sl:0", "ta", "du", "yo", "yd", "oe", "pe", "gl", "tv:5", "tv:7", "xx:1", "nop"]
    gorulen = set()
    while kuyruk and len(gorulen) < 120:
        veri = kuyruk.pop(0)
        if veri in gorulen or veri.startswith("c:e:"):  # confirmations execute; covered separately
            continue
        gorulen.add(veri)
        y = _tikla(ui, veri)
        yanit_ingilizce(y)
        kuyruk += [v for v in veriler(y) if v not in gorulen]
    assert {"ol:0", "pl:0", "sl:0", "ta", "du", "yo", "yd"} <= gorulen
    assert len(gorulen) > 12


def test_confirmation_screens_and_results_are_english(ui, src):
    _kaynak_hazirla(src)
    run(ui.komut(grup_komut("addplayer", "72400")))
    run(ui.komut(grup_komut("addparty", "1244")))
    for cmd, arg in (("removeplayer", "72400"), ("removeparty", "1244")):
        y = run(ui.komut(grup_komut(cmd, arg)))
        yanit_ingilizce(y)
        kid = next(v for v in veriler(y) if v.startswith("c:e:")).split(":")[2]
        yanit_ingilizce(_tikla(ui, f"c:e:{kid}"))  # confirm
        yanit_ingilizce(_tikla(ui, f"c:e:{kid}"))  # replay -> expired/used alert
        yanit_ingilizce(_tikla(ui, "c:h:unknown"))  # cancel unknown


def test_denied_users_get_english_texts(ui, conn):
    yanit_ingilizce(run(ui.komut(komut("players", kul=YABANCI))))  # private chat: owner only
    yanit_ingilizce(run(ui.komut(grup_komut("players", kul=YABANCI))))  # approved group, not authorized
    yanit_ingilizce(run(ui.buton(grup_dugme("ol:0", kul=YABANCI))))
    yanit_ingilizce(run(ui.buton(grup_dugme("gk:-1002", kul=YETKILI))))  # owner-only action by authorized user
    yanit_ingilizce(run(ui.komut(grup_komut("remove_group", "-1002", kul=YETKILI))))


def test_stale_button_alert_is_english(ui):
    y = _tikla(ui, "zz:unknown")
    assert y.uyari and y.uyari_goster
    ingilizce_mi(y.uyari)


# ---------------------------------------------------------------- 3. notifications
_ORNEK_BILDIRIMLER = {
    "oyuncu_degisti": {
        "oyuncu_id": "72400", "ad": "Ali", "tespit": "2026-10-21T21:30:00.000000Z",
        "degisimler": [{"alan": "bolge", "eski": "Varna", "yeni": "Sofia"}],
    },
    "parti_ad_degisti": {"parti_id": "1244", "eski_ad": "OLD", "yeni_ad": "NEW", "tespit": "2026-10-21T21:30:00.000000Z"},
    "parti_uyelik": {
        "parti_id": "1244", "parti_ad": "TEKNO", "tespit": "2026-10-21T21:30:00.000000Z",
        "katilanlar": [{"oyuncu_id": "1", "ad": "Ann"}], "ayrilanlar": [{"oyuncu_id": "2", "ad": "Bob"}],
    },
    "yeni_savas": {
        "savas_id": "w1", "ad": "War One", "war_number": 77, "attacker_name": "AAA", "defender_name": "BBB",
        "durum": "active", "tur": "rebellion", "tespit": "2026-10-21T21:30:00.000000Z",
    },
    "yeni_savas_ozet": {
        "sayi": 7, "tespit": "2026-10-21T21:30:00.000000Z",
        "savaslar": [{"savas_id": "w1", "ad": "War One", "war_number": 77}],
    },
    "yetkisiz_deneme": {
        "kullanici_id": 77, "grup_id": -1001, "islem": "/oyuncuekle", "zaman": "2026-10-21T21:30:00.000000Z",
    },
    "yetkisiz_ozet": {"sayi": 4, "zaman": "2026-10-21T21:30:00.000000Z"},
    "token_gecersiz": {"zaman": "2026-10-21T21:30:00.000000Z"},
    "token_hatirlatma": {"gun": 7, "zaman": "2026-10-21T21:30:00.000000Z"},
    "grup_goc": {"eski": -1001, "yeni": -1009, "zaman": "2026-10-21T21:30:00.000000Z"},
    "denetim": {"eylem": "grup_onayla", "aktor_id": 1000, "hedef_id": -1001, "zaman": "2026-10-21T21:30:00.000000Z"},
    "denetim_ozet": {"sayi": 9, "zaman": "2026-10-21T21:30:00.000000Z"},
}


def test_every_notification_type_has_a_sample():
    assert set(_ORNEK_BILDIRIMLER) == set(bicim._BICIMLER)


@pytest.mark.parametrize("tur", sorted(_ORNEK_BILDIRIMLER))
def test_notification_text_is_english(tur):
    m = bicim.bildirim_metni(tur, _ORNEK_BILDIRIMLER[tur])
    ingilizce_mi(m)
    assert not _AMPM.search(m)


def test_unauthorized_notification_shows_english_command_names():
    m = bicim.bildirim_metni("yetkisiz_deneme", _ORNEK_BILDIRIMLER["yetkisiz_deneme"])
    assert "/addplayer" in m and "/oyuncuekle" not in m
    m2 = bicim.bildirim_metni("yetkisiz_deneme", {**_ORNEK_BILDIRIMLER["yetkisiz_deneme"], "islem": "onaysiz_gruba_eklendi"})
    ingilizce_mi(m2)
    assert "/approve_group -1001" in m2


def test_notification_times_are_istanbul_24h():
    m = bicim.bildirim_metni("oyuncu_degisti", _ORNEK_BILDIRIMLER["oyuncu_degisti"])
    assert "Detected: 22.10.2026 00:30" in m  # 21:30 UTC on the 21st -> 00:30 on the 22nd in Istanbul
    assert not _AMPM.search(m)
    m = bicim.bildirim_metni("denetim", _ORNEK_BILDIRIMLER["denetim"])
    assert "Time: 22.10.2026 00:30" in m
    assert "2026-10-21T21:30" not in m  # raw UTC storage format never reaches the user


def test_stored_notification_flow_is_english_end_to_end(conn, servis, src, saat):
    src.oyuncu("72400", "Ali", "Varna", "Bulgaria")
    run(servis.oyuncu_ekle(G1, "72400", src))
    run(servis.oyuncular_tara(G1, src))  # baseline
    src.oyuncu("72400", "Ali", "Sofia", "Bulgaria")
    run(servis.oyuncular_tara(G1, src))
    b = bildirimler(conn, "oyuncu_degisti")
    assert b, "region change must produce a notification"
    m = bicim.bildirim_metni(b[0]["tur"], b[0]["veri"])
    ingilizce_mi(m)
    assert "Varna" in m and "Sofia" in m
    assert "Detected: 19.10.2026 15:00" in m  # 12:00 UTC stored -> 15:00 Istanbul shown
    assert _TARIH_SAAT.search(m) and not _AMPM.search(m)


# ---------------------------------------------------------------- 4. timezone conversion
@pytest.mark.parametrize(
    "utc,beklenen",
    [
        ("2026-10-22T18:56:00.000000Z", "22.10.2026 21:56"),  # example from the requirements
        ("2026-10-22T06:05:00.000000Z", "22.10.2026 09:05"),  # zero-padded morning
        ("2026-10-21T21:30:00.000000Z", "22.10.2026 00:30"),  # crosses UTC midnight forward
        ("2026-10-21T21:00:00.000000Z", "22.10.2026 00:00"),
        ("2026-10-21T20:59:00.000000Z", "21.10.2026 23:59"),
        ("2026-12-31T21:30:00.000000Z", "01.01.2027 00:30"),  # year boundary
        ("2026-02-28T22:15:00.000000Z", "01.03.2026 01:15"),  # month boundary
        ("2026-07-01T09:00:00.000000Z", "01.07.2026 12:00"),  # summer: Istanbul is UTC+3 all year (no DST)
        ("2026-01-15T12:00:00.000000Z", "15.01.2026 15:00"),  # winter: same offset
        ("2026-10-22T12:00:00.000000Z", "22.10.2026 15:00"),  # afternoon: 24-hour clock, never 3:00 PM
        ("2026-10-22T20:45:00.000000Z", "22.10.2026 23:45"),
    ],
)
def test_utc_to_istanbul_24h(utc, beklenen):
    from datetime import timezone

    dt = iso_coz(utc)
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0  # storage stays aware UTC
    assert yerel_goster(dt) == beklenen
    assert bicim.tarih(utc) == beklenen
    assert _TARIH_SAAT.fullmatch(beklenen)
    assert not _AMPM.search(yerel_goster(dt))
    assert yerel_saat(dt) == beklenen[-5:]
    assert dt.astimezone(timezone.utc) == dt  # the helpers never mutate / re-label the instant


def test_all_24_hours_have_no_am_pm_and_roundtrip():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
    saatler = set()
    for h in range(24):
        s = yerel_goster(base + timedelta(hours=h))
        assert _TARIH_SAAT.fullmatch(s) and not _AMPM.search(s)
        saatler.add(int(s[11:13]))
    assert saatler == set(range(24))


def test_naive_datetime_is_treated_as_utc_not_server_local():
    from datetime import datetime

    assert yerel_goster(datetime(2026, 10, 22, 18, 56)) == "22.10.2026 21:56"


def test_display_does_not_depend_on_server_timezone():
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    eski = os.environ.get("TZ")
    try:
        for tz in ("UTC", "America/New_York", "Asia/Tokyo", "Pacific/Auckland"):
            os.environ["TZ"] = tz
            time.tzset()
            assert bicim.tarih("2026-10-21T21:30:00.000000Z") == "22.10.2026 00:30", tz
    finally:
        if eski is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = eski
        time.tzset()


def test_display_timezone_constant_and_invalid_input():
    assert config.DISPLAY_TIMEZONE == "Europe/Istanbul"
    assert bicim.tarih(None) == "unknown" and bicim.tarih("") == "unknown" and bicim.tarih("garbage") == "unknown"


def test_storage_stays_utc_and_scheduler_comparisons_are_unchanged(conn, saat):
    """Presentation-only change: stored timestamps keep the fixed-width UTC ISO format and sort textually."""
    s = iso(saat())
    assert s.endswith("Z") and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", s)
    assert s == "2026-10-19T12:00:00.000000Z"  # 12:00 UTC, NOT 15:00 local
    assert iso_coz(s) == saat()
    assert bicim.tarih(s) == "19.10.2026 15:00"
    from datetime import timedelta

    assert iso(saat()) < iso(saat() + timedelta(minutes=5))


# ---------------------------------------------------------------- 5. status screen
def _durum_tohumla(conn, saat):
    for tip in ("oyuncular", "partiler", "savaslar"):
        conn.execute("INSERT INTO tarama_isleri(group_id, is_tipi, son_deneme, son_basari) VALUES(?,?,?,?)",
                     (G1, tip, iso(saat()), iso(saat())))
    conn.execute("UPDATE grup_ayarlari SET sonraki_tarama=? WHERE group_id=?", ("2026-10-19T21:30:00.000000Z", G1))


REMOVED_1 = "Liste sayfalamasız varsayılmıştır"
REMOVED_2 = "canlı bağlantı test edilmedi"


def test_status_has_the_requested_fields_in_english_istanbul_time(ui, conn, src, saat):
    src.oyuncu("1", "A")
    run(ui.komut(grup_komut("addplayer", "1")))
    _durum_tohumla(conn, saat)
    y = run(ui.komut(grup_komut("status")))
    t = y.metin
    ingilizce_mi(t)
    for parca in (
        "<b>Bot Status</b>", f"Group: <code>{G1}</code>", "Tracked Players: 1", "Tracked Parties: 0",
        "Scan Interval: 10 minutes", "Last Successful Scan:", "• Players: 19.10.2026 15:00",
        "• Parties: 19.10.2026 15:00", "• Wars: 19.10.2026 15:00",
        "Next Scheduled Scan: 20.10.2026 00:30", "Scan in Progress: No", "Pending Notifications: 0",
    ):
        assert parca in t, parca
    assert not _AMPM.search(t)


@pytest.mark.parametrize("giris", ["cmd", "button", "legacy"])
def test_status_never_shows_the_two_removed_diagnostics(ui, conn, src, saat, giris):
    src.oyuncu("1", "A")
    run(ui.komut(grup_komut("addplayer", "1")))
    for durum in ("fresh", "seeded"):
        if durum == "seeded":
            _durum_tohumla(conn, saat)
        if giris == "cmd":
            y = run(ui.komut(grup_komut("status")))
        elif giris == "legacy":
            y = run(ui.komut(grup_komut("durum")))
        else:
            y = run(ui.buton(grup_dugme("du")))
        for yasak in (REMOVED_1, REMOVED_2, "sayfalamasız", "pagination", "DevTools", "doğrulanmadı",
                      "not verified", "unverified", "yapılandırıldı", "Diplomacia bağlantısı"):
            assert yasak not in y.metin, (giris, durum, yasak)
        assert "Diplomacia connection" not in y.metin  # healthy status shows no connection boilerplate


def test_status_private_chat_without_group(ui):
    y = run(ui.komut(komut("status")))
    ingilizce_mi(y.metin)
    assert "Approved groups: 2; authorized users: 1" in y.metin
    assert REMOVED_1 not in y.metin and REMOVED_2 not in y.metin


def test_status_reports_real_problems_only(conn, src, saat):
    class Z:
        token_duraklatildi = True

        def calisiyor_mu(self, g):
            return True

    ui2 = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=Z())
    t = run(ui2.komut(grup_komut("status"))).metin
    ingilizce_mi(t)
    assert "Scan in Progress: Yes" in t
    assert "Diplomacia connection: the site rejected the token (401); scans are paused" in t
    assert "Next Scheduled Scan: paused (token)" in t


def test_status_shows_failed_scan_error_in_english(ui, conn, saat):
    conn.execute("INSERT INTO tarama_isleri(group_id, is_tipi, son_deneme, son_hata) VALUES(?,?,?,?)",
                 (G1, "oyuncular", iso(saat()), "rate limit"))
    t = run(ui.komut(grup_komut("status"))).metin
    assert "Diplomacia connection: last scan failed (oyuncular): rate limit" in t


# ---------------------------------------------------------------- 6. authorization and isolation unchanged
KISILER = {"owner": KURUCU, "authorized": YETKILI, "stranger": YABANCI}
SOHBETLER = ["private", "approved_group", "unapproved_group"]
BEKLENEN = {
    ("owner", "private"): True, ("authorized", "private"): False, ("stranger", "private"): False,
    ("owner", "approved_group"): True, ("authorized", "approved_group"): True, ("stranger", "approved_group"): False,
    ("owner", "unapproved_group"): False, ("authorized", "unapproved_group"): False,
    ("stranger", "unapproved_group"): False,
}


def _args(kisi_id, sohbet):
    if sohbet == "private":
        return dict(sohbet=kisi_id, tur="private", kul=kisi_id)
    return dict(sohbet=G1 if sohbet == "approved_group" else G3, tur="supergroup", kul=kisi_id)


@pytest.mark.parametrize("sohbet", SOHBETLER)
@pytest.mark.parametrize("kisi", list(KISILER))
@pytest.mark.parametrize("cmd", ["help", "yardim", "players", "oyuncular"])
def test_authorization_matrix_is_identical_for_english_and_legacy_names(ui, conn, kisi, sohbet, cmd):
    y = run(ui.komut(komut(cmd, **_args(KISILER[kisi], sohbet))))
    if BEKLENEN[(kisi, sohbet)]:
        assert y is not None and y.metin
    else:
        assert y is None or (y.metin in (ar.KAPALI_OZEL, ar.YETKISIZ))
        n = conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE user_id=?", (KISILER[kisi],)).fetchone()[0]
        assert n == 1


def test_denied_messages_are_english_but_audit_names_are_unchanged(ui, conn):
    y = run(ui.komut(komut("players", kul=YABANCI)))
    assert y.metin == ar.KAPALI_OZEL == "In private chat, this bot is available to the owner only."
    assert ar.YETKISIZ == "You are not authorized to perform this action."
    b = bildirimler(conn, "yetkisiz_deneme", hedef=KURUCU)
    assert b[0]["veri"]["islem"] == "/oyuncular"  # stored identifier unchanged
    assert "/players" in bicim.bildirim_metni("yetkisiz_deneme", b[0]["veri"])  # displayed in English


def test_owner_only_actions_remain_owner_only(ui, conn):
    y = run(ui.komut(grup_komut("remove_group", str(G2), kul=YETKILI)))
    assert y.metin == ar.YALNIZ_KURUCU
    assert G2 in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar  # nothing changed


def test_ordinary_group_members_gain_no_new_privileges(ui, conn):
    for cmd in ("addplayer", "grant", "approve_group", "scan"):
        y = run(ui.komut(grup_komut(cmd, "1", kul=YABANCI)))
        assert y.metin == ar.YETKISIZ
    assert takipte(conn, G1) == set()
    assert yonetim.durum_yukle(conn, KURUCU).yetkililer == frozenset({YETKILI})


def test_group_isolation_is_unchanged(ui, conn, src):
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("addplayer", "72400", grup=G1)))
    assert takipte(conn, G1) == {"72400"} and takipte(conn, G2) == set()
    y2 = run(ui.komut(grup_komut("players", grup=G2)))
    assert "72400" not in y2.metin and "Ali" not in y2.metin
    ingilizce_mi(y2.metin)
    y1 = run(ui.komut(grup_komut("players", grup=G1)))
    assert "Ali" in y1.metin
    # scan interval is per group
    run(ui.komut(grup_komut("scan", "20", grup=G1)))
    assert yonetim.tarama_araligi(conn, G1) == 20
    assert yonetim.tarama_araligi(conn, G2) == 10
