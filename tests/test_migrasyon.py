"""Eski grup kayıtlarının MERKEZİ takip alanına aktarımı: analiz (dry-run, salt okunur), uygulama, idempotency, kayıpsızlık.

MOCK/yerel testler: gerçek veritabanı yok. Gerçek migrasyon kurucunun açık onayıyla ayrıca yapılacaktır."""
import hashlib
import json
import sqlite3

import pytest

from conftest import bildirimler, run
from fakes import SahteKaynak, SahteSaat

from dipbot import config, db, migrasyon, rapor
from dipbot.kaynak import SavasOzet
from dipbot.takip import TakipServisi
from dipbot.zaman import iso

A, B, C = -1004485206993, -1004375821471, -5571949121  # üç eski grup (YALNIZ arşiv/kaynak)
M = config.MERKEZ_ID
T0, T1, T2 = "2026-10-01T10:00:00.000000Z", "2026-10-10T10:00:00.000000Z", "2026-10-20T10:00:00.000000Z"
KAYNAK_TABLOLAR = ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "bulunamadi_sayaclari", "grup_ayarlari",
                   "onayli_gruplar")


def _ekle_oyuncu(c, g, oid, ad, bolge, ulke, gozlem, *, elle=0, base=1, uid=None, eklendi=T0, xp_onayli=0):
    c.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, bolge, vatandaslik_ulkesi, son_gozlem, baseline_tamam, elle_eklendi,"
              " eklendi, uid, xp_onayli) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (g, oid, ad, bolge, ulke, gozlem, base, elle, eklendi, uid, xp_onayli))


def _ekle_parti(c, g, pid, ad, dogrulama, uyeler, *, eklendi=T0, kismi=0, toplam=None):
    c.execute("INSERT INTO takip_partiler(group_id, parti_id, ad, baseline_tamam, son_dogrulama, eklendi, kismi, toplam_uye)"
              " VALUES(?,?,?,1,?,?,?,?)", (g, pid, ad, dogrulama, eklendi, kismi, toplam))
    for uid_, uad in uyeler:
        c.execute("INSERT INTO parti_uyeleri(group_id, parti_id, oyuncu_id, ad, xp) VALUES(?,?,?,?,3000000)", (g, pid, uid_, uad))


def _savas(c, g, sid, ad, ilk):
    c.execute("INSERT INTO savaslar(group_id, savas_id, ad, ilk_gorulme) VALUES(?,?,?,?)", (g, sid, ad, ilk))


def _ayar(c, g, dk, baseline=1):
    c.execute("INSERT OR REPLACE INTO grup_ayarlari(group_id, tarama_araligi_dk, savas_baseline_tamam) VALUES(?,?,?)", (g, dk, baseline))


