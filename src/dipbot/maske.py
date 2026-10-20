"""Sır maskeleme (çekirdek + Telegram katmanı ortak): bot token ve yetki başlığı değerleri.

Kayıtlara (outbox.son_hata) ve günlüklere yazılacak serbest metinlerden geçirilir.
"""
from __future__ import annotations

import re

_TOKEN_DESENI = re.compile(r"(bot)?\d{6,}:[A-Za-z0-9_-]{20,}")
_BOT_URL = re.compile(r"(bot)\d+:[A-Za-z0-9_-]+")
_AUTH_BASLIK = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s,;'\"]+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")


_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")

# Çalışma anında öğrenilen sırlar (ör. TokenDeposu'ndan okunan Diplomacia token'ı): YALNIZ bellekte,
# log/DB/repr'e yazılmaz; yalnız maskeleme için kullanılır.
_CALISMA_SIRLARI: set[str] = set()


def sir_kaydet(deger: str | None) -> None:
    if deger and len(deger) >= 8:
        _CALISMA_SIRLARI.add(deger)


def maskele(metin: str) -> str:
    for s in sorted(_CALISMA_SIRLARI, key=len, reverse=True):
        metin = metin.replace(s, "<GIZLI>")
    metin = _JWT.sub("<GIZLI>", metin)
    metin = _TOKEN_DESENI.sub(lambda m: (m.group(1) or "") + "<GIZLI>", metin)
    metin = _BOT_URL.sub(lambda m: m.group(1) + "<GIZLI>", metin)
    metin = _AUTH_BASLIK.sub(lambda m: m.group(1) + "<GIZLI>", metin)
    return _BEARER.sub(lambda m: m.group(1) + "<GIZLI>", metin)
