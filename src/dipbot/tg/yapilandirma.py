"""Telegram çalışma yapılandırması (ortam değişkenleri). Sırlar repr/loga yazılmaz."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..config import Ayarlar
from ..diplomacia.istemci import GecersizNumara, numara_dogrula

_BOT_TOKEN_DESENI = re.compile(r"\d{3,}:[A-Za-z0-9_-]{20,}")


class YapilandirmaHatasi(Exception):
    """Eksik/geçersiz yapılandırma. Mesaj sır içermez."""


@dataclass(frozen=True)
class TelegramAyarlari:
    bot_token: str = field(repr=False)
    ayarlar: Ayarlar
    token_dosyasi: Path  # Diplomacia oyun hesabı token'ı (BOT_TOKEN değil); DB dışında, 0600
    dogrulama_oyuncusu: str = "72400"  # token doğrulamada sorgulanan herkese açık oyuncu (sır değil)
    kanal_id: int = config.LOG_KANAL_ID  # TÜM takip bildirimlerinin tek hedefi (Telegram kanalı)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TelegramAyarlari":
        env = os.environ if env is None else env
        bot_token = env.get("BOT_TOKEN", "").strip()
        if not bot_token:
            raise YapilandirmaHatasi("BOT_TOKEN ortam değişkeni tanımlı değil (BotFather'dan alınan Telegram bot token'ı)")
        if not _BOT_TOKEN_DESENI.fullmatch(bot_token):
            raise YapilandirmaHatasi("BOT_TOKEN biçimi geçersiz (<sayı>:<anahtar> bekleniyor; değer gösterilmez)")
        try:
            ayarlar = Ayarlar.from_env(env)
        except ValueError as e:
            raise YapilandirmaHatasi(str(e)) from None
        yol = env.get("DIPLOMACIA_TOKEN_DOSYASI", "").strip()
        token_dosyasi = Path(yol) if yol else ayarlar.db_path.parent / "diplomacia.token"
        oyuncu = env.get("DIPLOMACIA_DOGRULAMA_OYUNCU", "").strip() or "72400"
        try:
            oyuncu = numara_dogrula(oyuncu)
        except GecersizNumara:
            raise YapilandirmaHatasi(
                "DIPLOMACIA_DOGRULAMA_OYUNCU geçersiz (1-12 haneli pozitif oyuncu numarası bekleniyor)"
            ) from None
        kanal_ham = env.get("LOG_CHANNEL_ID", "").strip()
        kanal_id = config.LOG_KANAL_ID
        if kanal_ham:
            try:
                kanal_id = int(kanal_ham)
            except ValueError:
                kanal_id = 0
            if kanal_id >= 0:
                raise YapilandirmaHatasi("LOG_CHANNEL_ID negatif bir Telegram sohbet kimliği olmalı (ör. -100...)")
        return cls(bot_token, ayarlar, token_dosyasi, oyuncu, kanal_id)
