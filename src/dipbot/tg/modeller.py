"""Telegram'dan bağımsız olay/yanıt modelleri."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

CALLBACK_AZAMI_BAYT = 64
GRUP_TURLERI = frozenset({"group", "supergroup"})


@dataclass(frozen=True)
class Dugme:
    etiket: str
    veri: str | None = None  # callback_data
    url: str | None = None

    def __post_init__(self):
        if (self.veri is None) == (self.url is None):
            raise ValueError("düğmede veri veya url'den tam biri olmalı")
        if self.veri is not None and len(self.veri.encode("utf-8")) > CALLBACK_AZAMI_BAYT:
            raise ValueError("callback_data 64 baytı aşıyor")


Klavye = list[list[Dugme]]


@dataclass
class Olay:
    """Bir komut, düğme tıklaması veya düz metin mesajı (Telegram'dan bağımsız)."""

    sohbet_id: int
    sohbet_turu: str
    kullanici_id: int | None
    komut: str | None = None
    arg: str = ""
    metin: str = field(default="", repr=False)  # token içerebilir: repr'de gösterme
    veri: str | None = None  # callback_data (düğme ise dolu)
    mesaj_id: int | None = None
    sil: Callable[[], Awaitable[bool]] | None = field(default=None, repr=False)
    ack: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    acked: bool = False

    @property
    def grup_mu(self) -> bool:
        return self.sohbet_turu in GRUP_TURLERI

    @property
    def dugme_mi(self) -> bool:
        return self.veri is not None


@dataclass
class Yanit:
    metin: str = ""  # HTML
    klavye: Klavye | None = None
    duzenle: bool = False  # düğme: mevcut mesajı düzenle
    uyari: str | None = None  # answerCallbackQuery metni (<=200)
    uyari_goster: bool = False  # True: açılır uyarı
    sessiz: bool = False  # mesaj gönderme/düzenleme yok
