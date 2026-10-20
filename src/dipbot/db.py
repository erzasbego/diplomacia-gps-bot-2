"""SQLite bağlantısı, şema ve transaction yardımcısı.

Tüm zamanlar UTC (zaman.iso biçimi). Gruba ait tablolarda group_id (Telegram sayısal
grup kimliği) her zaman anahtarın parçasıdır; gruplar arası veri paylaşımı yoktur.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

SEMA_SURUMU = 6  # 2: takip_oyuncular.uid; 3: grup_gocleri; 4: onayli_gruplar.donem + outbox.onay_donemi (D7);
#                 5: takip_partiler.sinirli/toplam_uye/xp_sirali (büyük parti: ilk 100 üye);
#                 6: parti_uyeleri.xp + takip_oyuncular.xp_alti_sayac/xp_onayli (XP süzgeci)


def _sema() -> str:
    izinli = ",".join(str(x) for x in config.ALLOWED_SCAN_INTERVALS_MINUTES)
    return f"""
CREATE TABLE IF NOT EXISTS yetkili_kullanicilar(
    user_id INTEGER PRIMARY KEY,
    eklendi TEXT NOT NULL
);
-- donem: her YENİ onayda artan tamsayı (sistem_durumu['onay_donemi_sayaci']); saat/zaman damgasına bağlı değildir.
CREATE TABLE IF NOT EXISTS onayli_gruplar(
    group_id INTEGER PRIMARY KEY,
    onaylandi TEXT NOT NULL,
    donem INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS grup_ayarlari(
    group_id INTEGER PRIMARY KEY,
    tarama_araligi_dk INTEGER NOT NULL DEFAULT {config.DEFAULT_SCAN_INTERVAL_MINUTES}
        CHECK(tarama_araligi_dk IN ({izinli})),
    savas_baseline_tamam INTEGER NOT NULL DEFAULT 0,
    son_tarama_denemesi TEXT,
    sonraki_tarama TEXT,
    ardisik_hata INTEGER NOT NULL DEFAULT 0
);
-- Gruba özel takip edilen oyuncular + o grubun son BAŞARILI gözlemi.
-- ad / bolge / vatandaslik_ulkesi ayrı alanlardır; oyuncu_id sabit kimliktir.
CREATE TABLE IF NOT EXISTS takip_oyuncular(
    group_id INTEGER NOT NULL,
    oyuncu_id TEXT NOT NULL,
    ad TEXT,
    bolge TEXT,
    vatandaslik_ulkesi TEXT,
    son_gozlem TEXT,
    baseline_tamam INTEGER NOT NULL DEFAULT 0,  -- 0: henüz ilk gözlem yapılmadı (bildirimsiz)
    elle_eklendi INTEGER NOT NULL DEFAULT 0,
    eklendi TEXT NOT NULL,
    uid TEXT,                                   -- kaynağın kalıcı kimliği (UUID); ilk gözlemde yazılır
    xp_alti_sayac INTEGER NOT NULL DEFAULT 0,   -- OTOMATİK takipli: art arda XP-eşiği-altı gözlem sayısı
    xp_onayli INTEGER NOT NULL DEFAULT 0,       -- 1: XP süzgeciyle (>= eşik) onaylandı; 0: eski kayıt (XP kuralına tabi DEĞİL)
    PRIMARY KEY(group_id, oyuncu_id)
);
CREATE TABLE IF NOT EXISTS takip_partiler(
    group_id INTEGER NOT NULL,
    parti_id TEXT NOT NULL,
    ad TEXT,
    baseline_tamam INTEGER NOT NULL DEFAULT 1,
    son_dogrulama TEXT,
    eklendi TEXT NOT NULL,
    sinirli INTEGER NOT NULL DEFAULT 0,  -- 1: üye listesi API'nin döndürdüğü İLK 100 (geçerli ama kısmi)
    toplam_uye INTEGER,                  -- partinin gerçek üye sayısı (member_count); eski kayıtlarda NULL
    xp_sirali INTEGER,                   -- sınırlı listede xp alanları azalan mıydı: 1 / 0 / NULL (bilinmiyor)
    PRIMARY KEY(group_id, parti_id)
);
-- Partinin SON DOĞRULANMIŞ (tam sayfalanmış) üye listesi.
CREATE TABLE IF NOT EXISTS parti_uyeleri(
    group_id INTEGER NOT NULL,
    parti_id TEXT NOT NULL,
    oyuncu_id TEXT NOT NULL,
    ad TEXT,
    xp INTEGER,                                 -- son gözlenen XP; NULL = eksik/geçersiz/bilinmiyor
    PRIMARY KEY(group_id, parti_id, oyuncu_id)
);
-- Grup düzeyinde yeniden-ekleme engeli (sessiz otomatik eklemeyi önler).
-- Art arda 404 (bulunamadı) sayaçları; yalnız işaret içindir, kayıt silinmez.
CREATE TABLE IF NOT EXISTS bulunamadi_sayaclari(
    group_id INTEGER NOT NULL,
    tur TEXT NOT NULL,
    kimlik TEXT NOT NULL,
    sayac INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(group_id, tur, kimlik)
);
CREATE TABLE IF NOT EXISTS engeller(
    group_id INTEGER NOT NULL,
    oyuncu_id TEXT NOT NULL,
    neden TEXT NOT NULL,
    olusturuldu TEXT NOT NULL,
    PRIMARY KEY(group_id, oyuncu_id)
);
CREATE TABLE IF NOT EXISTS savaslar(
    group_id INTEGER NOT NULL,
    savas_id TEXT NOT NULL,
    ad TEXT,
    ilk_gorulme TEXT NOT NULL,
    PRIMARY KEY(group_id, savas_id)
);
-- Savaş ayrıntısı için son başarılı yanıt (savaş verisi gruba özel değil).
CREATE TABLE IF NOT EXISTS savas_detay_onbellek(
    savas_id TEXT PRIMARY KEY,
    veri TEXT NOT NULL,
    zaman TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hedef_sohbet INTEGER NOT NULL,
    grup_id INTEGER,              -- NULL: kurucuya özel bildirim
    tur TEXT NOT NULL,
    idem_anahtari TEXT NOT NULL,
    veri TEXT NOT NULL,           -- JSON
    durum TEXT NOT NULL DEFAULT 'bekliyor'
        CHECK(durum IN ('bekliyor','gonderildi','iptal','basarisiz')),
    deneme INTEGER NOT NULL DEFAULT 0,
    sonraki_deneme TEXT NOT NULL,
    son_hata TEXT,
    olusturuldu TEXT NOT NULL,
    gonderildi TEXT,
    onay_donemi INTEGER,          -- grup_id doluysa satır üretilirken grubun onay dönemi (0: onaysız/bayat)
    UNIQUE(hedef_sohbet, idem_anahtari)
);
CREATE INDEX IF NOT EXISTS ix_outbox_durum ON outbox(durum, sonraki_deneme);
CREATE TABLE IF NOT EXISTS tarama_isleri(
    group_id INTEGER NOT NULL,
    is_tipi TEXT NOT NULL,        -- oyuncular | partiler | savaslar
    son_deneme TEXT,
    son_basari TEXT,
    son_hata TEXT,
    PRIMARY KEY(group_id, is_tipi)
);
-- Telegram migrate olayı (eski->yeni) kaydı. Yalnız kayıttır: yeni kimliğe yetki VERMEZ, veri taşımaz.
-- Taşıma yalnız kurucunun /grup_tasi onayıyla (yonetim.grup_tasi) yapılır.
CREATE TABLE IF NOT EXISTS grup_gocleri(
    eski_id INTEGER NOT NULL,
    yeni_id INTEGER NOT NULL,
    tespit_zamani TEXT NOT NULL,
    durum TEXT NOT NULL DEFAULT 'bekliyor' CHECK(durum IN ('bekliyor','tamamlandi','iptal')),
    PRIMARY KEY(eski_id, yeni_id),
    CHECK(eski_id <> yeni_id)
);
CREATE TABLE IF NOT EXISTS sistem_durumu(
    anahtar TEXT PRIMARY KEY,
    deger TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS yetkisiz_denemeler(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zaman TEXT NOT NULL,
    user_id INTEGER,
    group_id INTEGER,
    islem TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bildirim_sinirlari(
    anahtar TEXT PRIMARY KEY,
    son_bildirim TEXT NOT NULL
);
"""


def baglan(yol: str | Path = ":memory:") -> sqlite3.Connection:
    """Bağlantı açar ve şemayı kurar. Autocommit modu; transaction'lar `islem` ile açık."""
    if str(yol) != ":memory:":
        Path(yol).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(yol), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if str(yol) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_sema())
    _goc(conn)
    return conn


def _goc(conn: sqlite3.Connection) -> None:
    """Basit, tekrarlanabilir migration: eksik sütunlar eklenir, sonra user_version yükseltilir."""
    surum = conn.execute("PRAGMA user_version").fetchone()[0]
    if surum < 2:
        sutunlar = [r[1] for r in conn.execute("PRAGMA table_info(takip_oyuncular)")]
        if "uid" not in sutunlar:
            conn.execute("ALTER TABLE takip_oyuncular ADD COLUMN uid TEXT")
    if surum < 4:
        _donem_goc(conn)
    if surum < 5:  # additive: mevcut kayıtlar korunur, yeni sütunlar güvenli varsayılanla gelir
        sutunlar = {r[1] for r in conn.execute("PRAGMA table_info(takip_partiler)")}
        for ad, tanim in (("sinirli", "INTEGER NOT NULL DEFAULT 0"), ("toplam_uye", "INTEGER"), ("xp_sirali", "INTEGER")):
            if ad not in sutunlar:
                conn.execute(f"ALTER TABLE takip_partiler ADD COLUMN {ad} {tanim}")
    if surum < 6:  # additive: mevcut kayıtlar korunur; eski otomatik kayıtlar XP kuralına tabi olmaz (xp_onayli=0)
        for tablo, ad, tanim in (
            ("parti_uyeleri", "xp", "INTEGER"),
            ("takip_oyuncular", "xp_alti_sayac", "INTEGER NOT NULL DEFAULT 0"),
            ("takip_oyuncular", "xp_onayli", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if ad not in {r[1] for r in conn.execute(f"PRAGMA table_info({tablo})")}:
                conn.execute(f"ALTER TABLE {tablo} ADD COLUMN {ad} {tanim}")
    if surum < SEMA_SURUMU:
        conn.execute(f"PRAGMA user_version={SEMA_SURUMU}")


DONEM_SAYACI = "onay_donemi_sayaci"


def _donem_goc(conn: sqlite3.Connection) -> None:
    """v4 (D7): onay dönemini zaman damgası karşılaştırması yerine artan tamsayıya taşır.
    Mevcut onaylı gruplar onay sırasıyla 1..N dönemini alır; bekleyen outbox satırı onaydan SONRA
    üretilmişse güncel dönemi, ÖNCE üretilmişse 0'ı (bayat -> gönderimde iptal) alır."""
    with islem(conn):
        gs = [r[1] for r in conn.execute("PRAGMA table_info(onayli_gruplar)")]
        if "donem" not in gs:
            conn.execute("ALTER TABLE onayli_gruplar ADD COLUMN donem INTEGER NOT NULL DEFAULT 0")
        os_ = [r[1] for r in conn.execute("PRAGMA table_info(outbox)")]
        if "onay_donemi" not in os_:
            conn.execute("ALTER TABLE outbox ADD COLUMN onay_donemi INTEGER")
        n = 0
        for r in conn.execute("SELECT group_id FROM onayli_gruplar ORDER BY onaylandi, group_id").fetchall():
            n += 1
            conn.execute("UPDATE onayli_gruplar SET donem=? WHERE group_id=?", (n, r[0]))
        conn.execute(
            "UPDATE outbox SET onay_donemi = COALESCE(("
            " SELECT CASE WHEN outbox.olusturuldu >= o.onaylandi THEN o.donem ELSE 0 END"
            " FROM onayli_gruplar o WHERE o.group_id=outbox.grup_id), 0)"
            " WHERE grup_id IS NOT NULL AND onay_donemi IS NULL"
        )
        mevcut = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (DONEM_SAYACI,)).fetchone()
        if mevcut is None:
            conn.execute("INSERT INTO sistem_durumu(anahtar, deger) VALUES(?,?)", (DONEM_SAYACI, str(n)))


def sonraki_onay_donemi(conn: sqlite3.Connection) -> int:
    """Artan onay dönemi sayacı (hiç geri dönmez, yeniden kullanılmaz). Çağıran transaction içinde olmalı."""
    r = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (DONEM_SAYACI,)).fetchone()
    yeni = (int(r[0]) if r else 0) + 1
    conn.execute("INSERT OR REPLACE INTO sistem_durumu(anahtar, deger) VALUES(?,?)", (DONEM_SAYACI, str(yeni)))
    return yeni


@contextmanager
def islem(conn: sqlite3.Connection):
    """Atomik blok. Zaten transaction içindeyse dıştakine katılır."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