@pytest.fixture
def veri(tmp_path):
    """Üç eski grup: kesişen oyuncular/partiler/savaşlar, çelişen alanlar, farklı aralıklar, engeller, bekleyen eski bildirim."""
    yol = tmp_path / "dipbot.db"
    c = db.baglan(yol)
    for g in (A, B, C):
        c.execute("INSERT INTO onayli_gruplar(group_id, onaylandi, donem) VALUES(?,?,?)", (g, T0, 1))
    # --- A
    _ekle_oyuncu(c, A, "1", "Ali", "Varna", "Bulgaria", T1, elle=1, uid="u-1")
    _ekle_oyuncu(c, A, "2", "Bob", "Sofia", "Bulgaria", T1, uid="u-2")
    _ekle_oyuncu(c, A, "7", "Gul", "Plovdiv", "Bulgaria", T1, uid="u-7")  # uid mükerrer (B'de "8" aynı kişi)
    _ekle_parti(c, A, "5", "TEKNO", T1, [("1", "Ali"), ("2", "Bob")])
    _savas(c, A, "w1", "War One", T0)
    _savas(c, A, "w2", "War Two", T1)
    c.execute("INSERT INTO engeller(group_id, oyuncu_id, neden, olusturuldu) VALUES(?,?,?,?)", (A, "9", "manuel", T0))
    c.execute("INSERT INTO engeller(group_id, oyuncu_id, neden, olusturuldu) VALUES(?,?,?,?)", (A, "3", "manuel", T0))  # B'de takipte
    c.execute("INSERT INTO bulunamadi_sayaclari(group_id, tur, kimlik, sayac) VALUES(?,?,?,?)", (A, "oyuncu", "2", 1))
    _ayar(c, A, 10)
    # --- B (oyuncu 2 daha YENİ gözlem + farklı bölge; parti 5 farklı ad ve daha güncel)
    _ekle_oyuncu(c, B, "2", "Bob", "Varna", "Bulgaria", T2)
    _ekle_oyuncu(c, B, "3", "Cem", "Ruse", "Bulgaria", T1, elle=1)
    _ekle_oyuncu(c, B, "8", "Gul", "Plovdiv", "Bulgaria", T0, uid="u-7")
    _ekle_parti(c, B, "5", "TEKNO-2", T2, [("2", "Bob"), ("3", "Cem")])
    _ekle_parti(c, B, "6", "BETA", T1, [("3", "Cem")])
    _savas(c, B, "w2", "War Two", T0)  # daha erken ilk görülme
    _savas(c, B, "w3", None, T1)
    c.execute("INSERT INTO bulunamadi_sayaclari(group_id, tur, kimlik, sayac) VALUES(?,?,?,?)", (B, "oyuncu", "2", 2))
    _ayar(c, B, 5)
    # --- C
    _ekle_oyuncu(c, C, "1", "Ali", "Varna", "Bulgaria", T0, elle=0)
    _ekle_oyuncu(c, C, "4", "Dan", None, None, None, base=0)
    _ayar(c, C, 15, baseline=0)
    # bekleyen eski bildirimler (A: grup hedefli, B: kanal hedefli ama eski kapsam)
    for g, hedef, k in ((A, A, "o1"), (B, config.LOG_KANAL_ID, "o2")):
        c.execute("INSERT INTO outbox(hedef_sohbet, grup_id, tur, idem_anahtari, veri, sonraki_deneme, olusturuldu, onay_donemi)"
                  " VALUES(?,?,?,?,?,?,?,1)", (hedef, g, "oyuncu_degisti", k, "{}", T0, T0))
    c.commit()
    return yol, c


def _dokum(c, tablolar=KAYNAK_TABLOLAR, hariç_merkez=True):
    out = {}
    for t in tablolar:
        sql = f"SELECT * FROM {t}" + (f" WHERE group_id<>{M}" if hariç_merkez else "") + " ORDER BY 1,2,3"
        out[t] = [tuple(r) for r in c.execute(sql)]
    return out


def _uygula(c, **k):
    """Fixture'daki sahte eski bekleyen bildirimler nedeniyle kayıp kabulü açıkça verilir (kabul edilmezse reddedilir: ayrı test)."""
    k.setdefault("bekleyen_kaybi_kabul", True)
    return migrasyon.uygula(c, **k)


