"""Eski grup takip kayıtlarını MERKEZİ takip alanına (config.MERKEZ_ID) aktarma: ANALİZ (dry-run), UYGULA, GERİ AL.

GÜVENLİK İLKELERİ
- `analiz` SALT OKUNUR çalışır (veritabanı `mode=ro` açılır; hiçbir şey yazmaz). Uygulama aynı planı kullanır: dry-run = uygulama.
- Eski grup kayıtları ASLA silinmez/değiştirilmez (arşiv ve geçiş kaynağıdır). Uygulama yalnız merkezi alana EKLER.
- Aynı oyuncu/parti birden çok grupta varsa TEKİLLEŞTİRİLİR: en güncel gözlemli kayıt seçilir, farklılıklar RAPORLANIR.
  Eksik (NULL) alanlar başka kayıttan DOLDURULMAZ (eski/bayat değer sahte değişiklik bildirimi üretmesin; ilk gerçek bilgi sessiz yazılır).
- Merkezde zaten var olan kayıt EZİLMEZ (INSERT OR IGNORE). Uygulama tek transaction'dır ve İDEMPOTENTTİR: ikinci çalıştırma
  `sistem_durumu['merkez_migrasyon']` işaretini görüp hiçbir şey yapmaz (zorlansa bile birincil anahtarlar çoğalmayı önler).
- Gerçek uygulama kurucunun açık onayını (`--onayliyorum`) ve DOĞRULANMIŞ, GÜNCEL bir yedek dosyasını (`--yedek`) gerektirir.
- Bildirim kopyası oluşmaz: giden kuyruğa (outbox) hiçbir satır eklenmez; eski bekleyen satırlar açılışta iptal edilir.

Kullanım (sunucuda, servis DURDURULMUŞKEN uygula; `dipbot` kullanıcısıyla):
    python -m dipbot.migrasyon analiz  /var/lib/dipbot/dipbot.db            # dry-run (salt okunur)
    python -m dipbot.migrasyon uygula  /var/lib/dipbot/dipbot.db --yedek <yedek.db> --onayliyorum [--aralik 10]
    python -m dipbot.migrasyon geri-al /var/lib/dipbot/dipbot.db --yedek <yedek.db> --onayliyorum   # yalnız MERKEZİ kayıtları kaldırır
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config, rapor
from .db import islem
from .tg.bicim import tarih
from .zaman import iso, simdi_utc

ANAHTAR = rapor.MIGRASYON_ANAHTARI
# Beklenen eski grup kimlikleri YALNIZ geçiş kaynağı (arşiv) olarak bilinir; aktif yapılandırmada KULLANILMAZ.
BEKLENEN_ESKI_GRUPLAR = (-1004485206993, -1004375821471, -5571949121)


class MigrasyonHatasi(Exception):
    pass


@dataclass
class Plan:
    kaynaklar: list[int]
    beklenmeyen_alanlar: list[int]  # veride var ama kaynak seçilmedi
    eksik_kaynaklar: list[int]  # beklenen/seçilen ama veride yok
    kaynak_ozetleri: list[dict]
    merkez_oncesi: dict
    oyuncular: list[dict] = field(default_factory=list)  # eklenecek satırlar
    partiler: list[dict] = field(default_factory=list)
    parti_uyeleri: list[dict] = field(default_factory=list)
    savaslar: list[dict] = field(default_factory=list)
    engeller: list[dict] = field(default_factory=list)
    sayaclar: list[dict] = field(default_factory=list)
    atlananlar: list[dict] = field(default_factory=list)  # {'tur','id','neden'}
    cakismalar: list[dict] = field(default_factory=list)  # {'tur','id','alanlar':{alan:{kaynak:deger}}}
    aralik_onerisi: int = config.DEFAULT_SCAN_INTERVAL_MINUTES
    aralik_gerekce: str = ""
    savas_baseline: bool = False
    bekleyen_eski_bildirim: int = 0
    istatistik: dict = field(default_factory=dict)


# ---------------------------------------------------------------- yardımcılar
def _satirlar(conn, sql, *a) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, a)]


def kaynaklari_belirle(conn: sqlite3.Connection, secili: list[int] | None = None) -> tuple[list[int], list[int], list[int]]:
    """(kaynaklar, beklenmeyen, eksik). Varsayılan: BEKLENEN_ESKI_GRUPLAR ∩ veride bulunanlar; veride bulunan diğer eski
    alanlar `beklenmeyen` olarak RAPORLANIR ve yalnız açıkça seçilirse (--kaynak) aktarılır."""
    mevcut = rapor.arsiv_alanlari(conn)
    if secili:
        for g in secili:
            if g == config.MERKEZ_ID:
                raise MigrasyonHatasi("merkezi alan kaynak olamaz")
        kaynaklar = [g for g in secili if g in mevcut]
        eksik = [g for g in secili if g not in mevcut]
    else:
        kaynaklar = [g for g in BEKLENEN_ESKI_GRUPLAR if g in mevcut]
        eksik = [g for g in BEKLENEN_ESKI_GRUPLAR if g not in mevcut]
    beklenmeyen = [g for g in mevcut if g not in kaynaklar]
    return kaynaklar, beklenmeyen, eksik


def _farklar(satirlar: list[tuple[int, dict]], alanlar) -> dict:
    out = {}
    for a in alanlar:
        degerler = {g: r.get(a) for g, r in satirlar}
        if len({v for v in degerler.values() if v is not None}) > 1:
            out[a] = degerler
    return out


# ---------------------------------------------------------------- plan (analiz = uygulama)
def plan_olustur(conn: sqlite3.Connection, kaynaklar: list[int] | None = None) -> Plan:
    M = config.MERKEZ_ID
    kaynaklar_, beklenmeyen, eksik = kaynaklari_belirle(conn, kaynaklar)
    p = Plan(kaynaklar_, beklenmeyen, eksik, [rapor.alan_ozeti(conn, g) for g in kaynaklar_], rapor.alan_ozeti(conn, M))
    sira = {g: i for i, g in enumerate(kaynaklar_)}
    merkez_oyuncu = {r[0] for r in conn.execute("SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=?", (M,))}
    merkez_parti = {r[0] for r in conn.execute("SELECT parti_id FROM takip_partiler WHERE group_id=?", (M,))}
    merkez_uid = {r[0]: r[1] for r in conn.execute(
        "SELECT uid, oyuncu_id FROM takip_oyuncular WHERE group_id=? AND uid IS NOT NULL", (M,))}

    # ---- oyuncular
    grup: dict[str, list[tuple[int, dict]]] = {}
    for g in kaynaklar_:
        for r in _satirlar(conn, "SELECT * FROM takip_oyuncular WHERE group_id=? ORDER BY oyuncu_id", g):
            grup.setdefault(r["oyuncu_id"], []).append((g, r))

    def anahtar_oyuncu(x):  # en iyi kayıt: ilk gözlemi tamam + en güncel gözlem; eşitlikte kaynak sırası
        g, r = x
        return (r["baseline_tamam"], r["son_gozlem"] or "", -sira[g])

    secilen_oyuncu: dict[str, dict] = {}
    for oid in sorted(grup, key=lambda x: (len(x), x)):
        rows = grup[oid]
        en, en_r = max(rows, key=anahtar_oyuncu)
        farklar = _farklar(rows, ("ad", "bolge", "vatandaslik_ulkesi", "uid", "baseline_tamam", "elle_eklendi", "son_gozlem"))
        if len(rows) > 1:
            p.istatistik["tekillestirilen_oyuncu"] = p.istatistik.get("tekillestirilen_oyuncu", 0) + 1
        if farklar:
            p.cakismalar.append({"tur": "oyuncu", "id": oid, "kaynaklar": [g for g, _ in rows], "alanlar": farklar,
                                 "secilen_kaynak": en})
        yeni = dict(en_r)
        yeni["group_id"] = M
        yeni["elle_eklendi"] = max(r["elle_eklendi"] for _, r in rows)  # elle eklenen asla otomatik kuralla silinmesin
        yeni["xp_onayli"] = max(r["xp_onayli"] for _, r in rows)
        yeni["eklendi"] = min(r["eklendi"] for _, r in rows)
        if yeni["uid"] is None:
            yeni["uid"] = next((r["uid"] for _, r in rows if r["uid"]), None)
        secilen_oyuncu[oid] = yeni

    # uid mükerrer (aynı kişi farklı numarayla): TEK kayıt kalır
    uid_sahibi: dict[str, str] = dict(merkez_uid)
    for oid in sorted(secilen_oyuncu, key=lambda x: (len(x), x)):
        r = secilen_oyuncu[oid]
        if oid in merkez_oyuncu:
            p.atlananlar.append({"tur": "oyuncu", "id": oid, "neden": "zaten merkezde (ezilmedi)"})
            continue
        if r["uid"] and r["uid"] in uid_sahibi and uid_sahibi[r["uid"]] != oid:
            p.atlananlar.append({"tur": "oyuncu", "id": oid,
                                 "neden": f"aynı kimlik (uid) başka numarayla zaten aktarıldı: {uid_sahibi[r['uid']]}"})
            continue
        if r["uid"]:
            uid_sahibi[r["uid"]] = oid
        p.oyuncular.append(r)
    p.istatistik["benzersiz_oyuncu"] = len(secilen_oyuncu)

    # ---- partiler (+ üyeleri seçilen kaynak kaydından)
    gp: dict[str, list[tuple[int, dict]]] = {}
    for g in kaynaklar_:
        for r in _satirlar(conn, "SELECT * FROM takip_partiler WHERE group_id=? ORDER BY parti_id", g):
            gp.setdefault(r["parti_id"], []).append((g, r))
    for pid in sorted(gp, key=lambda x: (len(x), x)):
        rows = gp[pid]
        uye_say = {g: conn.execute("SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (g, pid)).fetchone()[0]
                   for g, _ in rows}
        en, en_r = max(rows, key=lambda x: (x[1]["baseline_tamam"], x[1]["son_dogrulama"] or "", uye_say[x[0]], -sira[x[0]]))
        farklar = _farklar(rows, ("ad", "kismi", "toplam_uye", "son_dogrulama"))
        if len(set(uye_say.values())) > 1:
            farklar["uye_sayisi"] = dict(uye_say)
        if len(rows) > 1:
            p.istatistik["tekillestirilen_parti"] = p.istatistik.get("tekillestirilen_parti", 0) + 1
        if farklar:
            p.cakismalar.append({"tur": "parti", "id": pid, "kaynaklar": [g for g, _ in rows], "alanlar": farklar,
                                 "secilen_kaynak": en})
        if pid in merkez_parti:
            p.atlananlar.append({"tur": "parti", "id": pid, "neden": "zaten merkezde (ezilmedi)"})
            continue
        yeni = dict(en_r)
        yeni["group_id"] = M
        yeni["eklendi"] = min(r["eklendi"] for _, r in rows)
        p.partiler.append(yeni)
        for u in _satirlar(conn, "SELECT * FROM parti_uyeleri WHERE group_id=? AND parti_id=? ORDER BY oyuncu_id", en, pid):
            u["group_id"] = M
            p.parti_uyeleri.append(u)
    p.istatistik["benzersiz_parti"] = len(gp)

    # ---- savaşlar (tarihsel kayıtlar: BİRLEŞİM; ilk görülme = en erken)
    sv: dict[str, list[tuple[int, dict]]] = {}
    for g in kaynaklar_:
        for r in _satirlar(conn, "SELECT * FROM savaslar WHERE group_id=?", g):
            sv.setdefault(r["savas_id"], []).append((g, r))
    merkez_savas = {r[0] for r in conn.execute("SELECT savas_id FROM savaslar WHERE group_id=?", (M,))}
    for sid in sorted(sv):
        rows = sv[sid]
        farklar = _farklar(rows, ("ad",))
        if farklar:
            p.cakismalar.append({"tur": "savas", "id": sid, "kaynaklar": [g for g, _ in rows], "alanlar": farklar})
        if sid in merkez_savas:
            continue
        ad = next((r["ad"] for _, r in sorted(rows, key=lambda x: sira[x[0]]) if r["ad"]), None)
        p.savaslar.append({"group_id": M, "savas_id": sid, "ad": ad, "ilk_gorulme": min(r["ilk_gorulme"] for _, r in rows)})
    p.istatistik["benzersiz_savas"] = len(sv)

    # ---- engeller (yeniden-ekleme engeli): merkezde takipte olan kimlik için engel taşınmaz
    takipte = merkez_oyuncu | {r["oyuncu_id"] for r in p.oyuncular}
    eg: dict[str, dict] = {}
    for g in kaynaklar_:
        for r in _satirlar(conn, "SELECT * FROM engeller WHERE group_id=? ORDER BY oyuncu_id", g):
            if r["oyuncu_id"] in takipte:
                p.atlananlar.append({"tur": "engel", "id": r["oyuncu_id"], "neden": "oyuncu başka kayıtta takipte (engel taşınmadı)"})
                continue
            eg.setdefault(r["oyuncu_id"], {**r, "group_id": M})
    merkez_engel = {r[0] for r in conn.execute("SELECT oyuncu_id FROM engeller WHERE group_id=?", (M,))}
    p.engeller = [v for k, v in sorted(eg.items()) if k not in merkez_engel]

    # ---- 404 sayaçları (max)
    sy: dict[tuple, int] = {}
    for g in kaynaklar_:
        for r in _satirlar(conn, "SELECT * FROM bulunamadi_sayaclari WHERE group_id=?", g):
            k = (r["tur"], r["kimlik"])
            sy[k] = max(sy.get(k, 0), r["sayac"])
    merkez_sayac = {(r[0], r[1]) for r in conn.execute("SELECT tur, kimlik FROM bulunamadi_sayaclari WHERE group_id=?", (M,))}
    p.sayaclar = [{"group_id": M, "tur": t, "kimlik": k, "sayac": v} for (t, k), v in sorted(sy.items())
                  if (t, k) not in merkez_sayac]

    # ---- ayarlar
    araliklar = {g: o["aralik_dk"] for g, o in zip(kaynaklar_, p.kaynak_ozetleri) if o["aralik_dk"] is not None}
    benzersiz = sorted(set(araliklar.values()))
    if not benzersiz:
        p.aralik_onerisi = config.DEFAULT_SCAN_INTERVAL_MINUTES
        p.aralik_gerekce = "kaynak gruplarda aralık kaydı yok: varsayılan"
    elif len(benzersiz) == 1:
        p.aralik_onerisi = benzersiz[0]
        p.aralik_gerekce = "tüm kaynak gruplar aynı aralığı kullanıyor"
    else:
        p.aralik_onerisi = benzersiz[0]
        p.aralik_gerekce = ("kaynak gruplarda farklı aralıklar var " + ", ".join(f"{g}: {v} dk" for g, v in araliklar.items()) +
                            "; en KISA seçildi (en güncel tespit; istek yükü tek merkezi tarama için aynı kalır)")
    p.savas_baseline = any(o["savas_baseline"] for o in p.kaynak_ozetleri)
    p.bekleyen_eski_bildirim = sum(
        conn.execute("SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", (g,)).fetchone()[0] for g in rapor.arsiv_alanlari(conn))
    p.istatistik["eklenecek"] = {"oyuncu": len(p.oyuncular), "parti": len(p.partiler), "parti_uyesi": len(p.parti_uyeleri),
                                 "savas": len(p.savaslar), "engel": len(p.engeller), "sayac": len(p.sayaclar)}
    return p


# ---------------------------------------------------------------- metin raporu
_ETIKET = {"oyuncu": "player", "parti": "party", "savas": "war", "engel": "block"}


def metin(p: Plan) -> str:
    L = ["MIGRATION ANALYSIS (dry-run; READ-ONLY — nothing was changed, merged or deleted)", ""]
    L.append(f"Central tracking area id: {config.MERKEZ_ID} (not a Telegram id); log channel: {config.LOG_KANAL_ID}")
    L.append(f"Source (old group) areas: {p.kaynaklar or 'none found'}")
    if p.eksik_kaynaklar:
        L.append(f"  expected but NOT found in the database: {p.eksik_kaynaklar}")
    if p.beklenmeyen_alanlar:
        L.append(f"  other old areas found and NOT included (use --kaynak to include): {p.beklenmeyen_alanlar}")
    L += ["", "PER SOURCE"]
    for o in p.kaynak_ozetleri:
        L.append(f"  {o['alan_id']}: players {o['oyuncu']} (manual {o['oyuncu_elle']}), parties {o['parti']} "
                 f"(members {o['parti_uyesi']}), wars {o['savas']}, blocks {o['engel']}, interval {o['aralik_dk']} min, "
                 f"war baseline {'done' if o['savas_baseline'] else 'no'}, last scan "
                 f"{tarih(o['son_tarama']) if o['son_tarama'] else 'none'}, pending notifications {o['bekleyen_bildirim']}")
    m = p.merkez_oncesi
    L += ["", f"CENTRAL BEFORE: players {m['oyuncu']}, parties {m['parti']}, wars {m['savas']}, blocks {m['engel']}"]
    e = p.istatistik.get("eklenecek", {})
    L += ["", "RESULT IF APPLIED (unique records; existing central rows are never overwritten)",
          f"  unique players across sources: {p.istatistik.get('benzersiz_oyuncu', 0)} "
          f"(deduplicated: {p.istatistik.get('tekillestirilen_oyuncu', 0)}) -> to add: {e.get('oyuncu', 0)}",
          f"  unique parties across sources: {p.istatistik.get('benzersiz_parti', 0)} "
          f"(deduplicated: {p.istatistik.get('tekillestirilen_parti', 0)}) -> to add: {e.get('parti', 0)} "
          f"(+ {e.get('parti_uyesi', 0)} stored party members from the freshest source record)",
          f"  unique wars (history preserved, earliest first-seen kept): {p.istatistik.get('benzersiz_savas', 0)} -> to add: {e.get('savas', 0)}",
          f"  blocks to add: {e.get('engel', 0)}; not-found counters to add: {e.get('sayac', 0)}",
          f"  war baseline for the central area: {'already done (no first-scan war notification flood)' if p.savas_baseline else 'not done (first war scan will be silent)'}"]
    if p.atlananlar:
        L += ["", f"SKIPPED ({len(p.atlananlar)})"]
        for a in p.atlananlar[:200]:
            L.append(f"  {_ETIKET.get(a['tur'], a['tur'])} {a['id']}: {a['neden']}")
    L += ["", f"CONFLICTS BETWEEN SOURCES ({len(p.cakismalar)}; ids and field values only)"]
    if not p.cakismalar:
        L.append("  none")
    for c in p.cakismalar[:300]:
        L.append(f"  {_ETIKET.get(c['tur'], c['tur'])} {c['id']} in {c['kaynaklar']}" + (f"; kept record from {c['secilen_kaynak']}" if 'secilen_kaynak' in c else ""))
        for alan, vals in c["alanlar"].items():
            L.append(f"      {alan}: " + "; ".join(f"{g}={v!r}" for g, v in vals.items()))
    L += ["", "RECOMMENDED CENTRAL SETTINGS", f"  scan interval: {p.aralik_onerisi} min ({p.aralik_gerekce})",
          f"OLD PENDING NOTIFICATIONS tied to old areas: {p.bekleyen_eski_bildirim} (will be CANCELLED at start, never sent)", "",
          "NEXT STEPS (owner approval required): stop dipbot, take a verified backup, then run 'uygula' with --yedek <backup> --onayliyorum."]
    return "\n".join(L)


# ---------------------------------------------------------------- uygulama
_OYUNCU_KOLON = ("group_id", "oyuncu_id", "ad", "bolge", "vatandaslik_ulkesi", "son_gozlem", "baseline_tamam", "elle_eklendi",
                 "eklendi", "uid", "xp_alti_sayac", "xp_onayli")
_PARTI_KOLON = ("group_id", "parti_id", "ad", "baseline_tamam", "son_dogrulama", "eklendi", "kismi", "toplam_uye", "xp_sirali")


def _ekle(conn, tablo, kolonlar, satir) -> int:
    return conn.execute(
        f"INSERT OR IGNORE INTO {tablo}({','.join(kolonlar)}) VALUES({','.join('?' * len(kolonlar))})",
        tuple(satir.get(k) for k in kolonlar),
    ).rowcount


def uygula(conn: sqlite3.Connection, *, kaynaklar: list[int] | None = None, aralik: int | None = None, zorla: bool = False) -> dict:
    """Planı TEK transaction'da uygular. İdempotent. Kaynak kayıtlara DOKUNMAZ. Dönüş: sayılar (veya {'durum':'zaten_uygulanmis'})."""
    if aralik is not None and aralik not in config.ALLOWED_SCAN_INTERVALS_MINUTES:
        raise MigrasyonHatasi(f"aralık şunlardan biri olmalı: {config.ALLOWED_SCAN_INTERVALS_MINUTES}")
    M = config.MERKEZ_ID
    with islem(conn):
        if conn.execute("SELECT 1 FROM sistem_durumu WHERE anahtar=?", (ANAHTAR,)).fetchone() and not zorla:
            return {"durum": "zaten_uygulanmis"}
        p = plan_olustur(conn, kaynaklar)
        if not p.kaynaklar:
            raise MigrasyonHatasi("aktarılacak eski grup kaydı bulunamadı")
        once = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE group_id=?", (M,)).fetchone()[0]
                for t in ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "bulunamadi_sayaclari")}
        sonuc = {"oyuncu": 0, "parti": 0, "parti_uyesi": 0, "savas": 0, "engel": 0, "sayac": 0}
        for r in p.oyuncular:
            sonuc["oyuncu"] += _ekle(conn, "takip_oyuncular", _OYUNCU_KOLON, r)
        for r in p.partiler:
            sonuc["parti"] += _ekle(conn, "takip_partiler", _PARTI_KOLON, r)
        for r in p.parti_uyeleri:
            sonuc["parti_uyesi"] += _ekle(conn, "parti_uyeleri", ("group_id", "parti_id", "oyuncu_id", "ad", "xp"), r)
        for r in p.savaslar:
            sonuc["savas"] += _ekle(conn, "savaslar", ("group_id", "savas_id", "ad", "ilk_gorulme"), r)
        for r in p.engeller:
            sonuc["engel"] += _ekle(conn, "engeller", ("group_id", "oyuncu_id", "neden", "olusturuldu"), r)
        for r in p.sayaclar:
            sonuc["sayac"] += _ekle(conn, "bulunamadi_sayaclari", ("group_id", "tur", "kimlik", "sayac"), r)
        # merkezi tarama yapılandırması: kayıt yoksa oluşturulur; mevcut aralık yalnız açıkça istenirse değişir; baseline asla düşmez
        conn.execute("INSERT OR IGNORE INTO grup_ayarlari(group_id, tarama_araligi_dk, savas_baseline_tamam) VALUES(?,?,?)",
                     (M, aralik or p.aralik_onerisi, 1 if p.savas_baseline else 0))
        if aralik is not None:
            conn.execute("UPDATE grup_ayarlari SET tarama_araligi_dk=? WHERE group_id=?", (aralik, M))
        if p.savas_baseline:
            conn.execute("UPDATE grup_ayarlari SET savas_baseline_tamam=1 WHERE group_id=?", (M,))
        # doğrulama: eklenen satır sayıları plana eşit olmalı (aksi halde transaction GERİ ALINIR)
        for tablo, anahtar, beklenen in (("takip_oyuncular", "oyuncu", len(p.oyuncular)), ("takip_partiler", "parti", len(p.partiler)),
                                         ("savaslar", "savas", len(p.savaslar))):
            simdi = conn.execute(f"SELECT COUNT(*) FROM {tablo} WHERE group_id=?", (M,)).fetchone()[0]
            if simdi != once[tablo] + beklenen:
                raise MigrasyonHatasi(f"doğrulama başarısız ({tablo}: {simdi} != {once[tablo]} + {beklenen}); hiçbir şey değişmedi")
        kayit = {"zaman": iso(simdi_utc()), "kaynaklar": p.kaynaklar, "eklenen": sonuc, "aralik": aralik or p.aralik_onerisi,
                 "cakisma_sayisi": len(p.cakismalar), "atlanan_sayisi": len(p.atlananlar)}
        conn.execute("INSERT OR REPLACE INTO sistem_durumu(anahtar, deger) VALUES(?,?)", (ANAHTAR, json.dumps(kayit, sort_keys=True)))
    return {"durum": "uygulandi", **kayit}


