"""Onay bekleyen işlemler (silme, yetki/grup kaldırma, parti kaldırma).

callback_data yalnız kısa rastgele anahtar taşır; asıl veri burada (SQLite) durur. İşlem tek
kullanımlıktır, sürelidir ve oluşturulduğu sohbet + kullanıcıya bağlıdır.

Süre denetimi (D7): her kayıt oluşturulurken duvar saati + süreç içi monotonic saat birlikte BELLEKTE
tutulur; duvar saati ile monotonic geçen süre toleranstan fazla ayrışırsa (saat geri/ileri atladı) kayıt
geçersizdir (yeniden onay gerekir). Süreç yeniden başlayınca bekleyen tüm kayıtlar silinir: monotonic
başlangıç yeniden kurulamayacağı için geçerliliği belirsizdir.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from ..db import islem
from ..zaman import (
    SURE_GECERLI, Monotonik, Saat, ZamanDamgasi, damga_al, iso, monotonik_al, sure_durumu,
)

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
    def __init__(self, conn: sqlite3.Connection, saat: Saat, monotonic: Monotonik | None = None):
        self.conn = conn
        self.saat = saat
        self._mono = monotonic or monotonik_al(saat)
        self._damgalar: dict[str, ZamanDamgasi] = {}
        conn.executescript(_SEMA)
        with islem(conn):  # süreç yeniden başladı: önceki sürecin bekleyen onayları geçersiz
            conn.execute("DELETE FROM tg_islemler")

    def olustur(self, tur: str, sohbet_id: int, kullanici_id: int, grup_id: int | None, veri: dict) -> str:
        simdi = self.saat()
        self._temizle()
        with islem(self.conn):
            for _ in range(10):
                kimlik = secrets.token_hex(4)
                try:
                    self.conn.execute(
                        "INSERT INTO tg_islemler(id, tur, sohbet_id, kullanici_id, grup_id, veri, bitis)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (kimlik, tur, sohbet_id, kullanici_id, grup_id,
                         json.dumps(veri, ensure_ascii=False), iso(simdi + ONAY_OMRU)),
                    )
                    self._damgalar[kimlik] = damga_al(self.saat, self._mono)
                    return kimlik
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("işlem kimliği üretilemedi")

    def _temizle(self) -> None:
        """Süresi dolmuş / saati değişmiş kayıtları siler (bellek + tablo)."""
        for kimlik in list(self._damgalar):
            if sure_durumu(self._damgalar[kimlik], ONAY_OMRU, self.saat, self._mono) != SURE_GECERLI:
                self.sil(kimlik)
        with islem(self.conn):  # bellekte damgası olmayan (geçerliliği belirsiz) kayıtlar da silinir
            var = list(self._damgalar)
            if var:
                self.conn.execute(
                    f"DELETE FROM tg_islemler WHERE id NOT IN ({','.join('?' * len(var))})", var
                )
            else:
                self.conn.execute("DELETE FROM tg_islemler")

    def getir(self, kimlik: str) -> BekleyenIslem | None:
        """Süresi dolmuş, saati değişmiş veya olmayan işlem için None."""
        return self.getir_ayrintili(kimlik)[0]

    def getir_ayrintili(self, kimlik: str) -> tuple[BekleyenIslem | None, str | None]:
        """(işlem, neden). İşlem yoksa neden: 'yok' | 'doldu' | 'saat_degisti' (geçersiz kayıt silinir)."""
        r = self.conn.execute("SELECT * FROM tg_islemler WHERE id=?", (kimlik,)).fetchone()
        damga = self._damgalar.get(kimlik)
        if r is None:
            self._damgalar.pop(kimlik, None)
            return None, "yok"
        if damga is None:  # bu süreçte oluşturulmamış: geçerliliği belirsiz
            self.sil(kimlik)
            return None, "yok"
        durum = sure_durumu(damga, ONAY_OMRU, self.saat, self._mono)
        if durum != SURE_GECERLI:
            self.sil(kimlik)
            return None, durum
        return BekleyenIslem(
            r["id"], r["tur"], r["sohbet_id"], r["kullanici_id"], r["grup_id"], json.loads(r["veri"]), r["bitis"]
        ), None

    def sil(self, kimlik: str) -> bool:
        """Tek kullanımlık tüketim: True yalnız bu çağrı satırı gerçekten sildiyse."""
        self._damgalar.pop(kimlik, None)
        with islem(self.conn):
            return self.conn.execute("DELETE FROM tg_islemler WHERE id=?", (kimlik,)).rowcount == 1