def _cli_uygula(yol, yedek, *ek):
    c = sqlite3.connect(f"file:{yol}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    ozet = migrasyon.plan_ozeti(migrasyon.plan_olustur(c))
    c.close()
    return migrasyon.main(["uygula", str(yol), "--yedek", str(yedek), "--onayliyorum", "--plan", ozet, "--bekleyen-kaybi-kabul", *ek])


def _merkez(c, t):
    return [dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE group_id=?", (M,))]


# ---------------------------------------------------------------- analiz = salt okunur dry-run
def test_analiz_salt_okunur_ve_plan_dogru(veri, capsys):
    yol, c = veri
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    once = hashlib.sha256(yol.read_bytes()).hexdigest()
    assert migrasyon.main(["analiz", str(yol)]) == 0
    out = capsys.readouterr().out
    assert hashlib.sha256(yol.read_bytes()).hexdigest() == once, "analiz veritabanını DEĞİŞTİRMEDİ"
    assert "dry-run; READ-ONLY" in out
    for g in (A, B, C):
        assert str(g) in out
    assert "scan interval: 5 min" in out
    assert "CONFLICTS BETWEEN SOURCES" in out and "player 2" in out and "bolge" in out
    assert "OLD PENDING NOTIFICATIONS tied to old areas: 2" in out
    assert "owner approval required" in out
    assert "BOT_TOKEN" not in out


def test_plan_sayilari_tekillestirme_ve_oneriler(veri):
    yol, c = veri
    p = migrasyon.plan_olustur(c)
    assert p.kaynaklar == [A, B, C] and p.beklenmeyen_alanlar == [] and p.eksik_kaynaklar == []
    assert p.istatistik["benzersiz_oyuncu"] == 6  # {1,2,3,4,7,8}
    ids = {r["oyuncu_id"] for r in p.oyuncular}
    assert ids == {"1", "2", "3", "4", "7"}, "uid mükerrer '8' atlandı (aynı kişi '7')"
    assert any(a["id"] == "8" and "uid" in a["neden"] for a in p.atlananlar)
    assert {r["parti_id"] for r in p.partiler} == {"5", "6"}
    assert {r["savas_id"] for r in p.savaslar} == {"w1", "w2", "w3"}
    assert p.aralik_onerisi == 5 and "farklı aralıklar" in p.aralik_gerekce
    assert p.savas_baseline is True and p.bekleyen_eski_bildirim == 2
    assert any(a["tur"] == "engel" and a["id"] == "3" for a in p.atlananlar), "B'de takipte olan oyuncunun engeli taşınmaz"
    assert [r["oyuncu_id"] for r in p.engeller] == ["9"]


def test_beklenmeyen_eski_alan_raporlanir_ama_varsayilan_aktarilmaz(veri):
    yol, c = veri
    _ekle_oyuncu(c, -777, "50", "Eve", "X", "Y", T1)
    p = migrasyon.plan_olustur(c)
    assert p.beklenmeyen_alanlar == [-777] and -777 not in p.kaynaklar and "50" not in {r["oyuncu_id"] for r in p.oyuncular}
    p2 = migrasyon.plan_olustur(c, [-777])
    assert p2.kaynaklar == [-777] and {r["oyuncu_id"] for r in p2.oyuncular} == {"50"}
    with pytest.raises(migrasyon.MigrasyonHatasi):
        migrasyon.plan_olustur(c, [M])


# ---------------------------------------------------------------- uygulama: kayıpsız, tekilleştirilmiş, kaynak dokunulmaz
def test_uygulama_kayipsiz_tekillestirir_ve_kaynaklara_dokunmaz(veri):
    yol, c = veri
    once = _dokum(c)
    r = _uygula(c)
    assert r["durum"] == "uygulandi" and r["eklenen"]["oyuncu"] == 5 and r["eklenen"]["parti"] == 2
    assert _dokum(c) == once, "eski grup kayıtları AYNEN korundu (silinmedi/değişmedi)"
    oy = {x["oyuncu_id"]: x for x in _merkez(c, "takip_oyuncular")}
    assert set(oy) == {"1", "2", "3", "4", "7"}
    assert oy["2"]["bolge"] == "Varna" and oy["2"]["son_gozlem"] == T2, "en güncel gözlemli kayıt seçildi (B)"
    assert oy["1"]["elle_eklendi"] == 1, "bir kaynakta elle eklenmişse elle eklenmiş kalır"
    assert oy["3"]["elle_eklendi"] == 1 and oy["1"]["eklendi"] == T0
    assert oy["4"]["baseline_tamam"] == 0 and oy["4"]["bolge"] is None, "ilk gözlemi yapılmamış kayıt aynen (sahte değer uydurulmaz)"
    pa = {x["parti_id"]: x for x in _merkez(c, "takip_partiler")}
    assert pa["5"]["ad"] == "TEKNO-2" and pa["5"]["son_dogrulama"] == T2 and pa["6"]["ad"] == "BETA"
    uyeler = {(x["parti_id"], x["oyuncu_id"]) for x in _merkez(c, "parti_uyeleri")}
    assert uyeler == {("5", "2"), ("5", "3"), ("6", "3")}, "üyeler en güncel doğrulanmış kayıttan (B)"
    sv = {x["savas_id"]: x for x in _merkez(c, "savaslar")}
    assert set(sv) == {"w1", "w2", "w3"} and sv["w2"]["ilk_gorulme"] == T0, "tarihsel savaşlar birleşti; en erken ilk görülme"
    assert sv["w3"]["ad"] is None
    assert [x["oyuncu_id"] for x in _merkez(c, "engeller")] == ["9"]
    assert {(x["kimlik"], x["sayac"]) for x in _merkez(c, "bulunamadi_sayaclari")} == {("2", 2)}
    ay = _merkez(c, "grup_ayarlari")[0]
    assert ay["tarama_araligi_dk"] == 5 and ay["savas_baseline_tamam"] == 1 and ay["sonraki_tarama"] is None
    kayit = json.loads(c.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (migrasyon.ANAHTAR,)).fetchone()[0])
    assert kayit["kaynaklar"] == [A, B, C] and kayit["eklenen"]["oyuncu"] == 5


def test_dry_run_ile_uygulama_ayni_sonucu_verir(veri):
    yol, c = veri
    p = migrasyon.plan_olustur(c)
    r = _uygula(c)
    assert r["eklenen"] == {"oyuncu": len(p.oyuncular), "parti": len(p.partiler), "parti_uyesi": len(p.parti_uyeleri),
                            "savas": len(p.savaslar), "engel": len(p.engeller), "sayac": len(p.sayaclar)}


def test_ikinci_calistirma_veri_cogaltmaz_idempotent(veri):
    yol, c = veri
    _uygula(c)
    sayim = lambda: {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in  # noqa: E731
                     ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "bulunamadi_sayaclari", "grup_ayarlari")}
    once, dokum = sayim(), _dokum(c, hariç_merkez=False)
    assert _uygula(c) == {"durum": "zaten_uygulanmis"}
    assert sayim() == once
    r = _uygula(c, zorla=True)  # işaret olsa da zorlansa BİLE mükerrer oluşmaz (birincil anahtar + OR IGNORE)
    assert r["eklenen"] == {"oyuncu": 0, "parti": 0, "parti_uyesi": 0, "savas": 0, "engel": 0, "sayac": 0}
    assert sayim() == once and _dokum(c, hariç_merkez=False) == dokum


