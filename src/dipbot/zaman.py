"""Zaman yardımcıları: kayıt UTC (sabit biçimli ISO), gösterim Europe/Istanbul."""
from __future__ import annotations

from datetime import datetime, timezone
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