def geri_al(conn: sqlite3.Connection) -> dict:
    """YALNIZ merkezi alan kayıtlarını (ve migrasyon işaretini) kaldırır; eski grup kayıtlarına dokunmaz (hiç değişmemişlerdir).
    Merkezde geçişten SONRA yapılan değişiklikler de kaybolur: önce yedekten geri yüklemeyi (restore) tercih edin."""
    M = config.MERKEZ_ID
    sayilar = {}
    with islem(conn):
        for t in ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "bulunamadi_sayaclari", "tarama_isleri", "grup_ayarlari"):
            sayilar[t] = conn.execute(f"DELETE FROM {t} WHERE group_id=?", (M,)).rowcount
        sayilar["merkezi_bekleyen_bildirim_iptal"] = conn.execute(
            "UPDATE outbox SET durum='iptal', son_hata='merkezi migrasyon geri alindi' WHERE grup_id=? AND durum='bekliyor'", (M,)).rowcount
        conn.execute("DELETE FROM sistem_durumu WHERE anahtar=?", (ANAHTAR,))
    return sayilar


# ---------------------------------------------------------------- yedek doğrulama + CLI
_SAYILACAK = ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "grup_ayarlari", "onayli_gruplar")


def yedegi_dogrula(conn: sqlite3.Connection, yedek: Path) -> None:
    """Yedek var, bütünlüğü 'ok' ve kaynak tabloların satır sayıları CANLI veritabanıyla AYNI olmalı (bayat yedek reddedilir)."""
    if not yedek.is_file():
        raise MigrasyonHatasi(f"yedek dosyası yok: {yedek}")
    y = sqlite3.connect(f"file:{yedek.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        if y.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrasyonHatasi("yedek bütünlük kontrolünden geçmedi")
        for t in _SAYILACAK:
            try:
                a = y.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.DatabaseError:
                raise MigrasyonHatasi(f"yedekte '{t}' tablosu yok: bu veritabanının yedeği değil") from None
            b = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if a != b:
                raise MigrasyonHatasi(f"yedek GÜNCEL değil ({t}: yedekte {a}, canlıda {b}); yeni yedek alın")
    finally:
        y.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dipbot.migrasyon")
    ap.add_argument("komut", choices=["analiz", "uygula", "geri-al"])
    ap.add_argument("db")
    ap.add_argument("--kaynak", type=int, action="append", help="aktarılacak eski alan kimliği (tekrarlanabilir; varsayılan: beklenen üç eski grup)")
    ap.add_argument("--yedek", help="doğrulanmış ve güncel SQLite yedek dosyası (uygula/geri-al için zorunlu)")
    ap.add_argument("--onayliyorum", action="store_true", help="kurucunun açık onayı (uygula/geri-al için zorunlu)")
    ap.add_argument("--aralik", type=int, help="merkezi tarama aralığı (dakika); verilmezse önerilen")
    ap.add_argument("--zorla", action="store_true", help="işaret olsa da yeniden çalıştır (yine de mükerrer kayıt oluşmaz)")
    ap.add_argument("--json", action="store_true", help="analiz çıktısını JSON ver")
    a = ap.parse_args(argv)
    yol = Path(a.db)
    if not yol.is_file():
        print(f"ERROR: database not found: {yol}", file=sys.stderr)
        return 2
    if a.komut == "analiz":
        conn = sqlite3.connect(f"file:{yol.resolve()}?mode=ro", uri=True)  # SALT OKUNUR
        conn.row_factory = sqlite3.Row
        try:
            p = plan_olustur(conn, a.kaynak)
        finally:
            conn.close()
        print(json.dumps(p.__dict__, default=str, indent=1, ensure_ascii=False) if a.json else metin(p))
        return 0
    if not a.onayliyorum or not a.yedek:
        print("ERROR: --yedek <verified backup> and --onayliyorum are required (owner approval).", file=sys.stderr)
        return 2
    from . import db as dbmod

    conn = dbmod.baglan(yol)
    try:
        try:
            yedegi_dogrula(conn, Path(a.yedek))
            if a.komut == "uygula":
                r = uygula(conn, kaynaklar=a.kaynak, aralik=a.aralik, zorla=a.zorla)
            else:
                r = geri_al(conn)
        except MigrasyonHatasi as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 3
        print(json.dumps(r, indent=1, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
