"""Saf ayrıştırıcı: /api/wars gövdesi -> SavasKaydi listesi. Ağ yok.

Şema: API-ARASTIRMA.md §7 (kullanıcının gerçek /api/wars örneği; 19 savaş, 16 active + 3 preparing).
Henüz CANLI doğrulama yoktur; sayfalama davranışı ve toplam sayı alanı bilinmiyor.

Kararlar:
- `id` kalıcı savaş kimliğidir (zorunlu, boş olmayan metin). `war_number` AYRI alandır
  (profil bağlantısı numarası); id'den türetilmez, id yerine kullanılmaz. Eksik/yanlış tür -> None.
- status/war_type: yalnız görülen değerler tanınır. Başka değer savaşı DÜŞÜRMEZ; ham değer
  korunur ve `durum_taninmiyor` / `tur_taninmiyor` işaretlenir. 'preparing' aktif listededir.
- power alanları: int >= 0 ise değer (0 meşrudur); anahtar yok/null/bool/metin/negatif -> None.
- Tek öğe bozuksa TÜM liste EksikVeri (sessiz atlama yok). Mükerrer id -> EksikVeri.
- Üst düzeyde sayfalama anahtarı varsa tamlık doğrulanamaz -> tamam=False.
- Savaş sayfası bağlantısı (/wars/war/{war_number}) kullanıcı tarafından doğrulandı; tg katmanı üretir.
- Bayrak URL'leri ve diğer alanlar taşınmaz.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..kaynak import (
    EksikVeri,
    SavasDetay,
    SavasListesi,
    SavasOzet,
    SavasTaraf,
    metin_temizle,
)
from .ayristir import SAYFALAMA_ANAHTARLARI

BILINEN_DURUMLAR = frozenset({"active", "preparing"})
BILINEN_TURLER = frozenset({"standard", "rebellion", "independence"})
SAVAS_SAYFALAMA_ANAHTARLARI = SAYFALAMA_ANAHTARLARI | frozenset(
    {"next_url", "nextUrl", "has_next", "hasNext", "count", "cursor", "offset", "total", "pagination", "meta"}
)


@dataclass(frozen=True)
class SavasKaydi:
    savas_id: str  # API `id` (kalıcı kimlik)
    war_number: int | None  # ayrı alan; id değildir
    durum: str  # ham status
    durum_taninmiyor: bool
    tur: str | None  # ham war_type
    tur_taninmiyor: bool
    attacker_id: str | None  # independence'ta None olabilir
    defender_id: str | None
    attacker_name: str | None
    defender_name: str | None
    attacker_province: str | None
    defender_province: str | None
    war_name: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    created_at: datetime | None
    attacker_power: int | None  # None = veri yok (0 DEĞİL)
    defender_power: int | None
    war_goals: tuple[str, ...] | None
    is_conquest: bool | None


@dataclass(frozen=True)
class SavasKayitListesi:
    kayitlar: tuple[SavasKaydi, ...]
    tamam: bool
    neden: str = ""


# ---- yardımcılar ----------------------------------------------------------
def _metin_veya_none(v) -> str | None:
    return metin_temizle(v) if isinstance(v, str) else None


def _tarih(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        return None  # UTC varsayımı yapma
    return d.astimezone(timezone.utc)


def _guc(d: dict, anahtar: str) -> int | None:
    v = d.get(anahtar)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return v


def _numara(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return v


def _hedefler(v) -> tuple[str, ...] | None:
    if not isinstance(v, list):
        return None
    return tuple(x for x in v if isinstance(x, str))


def _kayit(w, i: int) -> SavasKaydi:
    ne = f"wars[{i}]"
    if not isinstance(w, dict):
        raise EksikVeri(f"{ne}: nesne bekleniyordu")
    sid = w.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise EksikVeri(f"{ne}: 'id' dolu metin olmalı")
    durum = w.get("status")
    if not isinstance(durum, str) or not durum.strip():
        raise EksikVeri(f"{ne}: 'status' dolu metin olmalı")
    durum = durum.strip()
    tur = _metin_veya_none(w.get("war_type"))
    goals = w.get("is_conquest")
    return SavasKaydi(
        savas_id=sid.strip(),
        war_number=_numara(w.get("war_number")),
        durum=durum,
        durum_taninmiyor=durum not in BILINEN_DURUMLAR,
        tur=tur,
        tur_taninmiyor=tur is not None and tur not in BILINEN_TURLER,
        attacker_id=_metin_veya_none(w.get("attacker_id")),
        defender_id=_metin_veya_none(w.get("defender_id")),
        attacker_name=_metin_veya_none(w.get("attacker_name")),
        defender_name=_metin_veya_none(w.get("defender_name")),
        attacker_province=_metin_veya_none(w.get("attacker_province")),
        defender_province=_metin_veya_none(w.get("defender_province")),
        war_name=_metin_veya_none(w.get("war_name")),
        starts_at=_tarih(w.get("starts_at")),
        ends_at=_tarih(w.get("ends_at")),
        created_at=_tarih(w.get("created_at")),
        attacker_power=_guc(w, "attacker_power"),
        defender_power=_guc(w, "defender_power"),
        war_goals=_hedefler(w.get("war_goals")),
        is_conquest=goals if isinstance(goals, bool) else None,
    )


def savaslari_ayristir(govde) -> SavasKayitListesi:
    """`{"wars":[...]}` gövdesi. Bozuk öğe/mükerrer id -> EksikVeri (tüm liste)."""
    if not isinstance(govde, dict):
        raise EksikVeri("savaş yanıtı: nesne bekleniyordu")
    liste = govde.get("wars")
    if not isinstance(liste, list):
        raise EksikVeri("savaş yanıtı: 'wars' dizi olmalı")
    kayitlar: list[SavasKaydi] = []
    gorulen: set[str] = set()
    for i, w in enumerate(liste):
        k = _kayit(w, i)
        if k.savas_id in gorulen:
            raise EksikVeri("savaş yanıtı: mükerrer savaş kimliği")
        gorulen.add(k.savas_id)
        kayitlar.append(k)
    sayfalama = sorted(SAVAS_SAYFALAMA_ANAHTARLARI & set(govde))
    neden = f"sayfalama anahtarı var {sayfalama}: tamlık doğrulanamaz" if sayfalama else ""
    return SavasKayitListesi(tuple(kayitlar), tamam=not neden, neden=neden)


# ---- çekirdek tiplerine dönüştürme -------------------------------------------
def savas_ozete_cevir(k: SavasKaydi) -> SavasOzet:
    return SavasOzet(
        savas_id=k.savas_id,
        ad=k.war_name,
        durum=k.durum,
        durum_taninmiyor=k.durum_taninmiyor,
        tur=k.tur,
        tur_taninmiyor=k.tur_taninmiyor,
        war_number=k.war_number,
        attacker_name=k.attacker_name,
        defender_name=k.defender_name,
        attacker_power=k.attacker_power,
        defender_power=k.defender_power,
    )


def savas_detayina_cevir(k: SavasKaydi) -> SavasDetay:
    """SavasDetay.durum = ham status; taraflar = (saldıran, savunan). power -> SavasTaraf.guc
    (güç = hasar olduğu doğrulanmadı; `hasar` alanına YAZILMAZ)."""
    return SavasDetay(
        savas_id=k.savas_id,
        ad=k.war_name,
        durum=k.durum,
        taraflar=(
            SavasTaraf(ad=k.attacker_name, guc=k.attacker_power),
            SavasTaraf(ad=k.defender_name, guc=k.defender_power),
        ),
        war_number=k.war_number,
        tur=k.tur,
        durum_taninmiyor=k.durum_taninmiyor,
        tur_taninmiyor=k.tur_taninmiyor,
    )


def savas_listesine_cevir(liste: SavasKayitListesi) -> SavasListesi:
    """tamam bayrağı aynen taşınır (tamam=False listeyi çekirdek baseline için kullanmaz)."""
    return SavasListesi(tuple(savas_ozete_cevir(k) for k in liste.kayitlar), liste.tamam)