def test_merkezde_mevcut_kayit_ezilmez(veri):
    yol, c = veri
    _ekle_oyuncu(c, M, "2", "BobCentral", "Burgas", "Bulgaria", T2, elle=1)
    _ayar(c, M, 20, baseline=0)
    r = _uygula(c)
    oy = {x["oyuncu_id"]: x for x in _merkez(c, "takip_oyuncular")}
    assert oy["2"]["ad"] == "BobCentral" and oy["2"]["bolge"] == "Burgas", "merkezdeki kayıt korunur"
    assert r["eklenen"]["oyuncu"] == 4
    ay = _merkez(c, "grup_ayarlari")[0]
    assert ay["tarama_araligi_dk"] == 20, "mevcut merkezi aralık yalnız --aralik ile değişir"
    assert ay["savas_baseline_tamam"] == 1, "baseline yalnız yükselir"
    _uygula(c, zorla=True, aralik=15)
    assert _merkez(c, "grup_ayarlari")[0]["tarama_araligi_dk"] == 15


def test_hata_halinde_tum_degisiklikler_geri_alinir(veri, monkeypatch):
    yol, c = veri
    cagri = {"n": 0}
    gercek = migrasyon._ekle

    def bozuk(*a, **k):
        cagri["n"] += 1
        if cagri["n"] == 4:
            raise RuntimeError("disk dolu (simülasyon)")
        return gercek(*a, **k)

    monkeypatch.setattr(migrasyon, "_ekle", bozuk)
    with pytest.raises(RuntimeError):
        _uygula(c)
    for t in ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "bulunamadi_sayaclari", "grup_ayarlari"):
        assert _merkez(c, t) == [], t
    assert c.execute("SELECT 1 FROM sistem_durumu WHERE anahtar=?", (migrasyon.ANAHTAR,)).fetchone() is None


def test_gecersiz_aralik_ve_kaynaksiz_calisma_reddedilir(veri):
    yol, c = veri
    with pytest.raises(migrasyon.MigrasyonHatasi):
        _uygula(c, aralik=7)
    c2 = db.baglan(":memory:")
    with pytest.raises(migrasyon.MigrasyonHatasi):
        _uygula(c2)


def test_geri_al_yalniz_merkezi_kayitlari_kaldirir(veri):
    yol, c = veri
    once = _dokum(c)
    _uygula(c)
    r = migrasyon.geri_al(c)
    assert r["takip_oyuncular"] == 5 and r["takip_partiler"] == 2
    assert _dokum(c) == once, "eski grup kayıtları dokunulmamış"
    for t in ("takip_oyuncular", "takip_partiler", "savaslar"):
        assert _merkez(c, t) == []
    assert c.execute("SELECT 1 FROM sistem_durumu WHERE anahtar=?", (migrasyon.ANAHTAR,)).fetchone() is None
    assert _uygula(c)["durum"] == "uygulandi", "geri alma sonrası yeniden uygulanabilir"


