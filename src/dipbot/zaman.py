"""Zaman yardımcıları: kayıt UTC (sabit biçimli ISO), gösterim Europe/Istanbul."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import config

Saat = Callable[[], datetime]

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def simdi_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """UTC, sabit genişlikli; metin olarak sıralanabilir."""
    if dt.tzinfo is None:
        raise ValueError("zaman dilimsiz datetime kabul edilmez")
    return dt.astimezone(timezone.utc).strftime(_FMT)


def iso_coz(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


def yerel_goster(dt: datetime) -> str:
    """Kullanıcıya gösterilecek biçim: 19.10.2026 15:20 (Europe/Istanbul)."""
    return dt.astimezone(ZoneInfo(config.DISPLAY_TIMEZONE)).strftime("%d.%m.%Y %H:%M")


# ----------------------------------------------------------------------------- D7: saat güvenliği
# Süreli onay/bekleme (TTL) kararları yalnız duvar saatine güvenmez: başlangıçta duvar saati ile
# süreç içi monotonic saat birlikte kaydedilir. İkisinin geçen süresi birbirinden toleransın ötesinde
# ayrışırsa (duvar saati geri/ileri atladı) kayıt GEÇERSİZ sayılır; yeniden onay istenir.
# Monotonic değerler yalnız bellekte tutulur: süreç yeniden başlayınca bekleyen kayıtlar geçersizdir.
# Bilinen sınır: Linux CLOCK_MONOTONIC askıya alma (suspend) süresini saymaz; sunucu VPS'tir, askıya alınmaz.
Monotonik = Callable[[], float]

SURE_GECERLI = "gecerli"
SURE_DOLDU = "doldu"
SURE_SAAT_DEGISTI = "saat_degisti"


def monotonik_al(saat: Saat) -> Monotonik:
    """Saat nesnesi `monotonic` özelliği sağlıyorsa (test saatleri) onu, yoksa time.monotonic'i döner."""
    return getattr(saat, "monotonic", None) or time.monotonic


@dataclass(frozen=True)
class ZamanDamgasi:
    duvar: datetime
    mono: float


def damga_al(saat: Saat, mono: Monotonik | None = None) -> ZamanDamgasi:
    return ZamanDamgasi(saat(), (mono or monotonik_al(saat))())


def sure_durumu(
    baslangic: ZamanDamgasi,
    omur: timedelta,
    saat: Saat,
    mono: Monotonik | None = None,
    tolerans: float | None = None,
) -> str:
    """SURE_GECERLI | SURE_DOLDU | SURE_SAAT_DEGISTI. Saat tutarsızlığı süre dolmasından ÖNCE denetlenir."""
    tolerans = config.SAAT_TUTARSIZLIK_TOLERANSI_SANIYE if tolerans is None else tolerans
    gecen_mono = (mono or monotonik_al(saat))() - baslangic.mono
    gecen_duvar = (saat() - baslangic.duvar).total_seconds()
    if gecen_mono < 0 or abs(gecen_duvar - gecen_mono) > tolerans:
        return SURE_SAAT_DEGISTI
    if gecen_mono > omur.total_seconds():
        return SURE_DOLDU
    return SURE_GECERLI
