"""Öncelik 3: UUID kalıcılığı + kimlik uyuşmazlığı + db migration. MOCK."""
import logging
import sqlite3

from conftest import G1, KURUCU, bildirimler, run

from dipbot import db
from dipbot.kaynak import OyuncuAnlik
from dipbot.zamanlayici import TaramaZamanlayici

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def uid_db(conn, oid="p1", g=G1):
    return conn.execute("SELECT uid FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)).fetchone()[0]


def test_oyuncuanlik_uid_opsiyonel_geriye_uyumlu():
    assert OyuncuAnlik("1", "a", "b", "c").uid is None
    assert OyuncuAnlik("1", "a", "b", "c", "u").uid == "u"


def test_ilk_gozlemde_uid_kaydedilir_ve_ayni_uid_sorun_degil(conn, src, servis):
    src.oyuncular["p1"] = OyuncuAnlik("p1", "Ayse", "Roma", "U", U1)
    run(servis.oyuncu_ekle(G1, "p1", src))
    assert uid_db(conn) == U1
    s = run(servis.oyuncular_tara(G1, src))
    assert s.ok and uid_db(conn) == U1


def test_uid_none_gelirse_eski_uid_korunur(conn, src, servis):
    src.oyuncular["p1"] = OyuncuAnlik("p1", "Ayse", "Roma", "U", U1)
    run(servis.oyuncu_ekle(G1, "p1", src))
    src.oyuncular["p1"] = OyuncuAnlik("p1", "Ayse", "Roma", "U", None)
    assert run(servis.oyuncular_tara(G1, src)).ok and uid_db(conn) == U1


def test_uid_ilk_kez_sonradan_gelirse_kaydedilir(conn, src, servis):
    src.oyuncular["p1"] = OyuncuAnlik("p1", "Ayse", "Roma", "U")
    run(servis.oyuncu_ekle(G1, "p1", src))
    assert uid_db(conn) is None
    src.oyuncular["p1"] = OyuncuAnlik("p1", "Ayse", "Roma", "U", U1)
    run(servis.oyuncular_tara(G1, src))
    assert uid_db(conn) == U1


def test_farkli_uid_basarisiz_tarama_kayit_ezilmez_bildirim_yok(conn, src, servis, caplog):
    src.oyuncular["p1"] = OyuncuAnlik("p1", "Ayse", "Roma", "U", U1)
    run(servis.oyuncu_ekle(G1, "p1", src))
    src.oyuncular["p1"] = OyuncuAnlik("p1", "BASKA", "Paris", "V", U2)
    caplog.set_level(logging.WARNING)
    s = run(servis.oyuncular_tara(G1, src))
    assert not s.ok and "uyuş" in s.hatalar[0]
    r = conn.execute("SELECT * FROM takip_oyuncular WHERE oyuncu_id='p1' AND group_id=?", (G1,)).fetchone()
    assert (r["uid"], r["ad"], r["bolge"], r["vatandaslik_ulkesi"]) == (U1, "Ayse", "Roma", "U")
    assert bildirimler(conn, "oyuncu_degisti") == []
    assert any("uyuşmazlığı" in x.getMessage() for x in caplog.records)
    # tarama_isleri başarısız kaydedilir (zamanlayıcı üzerinden)
    z = TaramaZamanlayici(conn, src, KURUCU, servis.saat)
    run(z.tick_ve_bekle())
    r = conn.execute("SELECT * FROM tarama_isleri WHERE group_id=? AND is_tipi='oyuncular'", (G1,)).fetchone()
    assert r["son_basari"] is None and r["son_hata"]


def test_parti_uyesi_uid_ile_otomatik_eklenir(conn, src, servis):
    src.oyuncular["p9"] = OyuncuAnlik("p9", "Z", "Roma", "U", U1)
    src.parti("t1", "Parti", [("p9", "Z")])
    run(servis.parti_ekle(G1, "t1", src))
    run(servis.oyuncular_tara(G1, src))  # C2: uid ilk taramada okunur
    assert uid_db(conn, "p9") == U1


# ---- migration ----
def test_eski_db_migration_uid_sutunu_eklenir_veri_korunur(tmp_path):
    yol = tmp_path / "eski.db"
    c = sqlite3.connect(yol)
    c.executescript(
        """CREATE TABLE takip_oyuncular(group_id INTEGER NOT NULL, oyuncu_id TEXT NOT NULL, ad TEXT, bolge TEXT,
           vatandaslik_ulkesi TEXT, son_gozlem TEXT, baseline_tamam INTEGER NOT NULL DEFAULT 0,
           elle_eklendi INTEGER NOT NULL DEFAULT 0, eklendi TEXT NOT NULL, PRIMARY KEY(group_id, oyuncu_id));
           INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi) VALUES(-1,'7','Eski','t');
           PRAGMA user_version=1;"""
    )
    c.close()
    conn = db.baglan(yol)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SEMA_SURUMU >= 2
    r = conn.execute("SELECT ad, uid FROM takip_oyuncular").fetchone()
    assert (r["ad"], r["uid"]) == ("Eski", None)
    conn.close()
    conn = db.baglan(yol)  # ikinci açılış zararsız (idempotent)
    assert conn.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 1
    conn.close()


def test_yeni_db_uid_sutunu_var(conn):
    assert "uid" in [r[1] for r in conn.execute("PRAGMA table_info(takip_oyuncular)")]


def test_adaptor_oyuncu_uid_doldurur(tmp_path):
    from test_diplomacia_istemci import Sahte, kur, tam_yanitlar, FX
    r = run(kur(tmp_path, Sahte(tam_yanitlar())).fetch_player("72400"))
    assert r.uid == FX["player_response"]["player"]["id"].lower()