# ---------------------------------------------------------------- aktarım sonrası merkezi takip: sessiz baseline, tek bildirim, tekrar yok
def test_aktarim_sonrasi_tarama_degisiklik_bildirimi_tek_kez_ve_tekrar_yok(veri):
    yol, c = veri
    _uygula(c)
    saat = SahteSaat()
    sv = TakipServisi(c, saat)
    src = SahteKaynak()
    src.oyuncu("1", "Ali", "Varna", "Bulgaria")
    src.oyuncu("2", "Bob", "Varna", "Bulgaria")      # kayıtla AYNI -> bildirim yok
    src.oyuncu("3", "Cem", "Sofia", "Bulgaria")      # kayıtta Ruse -> TEK gerçek değişiklik
    src.oyuncu("4", "Dan", "Ruse", "Bulgaria")       # ilk gözlem yapılmamıştı -> sessiz
    src.oyuncu("7", "Gul", "Plovdiv", "Bulgaria")
    src.savaslar = [SavasOzet("w1", "War One"), SavasOzet("w2", "War Two"), SavasOzet("w3", "x")]
    src.parti("5", "TEKNO-2", [("2", "Bob"), ("3", "Cem")])
    src.parti("6", "BETA", [("3", "Cem")])
    for _ in range(2):
        run(sv.partiler_tara(M, src))
        run(sv.oyuncular_tara(M, src))
        run(sv.savaslari_tara(M, src))
    b = [x for x in bildirimler(c) if x["grup_id"] == M]  # (fixture'daki sahte eski bekleyen satırlar hariç)
    assert [x["tur"] for x in b] == ["oyuncu_degisti"] and b[0]["veri"]["oyuncu_id"] == "3"
    assert b[0]["hedef_sohbet"] == config.LOG_KANAL_ID and b[0]["grup_id"] == M
    assert not [x for x in b if x["tur"].startswith("yeni_savas")], "bilinen savaşlar için bildirim seli yok (baseline korundu)"
    # eski gruplar hâlâ taranmaz/dokunulmaz
    assert c.execute("SELECT COUNT(*) FROM tarama_isleri WHERE group_id<>?", (M,)).fetchone()[0] == 0


# ---------------------------------------------------------------- CLI: onay, yedek, güncellik
def _yedek_al(yol, hedef):
    kaynak = sqlite3.connect(yol)
    hedefc = sqlite3.connect(hedef)
    kaynak.backup(hedefc)
    hedefc.close()
    kaynak.close()
    return hedef


def test_cli_uygula_onay_ve_yedek_zorunlu(veri, tmp_path, capsys):
    yol, c = veri
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    assert migrasyon.main(["uygula", str(yol)]) == 2
    assert migrasyon.main(["uygula", str(yol), "--onayliyorum"]) == 2
    assert migrasyon.main(["uygula", str(yol), "--yedek", str(tmp_path / "yok.db"), "--onayliyorum"]) == 2, "--plan zorunlu"
    assert migrasyon.main(["uygula", str(yol), "--yedek", str(tmp_path / "yok.db"), "--onayliyorum", "--plan", "x"]) == 3
    c = db.baglan(yol)
    assert _merkez(c, "takip_oyuncular") == [], "reddedilen çalıştırmalar hiçbir şey yazmadı"
    c.close()
    capsys.readouterr()


def test_cli_bayat_veya_yabanci_yedek_reddedilir_gecerli_yedekle_uygular(veri, tmp_path, capsys):
    yol, c = veri
    yedek = _yedek_al(yol, tmp_path / "yedek.db")
    c.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, eklendi) VALUES(?,?,?,?)", (A, "99", "Yeni", T2))  # yedekten sonra veri
    c.commit()
    c.close()
    assert _cli_uygula(yol, yedek) == 3
    assert "GÜNCEL değil" in capsys.readouterr().err
    yedek2 = _yedek_al(yol, tmp_path / "yedek2.db")
    yabanci = tmp_path / "yabanci.db"
    s = sqlite3.connect(yabanci)
    s.execute("CREATE TABLE x(a)")
    s.commit()
    s.close()
    assert _cli_uygula(yol, yabanci) == 3
    assert _cli_uygula(yol, yedek2, "--aralik", "10") == 0
    c = db.baglan(yol)
    assert len(_merkez(c, "takip_oyuncular")) == 6 and _merkez(c, "grup_ayarlari")[0]["tarama_araligi_dk"] == 10
    c.close()
    # ikinci çalıştırma: veri çoğalmaz
    assert _cli_uygula(yol, _yedek_al(yol, tmp_path / "yedek3.db")) == 0
    c = db.baglan(yol)
    assert len(_merkez(c, "takip_oyuncular")) == 6
    c.close()


