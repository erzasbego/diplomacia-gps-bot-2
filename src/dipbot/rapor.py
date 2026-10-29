"""Takip listesi raporu (SALT OKUNUR): hangi takip LİSTESİNİN (eski grup kimliği) kullanılacağına kurucu karar verebilsin.

Hiçbir şeyi değiştirmez, birleştirmez, silmez; yalnız SELECT çalıştırır (CLI veritabanını `mode=ro` açar). Çıktı sır içermez;
varsayılan çıktıda oyuncu/parti ADI yoktur (yalnız sayılar ve kimlik çakışma sayıları); `--ayrinti` ile ortak/özgün
KİMLİKLER listelenir (yine ad yok).

Kullanım (sunucuda `dipbot` kullanıcısıyla):
    sudo -u dipbot env PYTHONPATH=/opt/dipbot/src /opt/dipbot/.venv/bin/python -m dipbot.rapor /var/lib/dipbot/dipbot.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import config
from .tg.bicim import tarih


def _sayi(conn, sql: str, *a) -> int:
    return conn.execute(sql, a).fetchone()[0]


def _liste_idleri(conn) -> list[int]:
    idler: set[int] = set()
    for tablo, kolon in (
        ("onayli_gruplar", "group_id"), ("grup_ayarlari", "group_id"), ("takip_oyuncular", "group_id"),
        ("takip_partiler", "group_id"), ("savaslar", "group_id"),
    ):
        idler.update(r[0] for r in conn.execute(f"SELECT DISTINCT {kolon} FROM {tablo}"))
    return sorted(idler)


def liste_ozeti(conn: sqlite3.Connection, aktif=frozenset()) -> list[dict]:
    """Her takip listesi (grup kimliği) için sayılar. `aktif`: şu an taranacak olarak seçili kimlikler."""
    out = []
    for gid in _liste_idleri(conn):
        onayli = conn.execute("SELECT donem FROM onayli_gruplar WHERE group_id=?", (gid,)).fetchone()
        ayar = conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (gid,)).fetchone()
        isler = {r["is_tipi"]: r["son_basari"] for r in conn.execute(
            "SELECT is_tipi, son_basari FROM tarama_isleri WHERE group_id=?", (gid,))}
        out.append(
            {
                "liste_id": gid,
                "onayli": onayli is not None,
                "aktif": gid in aktif,
                "oyuncu": _sayi(conn, "SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=?", gid),
                "oyuncu_elle": _sayi(conn, "SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=? AND elle_eklendi=1", gid),
                "parti": _sayi(conn, "SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", gid),
                "parti_uyesi": _sayi(conn, "SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=?", gid),
                "savas": _sayi(conn, "SELECT COUNT(*) FROM savaslar WHERE group_id=?", gid),
                "engel": _sayi(conn, "SELECT COUNT(*) FROM engeller WHERE group_id=?", gid),
                "aralik_dk": ayar["tarama_araligi_dk"] if ayar else None,
                "son_tarama": max((v for v in isler.values() if v), default=None),
                "bekleyen_bildirim": _sayi(
                    conn, "SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", gid),
            }
        )
    return out


def _kume(conn, tablo: str, kolon: str, gid: int) -> set[str]:
    return {str(r[0]) for r in conn.execute(f"SELECT {kolon} FROM {tablo} WHERE group_id=?", (gid,))}


def cakisma(conn: sqlite3.Connection) -> list[dict]:
    """Liste çiftleri için ortak oyuncu/parti/savaş kimlik sayıları (kimliklerle; ad yok)."""
    idler = _liste_idleri(conn)
    out = []
    for i, a in enumerate(idler):
        for b in idler[i + 1:]:
            r = {"a": a, "b": b}
            for ad, tablo, kolon in (("oyuncu", "takip_oyuncular", "oyuncu_id"), ("parti", "takip_partiler", "parti_id"),
                                     ("savas", "savaslar", "savas_id")):
                ka, kb = _kume(conn, tablo, kolon, a), _kume(conn, tablo, kolon, b)
                r[ad] = {"ortak": len(ka & kb), "yalniz_a": len(ka - kb), "yalniz_b": len(kb - ka),
                         "ortak_idler": sorted(ka & kb)}
            out.append(r)
    return out


def bekleyen_hedefler(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Bekleyen outbox satırları: (hedef_sohbet, adet). Kanal geçişinde eski grup hedefleri İPTAL edilir."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT hedef_sohbet, COUNT(*) FROM outbox WHERE durum='bekliyor' GROUP BY 1 ORDER BY 1")]


def metin(conn: sqlite3.Connection, aktif=frozenset(), *, ayrinti: bool = False, kanal_id: int | None = None) -> str:
    kanal_id = config.LOG_KANAL_ID if kanal_id is None else kanal_id
    satirlar = ["TRACKING LIST REPORT (read-only; nothing was changed, merged or deleted)", ""]
    ozet = liste_ozeti(conn, aktif)
    if not ozet:
        satirlar.append("No tracking data found.")
    for o in ozet:
        satirlar.append(
            f"List {o['liste_id']}: {'ACTIVE (scanned)' if o['aktif'] else 'not active'}; "
            f"{'approved' if o['onayli'] else 'not approved'}"
        )
        satirlar.append(
            f"  players: {o['oyuncu']} (manually added: {o['oyuncu_elle']}), parties: {o['parti']} "
            f"(members stored: {o['parti_uyesi']}), wars: {o['savas']}, blocks: {o['engel']}"
        )
        satirlar.append(
            f"  scan interval: {o['aralik_dk']} min, last successful scan: "
            f"{tarih(o['son_tarama']) if o['son_tarama'] else 'none'}, pending notifications: {o['bekleyen_bildirim']}"
        )
    cak = cakisma(conn)
    if cak:
        satirlar += ["", "OVERLAP BETWEEN LISTS (same Diplomacia IDs; no names)"]
        for c in cak:
            satirlar.append(
                f"  {c['a']} vs {c['b']}: players common {c['oyuncu']['ortak']} "
                f"(only {c['a']}: {c['oyuncu']['yalniz_a']}, only {c['b']}: {c['oyuncu']['yalniz_b']}); "
                f"parties common {c['parti']['ortak']} (only {c['a']}: {c['parti']['yalniz_a']}, "
                f"only {c['b']}: {c['parti']['yalniz_b']}); wars common {c['savas']['ortak']}"
            )
            if ayrinti:
                for ad in ("oyuncu", "parti", "savas"):
                    if c[ad]["ortak_idler"]:
                        satirlar.append(f"    common {ad} ids: {', '.join(c[ad]['ortak_idler'][:200])}")
    hedefler = bekleyen_hedefler(conn)
    satirlar += ["", f"PENDING NOTIFICATION QUEUE by target chat (log channel = {kanal_id})"]
    if not hedefler:
        satirlar.append("  empty")
    for hedef, adet in hedefler:
        not_ = "log channel" if hedef == kanal_id else "OLD GROUP/OTHER (will be CANCELLED on start, never sent)"
        satirlar.append(f"  {hedef}: {adet} pending -> {not_}")
    satirlar += [
        "",
        "NEXT STEP (owner decision): choose which list becomes the tracking list, then set TRACKING_GROUP_IDS=<list id> "
        "in the service environment and restart. Lists are NOT merged automatically; unselected lists are kept untouched "
        "and are not scanned.",
    ]
    return "\n".join(satirlar)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dipbot.rapor", description=__doc__.split("\n")[0])
    ap.add_argument("db", help="SQLite dosyası (salt okunur açılır)")
    ap.add_argument("--ayrinti", action="store_true", help="ortak kimlikleri de listele")
    ap.add_argument("--aktif", default="", help="seçili/aktif liste kimlikleri (virgülle); varsayılan: TRACKING_GROUP_IDS")
    a = ap.parse_args(argv)
    yol = Path(a.db)
    if not yol.is_file():
        print(f"ERROR: database not found: {yol}", file=sys.stderr)
        return 2
    import os

    ham = a.aktif or os.environ.get("TRACKING_GROUP_IDS", "")
    try:
        aktif = frozenset(int(x) for x in ham.replace(";", ",").split(",") if x.strip())
    except ValueError:
        print("ERROR: --aktif/TRACKING_GROUP_IDS must be comma-separated integers", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{yol.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(metin(conn, aktif, ayrinti=a.ayrinti))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
