import asyncio
import json

import pytest

from dipbot import db, yonetim
from dipbot.takip import TakipServisi
from fakes import SahteGonderici, SahteKaynak, SahteSaat

KURUCU = 1000
G1 = -1001
G2 = -1002


def run(coro):
    return asyncio.run(coro)


def bildirimler(conn, tur=None, hedef=None):
    sql = "SELECT * FROM outbox WHERE 1=1"
    args = []
    if tur:
        sql += " AND tur=?"
        args.append(tur)
    if hedef is not None:
        sql += " AND hedef_sohbet=?"
        args.append(hedef)
    out = []
    for r in conn.execute(sql + " ORDER BY id", args):
        d = dict(r)
        d["veri"] = json.loads(d["veri"])
        out.append(d)
    return out


def takipte(conn, grup):
    return {r[0] for r in conn.execute("SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=?", (grup,))}


@pytest.fixture
def saat():
    return SahteSaat()


@pytest.fixture
def conn(saat):
    c = db.baglan(":memory:")
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G1)
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G2)
    yield c
    c.close()


@pytest.fixture
def src():
    return SahteKaynak()


@pytest.fixture
def servis(conn, saat):
    return TakipServisi(conn, saat)


@pytest.fixture
def gonderici():
    return SahteGonderici()