def test_cli_geri_al_ve_olmayan_db(veri, tmp_path):
    yol, c = veri
    _uygula(c)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    yedek = _yedek_al(yol, tmp_path / "y.db")
    assert migrasyon.main(["geri-al", str(yol), "--yedek", str(yedek), "--onayliyorum"]) == 0
    c = db.baglan(yol)
    assert _merkez(c, "takip_oyuncular") == [] and c.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0] == 8
    c.close()
    assert migrasyon.main(["analiz", str(tmp_path / "yok.db")]) == 2


def test_rapor_merkez_ozeti_arsiv_ve_migrasyon_durumu(veri):
    yol, c = veri
    o = rapor.merkez_ozeti(c)
    assert o["oyuncu"] == 0 and o["arsiv_alanlari"] == 3 and o["migrasyon_uygulandi"] is False
    _uygula(c)
    o = rapor.merkez_ozeti(c)
    assert o["oyuncu"] == 5 and o["parti"] == 2 and o["savas"] == 3 and o["aralik_dk"] == 5 and o["migrasyon_uygulandi"] is True
    assert o["arsiv_alanlari"] == 3


# ================================================================= Opus incelemesi bulguları (kilitleyen testler)
def test_yedek_canli_veritabaninin_kendisi_olamaz(veri, capsys):
    yol, c = veri
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    assert _cli_uygula(yol, yol) == 3
    assert "KENDİSİ" in capsys.readouterr().err
    c = db.baglan(yol)
    assert _merkez(c, "takip_oyuncular") == []
    c.close()


def test_yedek_satir_sayisi_ayni_ama_icerik_farkliysa_reddedilir(veri, tmp_path, capsys):
    yol, c = veri
    yedek = _yedek_al(yol, tmp_path / "y.db")
    c.execute("UPDATE takip_oyuncular SET bolge='Burgas' WHERE group_id=? AND oyuncu_id='1'", (A,))  # sayılar aynı, içerik farklı
    c.commit()
    c.close()
    assert _cli_uygula(yol, yedek) == 3
    assert "AYNI değil" in capsys.readouterr().err
    c = db.baglan(yol)
    assert _merkez(c, "takip_oyuncular") == []
    c.close()


def test_sema_surumu_beklenenden_farkliysa_baglanmadan_reddedilir(veri, tmp_path, capsys):
    yol, c = veri
    c.execute("PRAGMA user_version=3")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    once = hashlib.sha256(yol.read_bytes()).hexdigest()
    assert migrasyon.main(["uygula", str(yol), "--yedek", str(yol), "--onayliyorum", "--plan", "x"]) == 3
    assert "şema sürümü 3" in capsys.readouterr().err
    assert hashlib.sha256(yol.read_bytes()).hexdigest() == once, "güvenlik kontrolleri geçmeden veritabanına YAZILMADI (db.baglan çağrılmadı)"


def test_plan_ozeti_uyusmazsa_reddedilir_ve_analiz_ozeti_yazar(veri, capsys):
    yol, c = veri
    p = migrasyon.plan_olustur(c)
    ozet = migrasyon.plan_ozeti(p)
    assert len(ozet) == 16 and migrasyon.plan_ozeti(migrasyon.plan_olustur(c)) == ozet, "aynı veri -> aynı özet (deterministik)"
    assert f"PLAN DIGEST: {ozet}" in migrasyon.metin(p)
    with pytest.raises(migrasyon.MigrasyonHatasi, match="plan özeti uyuşmuyor"):
        _uygula(c, plan="0" * 16)
    assert _merkez(c, "takip_oyuncular") == []
    _ekle_oyuncu(c, A, "55", "Yeni", "X", "Y", T2)  # analizden sonra veri değişti
    with pytest.raises(migrasyon.MigrasyonHatasi, match="plan özeti uyuşmuyor"):
        _uygula(c, plan=ozet)
    assert _uygula(c, plan=migrasyon.plan_ozeti(migrasyon.plan_olustur(c)))["durum"] == "uygulandi"


