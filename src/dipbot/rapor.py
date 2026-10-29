"""Takip alanı özetleri (SALT OKUNUR; yalnız SELECT). `/lists` komutu ve geçiş analizi (`dipbot.migrasyon`) kullanır.

Merkezi alan: `config.MERKEZ_ID`. Eski grup kimlikleri YALNIZ arşiv/geçiş kaynağıdır (taranmaz, bildirim üretmez)."""
from __future__ import annotations

import json
import sqlite3

from . import config

MIGRASYON_ANAHTARI = "merkez_migrasyon"

_VERI_TABLOLARI = ("takip_oyuncular", "takip_partiler", "savaslar", "engeller", "grup_ayarlari", "onayli_gruplar")


def _sayi(conn, sql: str, *a) -> int:
    return conn.execute(sql, a).fetchone()[0]


def alan_idleri(conn: sqlite3.Connection) -> list[int]:
    """Veride görülen TÜM alan kimlikleri (merkezi alan dahil)."""
    idler: set[int] = set()
    for tablo in _VERI_TABLOLARI:
        idler.update(r[0] for r in conn.execute(f"SELECT DISTINCT group_id FROM {tablo}"))
    return sorted(idler)


def arsiv_alanlari(conn: sqlite3.Connection) -> list[int]:
    """Eski grup kimlikleri (merkezi alan dışındaki tüm alan kimlikleri): yalnız arşiv."""
    return [g for g in alan_idleri(conn) if g != config.MERKEZ_ID]


def alan_ozeti(conn: sqlite3.Connection, gid: int) -> dict:
    ayar = conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (gid,)).fetchone()
    isler = [r["son_basari"] for r in conn.execute(
        "SELECT son_basari FROM tarama_isleri WHERE group_id=? AND son_basari IS NOT NULL", (gid,))]
    return {
        "alan_id": gid,
        "oyuncu": _sayi(conn, "SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=?", gid),
        "oyuncu_elle": _sayi(conn, "SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=? AND elle_eklendi=1", gid),
        "parti": _sayi(conn, "SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", gid),
        "parti_uyesi": _sayi(conn, "SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=?", gid),
        "savas": _sayi(conn, "SELECT COUNT(*) FROM savaslar WHERE group_id=?", gid),
        "engel": _sayi(conn, "SELECT COUNT(*) FROM engeller WHERE group_id=?", gid),
        "aralik_dk": ayar["tarama_araligi_dk"] if ayar else None,
        "savas_baseline": bool(ayar["savas_baseline_tamam"]) if ayar else False,
        "son_tarama": max(isler, default=None),
        "bekleyen_bildirim": _sayi(conn, "SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", gid),
    }


def merkez_ozeti(conn: sqlite3.Connection, kapsam: int = config.MERKEZ_ID) -> dict:
    o = alan_ozeti(conn, kapsam)
    if o["aralik_dk"] is None:
        o["aralik_dk"] = config.DEFAULT_SCAN_INTERVAL_MINUTES
    o["arsiv_alanlari"] = len([g for g in alan_idleri(conn) if g != kapsam])
    r = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (MIGRASYON_ANAHTARI,)).fetchone()
    o["migrasyon_uygulandi"] = r is not None
    o["migrasyon"] = json.loads(r[0]) if r is not None else None
    return o


def bekleyen_hedefler(conn: sqlite3.Connection) -> list[tuple[int, int | None, int]]:
    """Bekleyen outbox satırları: (hedef_sohbet, grup_id, adet)."""
    return [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT hedef_sohbet, grup_id, COUNT(*) FROM outbox WHERE durum='bekliyor' GROUP BY 1,2 ORDER BY 1,2")]
