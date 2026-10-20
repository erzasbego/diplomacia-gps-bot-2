"""Günlükleme kurulumu + sır maskeleme.

python-telegram-bot ve httpx, istek URL'sini (https://api.telegram.org/bot<TOKEN>/...) loglayabilir.
Önlem: (1) httpx/httpcore günlükleri WARNING'e çekilir, (2) tüm işleyicilere, hem biçimlenmiş
mesajı hem de istisna izini maskeleyen bir süzgeç eklenir.
"""
from __future__ import annotations

import logging
import sys
import traceback

from .gonderici import maskele


class SirFiltresi(logging.Filter):
    def __init__(self, *sirlar: str | None):
        super().__init__()
        self._sirlar = [s for s in sirlar if s]

    def maske(self, metin: str) -> str:
        metin = maskele(metin)
        for s in self._sirlar:
            metin = metin.replace(s, "<GIZLI>")
        return metin

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.maske(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = self.maske("".join(traceback.format_exception(*record.exc_info)))
            elif record.exc_text:
                record.exc_text = self.maske(record.exc_text)
            if record.stack_info:
                record.stack_info = self.maske(record.stack_info)
        except Exception:  # süzgeç günlüklemeyi asla düşürmesin
            record.msg = "<günlük kaydı maskelenemedi>"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def gunluk_kur(bot_token: str | None, *extra_sirlar: str | None, seviye: int = logging.INFO) -> logging.Handler:
    isleyici = logging.StreamHandler(sys.stderr)  # systemd/journald stderr'i toplar
    isleyici.addFilter(SirFiltresi(bot_token, *extra_sirlar))
    isleyici.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    kok = logging.getLogger()
    kok.handlers[:] = [isleyici]
    kok.setLevel(seviye)
    for ad in ("httpx", "httpcore", "hpack"):
        logging.getLogger(ad).setLevel(logging.WARNING)
    return isleyici