def test_bekleyen_eski_bildirim_varsa_acik_kabul_olmadan_uygulanmaz(veri):
    yol, c = veri
    with pytest.raises(migrasyon.MigrasyonHatasi, match="teslim edilmemiş bildirim"):
        migrasyon.uygula(c)
    assert _merkez(c, "takip_oyuncular") == []
    assert migrasyon.uygula(c, bekleyen_kaybi_kabul=True)["durum"] == "uygulandi"
    # bekleyen satırlar migrasyon tarafından DEĞİŞTİRİLMEZ (açılışta hizalama iptal eder)
    assert c.execute("SELECT COUNT(*) FROM outbox WHERE durum='bekliyor'").fetchone()[0] == 2


def test_merkezde_engelli_oyuncu_geri_eklenmez_ve_zorla_merkez_degismisse_reddedilir(veri):
    yol, c = veri
    c.execute("INSERT INTO engeller(group_id, oyuncu_id, neden, olusturuldu) VALUES(?,?,?,?)", (M, "4", "elle_silindi", T2))
    r = _uygula(c)
    assert "4" not in {x["oyuncu_id"] for x in _merkez(c, "takip_oyuncular")}
    assert r["eklenen"]["oyuncu"] == 4
    p = migrasyon.plan_olustur(c)
    assert any(a["id"] == "4" and "engelli" in a["neden"] for a in p.atlananlar)
    # geçişten sonra merkezde bir oyuncu silinir -> --zorla yeniden eklemesin
    c.execute("DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id='1'", (M,))
    with pytest.raises(migrasyon.MigrasyonHatasi, match="DEĞİŞMİŞ"):
        _uygula(c, zorla=True)
    assert "1" not in {x["oyuncu_id"] for x in _merkez(c, "takip_oyuncular")}


def test_uid_mukerrerinde_elle_eklenmis_kayit_tercih_edilir_ve_bayraklar_birlesir(veri):
    yol, c = veri
    c.execute("DELETE FROM takip_oyuncular WHERE oyuncu_id IN ('7','8')")
    _ekle_oyuncu(c, A, "123", "Eda", "Sofia", "Bulgaria", T1, elle=0, uid="u-dup", xp_onayli=1)
    _ekle_oyuncu(c, B, "0123", "Eda", "Sofia", "Bulgaria", T1, elle=1, uid="u-dup", xp_onayli=0)
    _uygula(c)
    oy = {x["oyuncu_id"]: x for x in _merkez(c, "takip_oyuncular")}
    assert "0123" in oy and "123" not in oy, "elle eklenmiş kayıt kazandı"
    assert oy["0123"]["elle_eklendi"] == 1 and oy["0123"]["xp_onayli"] == 0, "manuel kişi XP kuralıyla otomatik silinmez"
    c2 = db.baglan(":memory:")
    _ekle_oyuncu(c2, A, "123", "Eda", "S", "B", T1, elle=0, uid="u", xp_onayli=1)
    _ekle_oyuncu(c2, B, "0123", "Eda", "S", "B", T1, elle=0, uid="u", xp_onayli=0)
    c2.execute("INSERT INTO onayli_gruplar(group_id, onaylandi, donem) VALUES(?,?,1)", (A, T0))
    p = migrasyon.plan_olustur(c2, [A, B])
    assert [r["oyuncu_id"] for r in p.oyuncular] == ["123"] and p.oyuncular[0]["xp_onayli"] == 0, "xp_onayli en düşük (korumacı)"


