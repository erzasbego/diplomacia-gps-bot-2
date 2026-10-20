"""Onay bekleyen işlemler (silme, yetki/grup kaldırma, parti kaldırma).

callback_data yalnız kısa rastgele anahtar taşır; asıl veri burada (SQLite) durur. İşlem tek
kullanımlıktır, sürelidir ve oluşturulduğu sohbet + kullanıcıya bağlıdır.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from ..db import islem
from ..zaman import Saat, iso, iso_coz

ONAY_OMRU = timedelta(minutes=10)

_SEMA = """
CREATE TABLE IF NOT EXISTS tg_islemler(
    id TEXT PRIMARY KEY,
    tur TEXT NOT NULL,
    sohbet_id INTEGER NOT NULL,
    kullanici_id INTEGER NOT NULL,
    grup_id INTEGER,            -- NULL: genel işlem (yetki, grup onayı)
    veri TEXT NOT NULL,
    bitis TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class BekleyenIslem:
    id: str
    tur: str
    sohbet_id: int
    kullanici_id: int
    grup_id: int | None
    veri: dict
    bitis: str


class IslemDeposu:
    def __init__(self, conn: sqlite3.Connection, saat: Saat):
        self.conn = conn
        self.saat = saat
        conn.executescript(_SEMA)

    def olustur(self, tur: str, sohbet_id: int, kullanici_id: int, grup_id: int | None, veri: dict) -> str:
        simdi = self.saat()
        with islem(self.conn):
            self.conn.execute("DELETE FROM tg_islemler WHERE bitis<?", (iso(simdi),))
            for _ in range(10):
                kimlik = secrets.token_hex(4)
                try:
                    self.conn.execute(
                        "INSERT INTO tg_islemler(id, tur, sohbet_id, kullanici_id, grup_id, veri, bitis)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (kimlik, tur, sohbet_id, kullanici_id, grup_id,
                         json.dumps(veri, ensure_ascii=False), iso(simdi + ONAY_OMRU)),
                    )
                    return kimlik
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("işlem kimliği üretilemedi")

    def getir(self, kimlik: str) -> BekleyenIslem | None:
        """Süresi dolmuş veya olmayan işlem için None."""
        r = self.conn.execute("SELECT * FROM tg_islemler WHERE id=?", (kimlik,)).fetchone()
        if r is None:
            return None
        if iso_coz(r["bitis"]) < self.saat():
            self.sil(kimlik)
            return None
        return BekleyenIslem(
            r["id"], r["tur"], r["sohbet_id"], r["kullanici_id"], r["grup_id"], json.loads(r["veri"]), r["bitis"]
        )

    def sil(self, kimlik: str) -> bool:
        """Tek kullanımlık tüketim: True yalnız bu çağrı satırı gerçekten sildiyse."""
        with islem(self.conn):
            return self.conn.execute("DELETE FROM tg_islemler WHERE id=?", (kimlik,)).rowcount == 1
