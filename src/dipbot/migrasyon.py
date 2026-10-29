"""Eski grup takip kayıtlarını MERKEZİ takip alanına (config.MERKEZ_ID) aktarma: ANALİZ (dry-run), UYGULA, GERİ AL.

GÜVENLİK İLKELERİ
- `analiz` SALT OKUNUR çalışır (veritabanı `mode=ro` açılır; hiçbir şey yazmaz). Uygulama aynı planı kullanır: dry-run = uygulama.
- Eski grup kayıtları ASLA silinmez/değiştirilmez (arşiv ve geçiş kaynağıdır). Uygulama yalnız merkezi alana EKLER.
- Aynı oyuncu/parti birden çok grupta varsa TEKİLLEŞTİRİLİR: en güncel gözlemli kayıt seçilir, farklılıklar RAPORLANIR.
  Eksik (NULL) alanlar başka kayıttan DOLDURULMAZ (eski/bayat değer sahte değişiklik bildirimi üretmesin; ilk gerçek bilgi sessiz yazılır).
- Merkezde zaten var olan kayıt EZİLMEZ (INSERT OR IGNORE). Uygulama tek transaction'dır ve İDEMPOTENTTİR: ikinci çalıştırma
  `sistem_durumu['merkez_migrasyon']` işaretini görüp hiçbir şey yapmaz (zorlansa bile birincil anahtarlar çoğalmayı önler).
- Gerçek uygulama kurucunun açık onayını (`--onayliyorum`), `analiz` çıktısındaki plan özetini (`--plan <özet>`: onaylanan plan = uygulanan plan)
  ve DOĞRULANMIŞ, GÜNCEL bir yedek dosyasını (`--yedek`) gerektirir. Yedek, canlı veritabanıyla AYNI dosya olamaz ve kaynak tabloların
  İÇERİĞİ (özet değeri) canlıyla birebir eşleşmelidir; bayat/yabancı yedek reddedilir.
- Eski grupta teslim edilmemiş bekleyen bildirim varsa uygulama REDDEDİLİR (o değişiklikler hiçbir yere gönderilmeyecek): kurucu bunu
  `--bekleyen-kaybi-kabul` ile açıkça kabul etmelidir.
- Bildirim kopyası oluşmaz: giden kuyruğa (outbox) hiçbir satır eklenmez; eski bekleyen satırlar açılışta iptal edilir.

Kullanım (sunucuda, servis DURDURULMUŞKEN uygula; `dipbot` kullanıcısıyla):
    python -m dipbot.migrasyon analiz  /var/lib/dipbot/dipbot.db            # dry-run (salt okunur)
    python -m dipbot.migrasyon uygula  /var/lib/dipbot/dipbot.db --yedek <yedek.db> --onayliyorum [--aralik 10]
    python -m dipbot.migrasyon geri-al /var/lib/dipbot/dipbot.db --yedek <yedek.db> --onayliyorum   # yalnız MERKEZİ kayıtları kaldırır
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import timedelta
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
    gozlem_yasi: list = field(default_factory=list)  # kaynak başına en eski/en yeni gözlem (ilk merkezi taramada bildirim seli riski)
    sessiz_gun: int | None = None
    sessiz_oyuncu: int = 0
    sessiz_parti: int = 0
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
def plan_olustur(conn: sqlite3.Connection, kaynaklar: list[int] | None = None, sessiz_gun: int | None = None) -> Plan:
    M = config.MERKEZ_ID
    kaynaklar_, beklenmeyen, eksik = kaynaklari_belirle(conn, kaynaklar)
    p = Plan(kaynaklar_, beklenmeyen, eksik, [rapor.alan_ozeti(conn, g) for g in kaynaklar_], rapor.alan_ozeti(conn, M))
    kesim = iso(simdi_utc() - timedelta(days=sessiz_gun)) if sessiz_gun is not None else None
    p.sessiz_gun = sessiz_gun
    merkez_engelli = {r[0] for r in conn.execute("SELECT oyuncu_id FROM engeller WHERE group_id=?", (M,))}
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

    # merkezde zaten var / merkezde ELLE SİLİNMİŞ (engelli) kimlikler aktarılmaz
    adaylar: dict[str, dict] = {}
    for oid in sorted(secilen_oyuncu, key=lambda x: (len(x), x)):
        if oid in merkez_oyuncu:
            p.atlananlar.append({"tur": "oyuncu", "id": oid, "neden": "zaten merkezde (ezilmedi)"})
        elif oid in merkez_engelli:
            p.atlananlar.append({"tur": "oyuncu", "id": oid, "neden": "merkezde engelli (merkezde silinmiş/engellenmiş; yeniden eklenmedi)"})
        else:
            adaylar[oid] = secilen_oyuncu[oid]
    # uid mükerrer (aynı kişi farklı numarayla): TEK kayıt kalır — ELLE EKLENMİŞ olan tercih edilir, bayraklar birleştirilir
    uid_grup: dict[str, list[str]] = {}
    for oid, r in adaylar.items():
        if r["uid"]:
            uid_grup.setdefault(r["uid"], []).append(oid)
    for uid_, grp in sorted(uid_grup.items()):
        mevcut = merkez_uid.get(uid_)
        if mevcut is not None:
            for oid in grp:
                p.atlananlar.append({"tur": "oyuncu", "id": oid, "neden": f"aynı kimlik (uid) merkezde başka numarayla zaten var: {mevcut}"})
                del adaylar[oid]
            continue
        if len(grp) < 2:
            continue
        kazanan = min(grp, key=lambda o: (-adaylar[o]["elle_eklendi"], len(o), o))
        adaylar[kazanan]["elle_eklendi"] = max(adaylar[o]["elle_eklendi"] for o in grp)
        adaylar[kazanan]["xp_onayli"] = min(adaylar[o]["xp_onayli"] for o in grp)
        for oid in grp:
            if oid != kazanan:
                p.atlananlar.append({"tur": "oyuncu", "id": oid, "neden": f"aynı kimlik (uid) başka numarayla aktarıldı: {kazanan}"})
                del adaylar[oid]
    for oid in sorted(adaylar, key=lambda x: (len(x), x)):
        r = adaylar[oid]
        if kesim is not None and r["son_gozlem"] is not None and r["son_gozlem"] < kesim:
            r["baseline_tamam"] = 0  # eski gözlem: ilk merkezi taramada SESSİZ yeniden gözlem (bildirim seli olmasın)
            p.sessiz_oyuncu += 1
        p.oyuncular.append(r)
    p.istatistik["benzersiz_oyuncu"] = len(secilen_oyuncu)
    for g, o in zip(kaynaklar_, p.kaynak_ozetleri):
        gz = [r[0] for r in conn.execute("SELECT son_gozlem FROM takip_oyuncular WHERE group_id=? AND son_gozlem IS NOT NULL", (g,))]
        p.gozlem_yasi.append({"alan": g, "en_eski": min(gz, default=None), "en_yeni": max(gz, default=None)})

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
        if kesim is not None and (yeni["son_dogrulama"] or "") < kesim:
            yeni["baseline_tamam"] = 0  # eski üye listesi: ilk merkezi taramada sessiz yeniden gözlem
            p.sessiz_parti += 1
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
    L += ["", "AGE OF LAST OBSERVATIONS (a long gap means the first central scan may announce many real changes at once)"]
    for g_ in p.gozlem_yasi:
        L.append(f"  {g_['alan']}: oldest {tarih(g_['en_eski']) if g_['en_eski'] else 'none'}, newest {tarih(g_['en_yeni']) if g_['en_yeni'] else 'none'}")
    if p.sessiz_gun is not None:
        L.append(f"  --eski-gozlem-sessiz {p.sessiz_gun}: {p.sessiz_oyuncu} players and {p.sessiz_parti} parties older than that "
                 "will be re-observed SILENTLY (real changes in that gap are NOT announced)")
    L += ["", "RECOMMENDED CENTRAL SETTINGS", f"  scan interval: {p.aralik_onerisi} min ({p.aralik_gerekce})",
          f"OLD PENDING NOTIFICATIONS tied to old areas: {p.bekleyen_eski_bildirim} (will be CANCELLED at start, never sent)", "",
          f"PLAN DIGEST: {plan_ozeti(p)}  (pass it as --plan to 'uygula'; a different plan is refused)",
          "NEXT STEPS (owner approval required): stop dipbot, take a verified backup, then run 'uygula' with --yedek <backup> --onayliyorum "
          "--plan <digest>" + (" --bekleyen-kaybi-kabul" if p.bekleyen_eski_bildirim else "") + "."]
    return "\n".join(L)


# ---------------------------------------------------------------- uygulama
_OYUNCU_KOLON = ("group_id", "oyuncu_id", "ad", "bolge", "vatandaslik_ulkesi", "son_gozlem", "baseline_tamam", "elle_eklendi",
                 "eklendi", "uid", "xp_alti_sayac", "xp_onayli")
_PARTI_KOLON = ("group_id", "parti_id", "ad", "baseline_tamam", "son_dogrulama", "eklendi", "kismi", "toplam_uye", "xp_sirali")
_MERKEZ_TABLOLAR = ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "bulunamadi_sayaclari")


def _ekle(conn, tablo, kolonlar, satir) -> int:
    return conn.execute(
        f"INSERT OR IGNORE INTO {tablo}({','.join(kolonlar)}) VALUES({','.join('?' * len(kolonlar))})",
        tuple(satir.get(k) for k in kolonlar),
    ).rowcount


def plan_ozeti(p: Plan) -> str:
    """Planın içerik özeti: `analiz` çıktısında yazılır, `uygula --plan <özet>` ile ONAYLANAN plan = UYGULANAN plan olur."""
    govde = {k: getattr(p, k) for k in ("oyuncular", "partiler", "parti_uyeleri", "savaslar", "engeller", "sayaclar", "kaynaklar")}
    return hashlib.sha256(json.dumps(govde, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _merkez_toplam(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE group_id=?", (config.MERKEZ_ID,)).fetchone()[0] for t in _MERKEZ_TABLOLAR}


def uygula(conn: sqlite3.Connection, *, kaynaklar: list[int] | None = None, aralik: int | None = None, zorla: bool = False,
           sessiz_gun: int | None = None, plan: str | None = None, bekleyen_kaybi_kabul: bool = False) -> dict:
    """Planı TEK transaction'da uygular. İdempotent. Kaynak kayıtlara DOKUNMAZ. Dönüş: sayılar (veya {'durum':'zaten_uygulanmis'})."""
    if aralik is not None and aralik not in config.ALLOWED_SCAN_INTERVALS_MINUTES:
        raise MigrasyonHatasi(f"aralık şunlardan biri olmalı: {config.ALLOWED_SCAN_INTERVALS_MINUTES}")
    M = config.MERKEZ_ID
    with islem(conn):
        isaret = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (ANAHTAR,)).fetchone()
        if isaret and not zorla:
            return {"durum": "zaten_uygulanmis"}
        if isaret and zorla:
            onceki = json.loads(isaret[0])
            if onceki.get("merkez_toplam") != _merkez_toplam(conn):
                raise MigrasyonHatasi("--zorla reddedildi: merkezi alan geçişten sonra DEĞİŞMİŞ; yeniden çalıştırmak merkezde "
                                      "silinmiş kayıtları geri getirebilir")
        p = plan_olustur(conn, kaynaklar, sessiz_gun)
        if not p.kaynaklar:
            raise MigrasyonHatasi("aktarılacak eski grup kaydı bulunamadı")
        if plan is not None and plan != plan_ozeti(p):
            raise MigrasyonHatasi(f"plan özeti uyuşmuyor (onaylanan {plan}, şimdiki {plan_ozeti(p)}): veri veya seçenekler analizden "
                                  "sonra değişti; analizi yeniden çalıştırıp onaylayın")
        if p.bekleyen_eski_bildirim and not bekleyen_kaybi_kabul:
            raise MigrasyonHatasi(f"eski gruplarda {p.bekleyen_eski_bildirim} teslim edilmemiş bildirim var; geçişte İPTAL edilecekler ve "
                                  "hiçbir yere gönderilmeyecek (bu değişiklikler merkezde yeniden duyurulmaz). Kabul ediyorsanız "
                                  "--bekleyen-kaybi-kabul ekleyin")
        once = _merkez_toplam(conn)
        ayar_vardi = conn.execute("SELECT 1 FROM grup_ayarlari WHERE group_id=?", (M,)).fetchone() is not None
        ayar_once = conn.execute("SELECT tarama_araligi_dk, savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (M,)).fetchone()
        eklenen = {k: [] for k in ("oyuncu", "parti", "savas", "engel", "sayac")}
        sonuc = {"oyuncu": 0, "parti": 0, "parti_uyesi": 0, "savas": 0, "engel": 0, "sayac": 0}
        for r in p.oyuncular:
            if _ekle(conn, "takip_oyuncular", _OYUNCU_KOLON, r):
                sonuc["oyuncu"] += 1
                eklenen["oyuncu"].append(r["oyuncu_id"])
        eklenen_parti = set()
        for r in p.partiler:
            if _ekle(conn, "takip_partiler", _PARTI_KOLON, r):
                sonuc["parti"] += 1
                eklenen["parti"].append(r["parti_id"])
                eklenen_parti.add(r["parti_id"])
        for r in p.parti_uyeleri:
            sonuc["parti_uyesi"] += _ekle(conn, "parti_uyeleri", ("group_id", "parti_id", "oyuncu_id", "ad", "xp"), r)
        for r in p.savaslar:
            if _ekle(conn, "savaslar", ("group_id", "savas_id", "ad", "ilk_gorulme"), r):
                sonuc["savas"] += 1
                eklenen["savas"].append(r["savas_id"])
        for r in p.engeller:
            if _ekle(conn, "engeller", ("group_id", "oyuncu_id", "neden", "olusturuldu"), r):
                sonuc["engel"] += 1
                eklenen["engel"].append(r["oyuncu_id"])
        for r in p.sayaclar:
            if _ekle(conn, "bulunamadi_sayaclari", ("group_id", "tur", "kimlik", "sayac"), r):
                sonuc["sayac"] += 1
                eklenen["sayac"].append([r["tur"], r["kimlik"]])
        # merkezi tarama yapılandırması: kayıt yoksa oluşturulur; mevcut aralık yalnız açıkça istenirse değişir; baseline asla düşmez
        conn.execute("INSERT OR IGNORE INTO grup_ayarlari(group_id, tarama_araligi_dk, savas_baseline_tamam) VALUES(?,?,?)",
                     (M, aralik or p.aralik_onerisi, 1 if p.savas_baseline else 0))
        if aralik is not None:
            conn.execute("UPDATE grup_ayarlari SET tarama_araligi_dk=? WHERE group_id=?", (aralik, M))
        baseline_yukseltildi = False
        if p.savas_baseline and ayar_vardi and not ayar_once["savas_baseline_tamam"]:
            conn.execute("UPDATE grup_ayarlari SET savas_baseline_tamam=1 WHERE group_id=?", (M,))
            baseline_yukseltildi = True
        # doğrulama: HER tablo için eklenen satır sayısı plana eşit olmalı (INSERT OR IGNORE sessizce satır düşürmesin); aksi halde GERİ ALINIR
        planli = {"oyuncu": len(p.oyuncular), "parti": len(p.partiler), "parti_uyesi": len(p.parti_uyeleri), "savas": len(p.savaslar),
                  "engel": len(p.engeller), "sayac": len(p.sayaclar)}
        for k, v in planli.items():
            if sonuc[k] != v:
                raise MigrasyonHatasi(f"doğrulama başarısız ({k}: eklenen {sonuc[k]} != planlanan {v}); hiçbir şey değişmedi")
        sonra = _merkez_toplam(conn)
        for tablo, anahtar in (("takip_oyuncular", "oyuncu"), ("takip_partiler", "parti"), ("parti_uyeleri", "parti_uyesi"),
                               ("savaslar", "savas"), ("engeller", "engel"), ("bulunamadi_sayaclari", "sayac")):
            if sonra[tablo] != once[tablo] + planli[anahtar]:
                raise MigrasyonHatasi(f"doğrulama başarısız ({tablo}: {sonra[tablo]} != {once[tablo]} + {planli[anahtar]}); hiçbir şey değişmedi")
        gercek_aralik = conn.execute("SELECT tarama_araligi_dk FROM grup_ayarlari WHERE group_id=?", (M,)).fetchone()[0]
        kayit = {"zaman": iso(simdi_utc()), "kaynaklar": p.kaynaklar, "eklenen": sonuc, "aralik": gercek_aralik,
                 "onerilen_aralik": p.aralik_onerisi, "aralik_uygulandi": gercek_aralik == (aralik or p.aralik_onerisi),
                 "cakisma_sayisi": len(p.cakismalar), "atlanan_sayisi": len(p.atlananlar), "sessiz_oyuncu": p.sessiz_oyuncu,
                 "sessiz_parti": p.sessiz_parti, "merkez_toplam": sonra}
        # geri-al için: YALNIZ bu migrasyonun eklediği anahtarlar (merkezde önceden var olan hiçbir şey sayılmaz)
        anahtarlar = {"eklenen": eklenen, "ayar_olusturuldu": not ayar_vardi, "baseline_yukseltildi": baseline_yukseltildi}
        conn.execute("INSERT OR REPLACE INTO sistem_durumu(anahtar, deger) VALUES(?,?)",
                     (ANAHTAR, json.dumps({**kayit, "anahtarlar": anahtarlar}, sort_keys=True)))
    return {"durum": "uygulandi", **kayit}


def geri_al(conn: sqlite3.Connection) -> dict:
    """YALNIZ bu migrasyonun EKLEDİĞİ merkezi kayıtları (işarette saklanan anahtarlar) ve işareti kaldırır. İşaret yoksa reddedilir.
    Merkezde önceden var olan veya sonradan eklenen kayıtlar, eski grup kayıtları ve bekleyen bildirimler DOKUNULMAZ."""
    M = config.MERKEZ_ID
    sayilar = {}
    with islem(conn):
        r = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (ANAHTAR,)).fetchone()
        if r is None:
            raise MigrasyonHatasi("migrasyon işareti yok: geri alınacak bir şey yok (merkezi kayıtlara dokunulmadı)")
        k = json.loads(r[0]).get("anahtarlar")
        if k is None:
            raise MigrasyonHatasi("işaret eklenen anahtarları içermiyor (eski sürüm); yedekten geri yükleyin")
        e = k["eklenen"]
        sayilar["takip_oyuncular"] = sum(conn.execute("DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (M, x)).rowcount
                                         for x in e["oyuncu"])
        sayilar["takip_partiler"] = sum(conn.execute("DELETE FROM takip_partiler WHERE group_id=? AND parti_id=?", (M, x)).rowcount
                                        for x in e["parti"])
        sayilar["parti_uyeleri"] = sum(conn.execute("DELETE FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (M, x)).rowcount
                                       for x in e["parti"])
        sayilar["savaslar"] = sum(conn.execute("DELETE FROM savaslar WHERE group_id=? AND savas_id=?", (M, x)).rowcount for x in e["savas"])
        sayilar["engeller"] = sum(conn.execute("DELETE FROM engeller WHERE group_id=? AND oyuncu_id=?", (M, x)).rowcount for x in e["engel"])
        sayilar["bulunamadi_sayaclari"] = sum(
            conn.execute("DELETE FROM bulunamadi_sayaclari WHERE group_id=? AND tur=? AND kimlik=?", (M, t, kim)).rowcount
            for t, kim in e["sayac"])
        if k.get("ayar_olusturuldu"):
            sayilar["grup_ayarlari"] = conn.execute("DELETE FROM grup_ayarlari WHERE group_id=?", (M,)).rowcount
        elif k.get("baseline_yukseltildi"):
            conn.execute("UPDATE grup_ayarlari SET savas_baseline_tamam=0 WHERE group_id=?", (M,))
        conn.execute("DELETE FROM sistem_durumu WHERE anahtar=?", (ANAHTAR,))
    return sayilar


# ---------------------------------------------------------------- yedek doğrulama + CLI
_SAYILACAK = ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "savaslar", "engeller", "bulunamadi_sayaclari", "grup_ayarlari",
              "onayli_gruplar")


def _icerik_ozeti(conn) -> str:
    """Kaynak tabloların İÇERİK özeti (satır sırası rowid'e göre); yedek ile canlı veri birebir aynı mı?"""
    h = hashlib.sha256()
    for t in _SAYILACAK:
        h.update(t.encode())
        for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid"):
            h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def yedegi_dogrula(conn: sqlite3.Connection, yedek: Path, db_yolu: Path | None = None) -> None:
    """Yedek: var, canlı veritabanıyla AYNI DOSYA DEĞİL, bütünlüğü 'ok' ve kaynak tabloların İÇERİĞİ canlıyla birebir aynı
    (satır sayısı değil içerik özeti; bayat/yabancı yedek reddedilir)."""
    if not yedek.is_file():
        raise MigrasyonHatasi(f"yedek dosyası yok: {yedek}")
    if db_yolu is not None and os.path.exists(db_yolu) and os.path.samefile(yedek, db_yolu):
        raise MigrasyonHatasi("yedek dosyası canlı veritabanının KENDİSİ olamaz")
    y = sqlite3.connect(f"file:{yedek.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        if y.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrasyonHatasi("yedek bütünlük kontrolünden geçmedi")
        for t in _SAYILACAK:
            try:
                y.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            except sqlite3.DatabaseError:
                raise MigrasyonHatasi(f"yedekte '{t}' tablosu yok: bu veritabanının yedeği değil") from None
        if _icerik_ozeti(y) != _icerik_ozeti(conn):
            raise MigrasyonHatasi("yedek GÜNCEL değil veya içeriği canlı veritabanıyla AYNI değil; servis durdurulmuşken TAZE yedek alın")
    finally:
        y.close()


def _surum_kontrol(yol: Path) -> None:
    """Canlı DB'ye yazmadan ÖNCE şema sürümü beklenenle aynı mı? (Aksi halde db.baglan şema/veri göçü yapardı.)"""
    from . import db as dbmod

    c = sqlite3.connect(f"file:{yol.resolve()}?mode=ro", uri=True)
    try:
        v = c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()
    if v != dbmod.SEMA_SURUMU:
        raise MigrasyonHatasi(f"veritabanı şema sürümü {v}, beklenen {dbmod.SEMA_SURUMU}: önce güncel kodla servisi bir kez başlatıp "
                              "durdurun (şema göçü), sonra yeniden yedekleyip deneyin")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dipbot.migrasyon")
    ap.add_argument("komut", choices=["analiz", "uygula", "geri-al"])
    ap.add_argument("db")
    ap.add_argument("--kaynak", type=int, action="append", help="aktarılacak eski alan kimliği (tekrarlanabilir; varsayılan: beklenen üç eski grup)")
    ap.add_argument("--yedek", help="doğrulanmış ve güncel SQLite yedek dosyası (uygula/geri-al için zorunlu)")
    ap.add_argument("--onayliyorum", action="store_true", help="kurucunun açık onayı (uygula/geri-al için zorunlu)")
    ap.add_argument("--plan", help="analiz çıktısındaki plan özeti (uygula için zorunlu)")
    ap.add_argument("--aralik", type=int, help="merkezi tarama aralığı (dakika); verilmezse önerilen")
    ap.add_argument("--eski-gozlem-sessiz", type=int, metavar="GUN",
                    help="son gözlemi GUN günden eski oyuncu/parti kayıtları ilk merkezi taramada SESSİZ yeniden gözlenir (bildirim seli önlemi; "
                         "gerçek değişiklikleri gizler: kurucu kararı)")
    ap.add_argument("--bekleyen-kaybi-kabul", action="store_true",
                    help="eski gruplarda teslim edilmemiş bildirimlerin iptal edilip hiçbir yere gönderilmeyeceğini kabul ediyorum")
    ap.add_argument("--zorla", action="store_true", help="işaret olsa da yeniden çalıştır (merkez değişmemişse; mükerrer oluşmaz)")
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
            conn.execute("BEGIN")  # tek tutarlı okuma anlık görüntüsü (servis çalışıyorsa bile)
            p = plan_olustur(conn, a.kaynak, a.eski_gozlem_sessiz)
        finally:
            conn.close()
        if a.json:
            print(json.dumps({**p.__dict__, "plan_ozeti": plan_ozeti(p)}, default=str, indent=1, ensure_ascii=False))
        else:
            print(metin(p))
        return 0
    if not a.onayliyorum or not a.yedek or (a.komut == "uygula" and not a.plan):
        print("ERROR: --yedek <verified backup>, --onayliyorum (and for 'uygula' also --plan <digest from analiz>) are required.", file=sys.stderr)
        return 2
    from . import db as dbmod

    try:
        _surum_kontrol(yol)  # db.baglan'dan ÖNCE: şema/veri göçü yapma riski yok
    except MigrasyonHatasi as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    conn = dbmod.baglan(yol)
    try:
        try:
            yedegi_dogrula(conn, Path(a.yedek), yol)
            if a.komut == "uygula":
                r = uygula(conn, kaynaklar=a.kaynak, aralik=a.aralik, zorla=a.zorla, sessiz_gun=a.eski_gozlem_sessiz, plan=a.plan,
                           bekleyen_kaybi_kabul=a.bekleyen_kaybi_kabul)
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