def test_geri_al_isaret_olmadan_reddedilir_ve_onceden_var_olan_veya_sonradan_eklenen_merkezi_kayda_dokunmaz(veri):
    yol, c = veri
    _ekle_oyuncu(c, M, "900", "Manuel", "X", "Y", T2, elle=1)
    with pytest.raises(migrasyon.MigrasyonHatasi, match="işareti yok"):
        migrasyon.geri_al(c)
    assert len(_merkez(c, "takip_oyuncular")) == 1, "işaretsiz geri alma hiçbir şey silmedi"
    _ayar(c, M, 20, baseline=0)
    _uygula(c)
    _ekle_oyuncu(c, M, "901", "SonradanEklendi", "X", "Y", T2, elle=1)  # geçişten SONRA eklendi
    r = migrasyon.geri_al(c)
    kalan = {x["oyuncu_id"] for x in _merkez(c, "takip_oyuncular")}
    assert kalan == {"900", "901"}, "yalnız migrasyonun eklediği 5 oyuncu silindi"
    assert r["takip_oyuncular"] == 5
    ay = _merkez(c, "grup_ayarlari")[0]
    assert ay["tarama_araligi_dk"] == 20, "önceden var olan merkezi ayar silinmedi"
    assert ay["savas_baseline_tamam"] == 0, "migrasyonun yükselttiği baseline geri alındı"
    assert _merkez(c, "takip_partiler") == [] and _merkez(c, "savaslar") == []


def test_eski_gozlemler_sessiz_yeniden_gozlenir_secenegi(veri):
    yol, c = veri
    p = migrasyon.plan_olustur(c, sessiz_gun=1)  # T0-T2 (2026-10) gözlemleri bir günden eski kabul edilir
    assert p.sessiz_oyuncu >= 4 and p.sessiz_parti == 2
    oy = {r["oyuncu_id"]: r for r in p.oyuncular}
    assert oy["2"]["baseline_tamam"] == 0 and oy["3"]["baseline_tamam"] == 0
    assert oy["4"]["baseline_tamam"] == 0
    assert all(r["baseline_tamam"] == 0 for r in p.partiler)
    assert [g["alan"] for g in p.gozlem_yasi] == [A, B, C] and p.gozlem_yasi[1]["en_yeni"] == T2
    out = migrasyon.metin(p)
    assert "AGE OF LAST OBSERVATIONS" in out and "re-observed SILENTLY" in out
    # seçenek yokken kayıtlar aynen kalır
    p0 = migrasyon.plan_olustur(c)
    assert {r["oyuncu_id"]: r["baseline_tamam"] for r in p0.oyuncular}["2"] == 1 and p0.sessiz_oyuncu == 0


def test_onerilen_aralik_uygulanmadiysa_gercek_deger_raporlanir(veri):
    yol, c = veri
    _ayar(c, M, 20, baseline=0)  # tick'in oluşturduğu benzeri mevcut merkezi ayar
    r = _uygula(c)
    assert r["aralik"] == 20 and r["onerilen_aralik"] == 5 and r["aralik_uygulandi"] is False
    c2 = db.baglan(":memory:")
    c2.execute("INSERT INTO onayli_gruplar(group_id, onaylandi, donem) VALUES(?,?,1)", (A, T0))
    _ekle_oyuncu(c2, A, "1", "A", "V", "B", T1)
    _ayar(c2, A, 15)
    r2 = migrasyon.uygula(c2, bekleyen_kaybi_kabul=True)
    assert r2["aralik"] == 15 and r2["aralik_uygulandi"] is True


def test_dogrulama_tum_tablolar_icin_eklenen_sayisini_plana_esitler(veri, monkeypatch):
    yol, c = veri
    gercek = migrasyon._ekle

    def ignore_uye(conn, tablo, kolonlar, satir):  # parti_uyeleri satırlarını sessizce düşüren hata simülasyonu
        return 0 if tablo == "parti_uyeleri" else gercek(conn, tablo, kolonlar, satir)

    monkeypatch.setattr(migrasyon, "_ekle", ignore_uye)
    with pytest.raises(migrasyon.MigrasyonHatasi, match="parti_uyesi"):
        _uygula(c)
    assert _merkez(c, "takip_oyuncular") == [] and c.execute("SELECT 1 FROM sistem_durumu WHERE anahtar=?", (migrasyon.ANAHTAR,)).fetchone() is None


def test_analiz_tek_tutarli_anlik_goruntude_calisir_ve_plan_ozeti_yazar(veri, capsys):
    yol, c = veri
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert migrasyon.main(["analiz", str(yol)]) == 0  # servis (başka bağlantı) açıkken de çalışır
    out = capsys.readouterr().out
    assert "PLAN DIGEST:" in out and "--plan <digest>" in out and "--bekleyen-kaybi-kabul" in out
    assert migrasyon.main(["analiz", str(yol), "--json", "--eski-gozlem-sessiz", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["plan_ozeti"]
