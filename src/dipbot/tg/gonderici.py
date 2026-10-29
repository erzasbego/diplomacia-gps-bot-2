"""Telegram gönderici: outbox `Gonderici` arayüzünün uygulaması + etkileşimli yanıtlar.

- HTML parse_mode; 4096 sınırı için satır sınırında bölme.
- Hız sınırı: sohbette saniyede ~1, grupta dakikada 20 (sohbet başına).
- Hata sınıflandırması İSTİSNA SINIFINA göre yapılır (metin eşleşmesi yok). Dikkat:
  BadRequest, NetworkError'ın alt sınıfıdır; BadRequest önce denetlenmelidir.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from ..outbox import GonderimHatasi
from .kanal import kanal_sorunu_mu
from . import bicim
from .modeller import Klavye

log = logging.getLogger("dipbot.tg.gonderici")

GRUP_DAKIKA_SINIRI = 20
SOHBET_ARALIK_SN = 1.0
OUTBOX_AZAMI_UYKU = 5.0  # outbox gönderiminde bundan uzun bekleme uyutmaz; satır sonraki tura ertelenir
ETKILESIM_AZAMI_BEKLEME = 30.0  # etkileşimli yanıtta RetryAfter'ı bu süreye kadar bekle

from ..maske import maskele  # noqa: E402,F401  (ortak maskeleme; geriye dönük içe aktarım)


# ---- Hata sınıflandırması -------------------------------------------------
@dataclass(frozen=True)
class Siniflama:
    tur: str  # retry_after | gecici | kalici | goc | fatal
    kalici: bool
    bekle_saniye: float | None = None
    yeni_sohbet_id: int | None = None
    aciklama: str = ""


def _saniye(deger) -> float:
    return deger.total_seconds() if isinstance(deger, timedelta) else float(deger)


def siniflandir(exc: BaseException) -> Siniflama:
    """İstisna sınıfına göre karar. SIRA ÖNEMLİ: BadRequest ve TimedOut, NetworkError'ın altındadır."""
    ad = type(exc).__name__
    if isinstance(exc, RetryAfter):
        return Siniflama("retry_after", False, _saniye(exc.retry_after), aciklama=ad)
    if isinstance(exc, ChatMigrated):
        return Siniflama("goc", True, yeni_sohbet_id=int(exc.new_chat_id), aciklama=ad)
    if isinstance(exc, (InvalidToken, Conflict)):
        return Siniflama("fatal", True, aciklama=ad)
    if isinstance(exc, Forbidden):
        return Siniflama("kalici", True, aciklama=ad)  # bot engellendi/atıldı/yetkisiz
    if isinstance(exc, BadRequest):
        return Siniflama("kalici", True, aciklama=ad)  # içerik/hedef hatası: yeniden denemek çözmez
    if isinstance(exc, TimedOut):
        return Siniflama("gecici", False, aciklama=ad)
    if isinstance(exc, NetworkError):
        return Siniflama("gecici", False, aciklama=ad)
    if isinstance(exc, TelegramError):
        return Siniflama("gecici", False, aciklama=ad)  # bilinmeyen Telegram hatası: sınırlı yeniden dene
    return Siniflama("gecici", False, aciklama=ad)


def gonderim_hatasina_cevir(exc: BaseException) -> GonderimHatasi:
    s = siniflandir(exc)
    mesaj = s.aciklama
    if s.tur in ("kalici", "fatal"):
        # Telegram'ın kendi açıklaması (örn. chat not found) teşhise yardım eder; token desenleri maskelenir.
        mesaj += ": " + maskele(str(exc))[:120]
    h = GonderimHatasi(mesaj, kalici=s.kalici, bekle_saniye=s.bekle_saniye)
    h.siniflama = s  # type: ignore[attr-defined]
    return h


# ---- Hız sınırı -----------------------------------------------------------
class HizSiniri:
    """Sohbet başına: ardışık mesajlar arası >= 1 sn; grupta (kimlik < 0) 60 sn'de en çok 20."""

    def __init__(
        self,
        saat: Callable[[], float] = time.monotonic,
        uyku: Callable[[float], Awaitable[None]] = asyncio.sleep,
        grup_dk: int = GRUP_DAKIKA_SINIRI,
        sohbet_aralik: float = SOHBET_ARALIK_SN,
    ):
        self._saat = saat
        self._uyku = uyku
        self.grup_dk = grup_dk
        self.sohbet_aralik = sohbet_aralik
        self._son: dict[int, deque[float]] = {}
        self._engel: dict[int, float] = {}
        self._kilit: dict[int, asyncio.Lock] = {}

    def ertele(self, sohbet_id: int, saniye: float) -> None:
        """Telegram RetryAfter verdiyse o sohbete o süre boyunca gönderme."""
        self._engel[sohbet_id] = max(self._engel.get(sohbet_id, 0.0), self._saat() + saniye)

    async def bekle(self, sohbet_id: int, azami: float | None = None) -> None:
        """`azami` verilirse ve gereken bekleme bunu aşıyorsa UYUMAZ: bekle_saniyeli GonderimHatasi fırlatır."""
        kilit = self._kilit.setdefault(sohbet_id, asyncio.Lock())
        async with kilit:
            while True:
                simdi = self._saat()
                d = self._son.setdefault(sohbet_id, deque())
                while d and simdi - d[0] >= 60.0:
                    d.popleft()
                bekle = self._engel.get(sohbet_id, 0.0) - simdi
                if d:
                    bekle = max(bekle, d[-1] + self.sohbet_aralik - simdi)
                if sohbet_id < 0 and len(d) >= self.grup_dk:
                    bekle = max(bekle, d[0] + 60.0 - simdi)
                if bekle <= 0:
                    break
                if azami is not None and bekle > azami:
                    raise GonderimHatasi("hiz siniri penceresi", bekle_saniye=bekle)
                await self._uyku(bekle)
            d.append(self._saat())


# ---- Klavye dönüştürme ----------------------------------------------------
def klavye_markup(klavye: Klavye | None):
    if not klavye:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(d.etiket, callback_data=d.veri) if d.veri is not None
                else InlineKeyboardButton(d.etiket, url=d.url)
                for d in satir
            ]
            for satir in klavye
        ]
    )


_ONIZLEME_KAPALI = LinkPreviewOptions(is_disabled=True)


class TelegramGonderici:
    """`bot`: telegram.Bot benzeri (send_message, edit_message_text, answer_callback_query,
    delete_message, set_my_commands). Testlerde sahte bot verilir."""

    def __init__(
        self,
        bot,
        *,
        siniri: HizSiniri | None = None,
        goc_geri_cagrisi: Callable[[int, int], None] | None = None,
        uyku: Callable[[float], Awaitable[None]] = asyncio.sleep,
        izinli_hedefler=None,
        kanal=None,
    ):
        self.bot = bot
        # Üretimde: {kurucu özel sohbeti, log kanalı}. Outbox gönderimi bunun dışındaki hedefi REDDEDER (kalıcı hata).
        self._izinli = None if izinli_hedefler is None else {int(x) for x in izinli_hedefler}
        self._kanal = kanal  # KanalDogrulayici: hazır değilse kanal satırları bekletilir (hata sayılmaz)
        self.siniri = siniri or HizSiniri()
        self._goc = goc_geri_cagrisi
        self._uyku = uyku

    # ---- ortak çağrı: hız sınırı + sınıflandırma
    async def _yap(self, sohbet_id: int, cagri: Callable[[], Awaitable], azami: float = ETKILESIM_AZAMI_BEKLEME):
        # Uzun bekleme (RetryAfter / grup penceresi) çağıranı bloke etmez; bekle_saniyeli hata döner.
        await self.siniri.bekle(sohbet_id, azami)
        try:
            return await cagri()
        except Exception as e:
            s = siniflandir(e)
            if s.tur == "retry_after" and s.bekle_saniye is not None:
                self.siniri.ertele(sohbet_id, s.bekle_saniye)
            if s.tur == "goc" and s.yeni_sohbet_id is not None and self._goc is not None:
                try:
                    self._goc(sohbet_id, s.yeni_sohbet_id)
                except Exception:  # bildirim hatası gönderim sınıflandırmasını bozmasın
                    log.exception("grup göçü geri çağrısı hatası")
            raise gonderim_hatasina_cevir(e) from e

    # ---- outbox arayüzü
    async def gonder(self, sohbet_id: int, tur: str, veri: dict) -> None:
        if self._izinli is not None and sohbet_id not in self._izinli:
            raise GonderimHatasi("target is neither the owner chat nor the log channel", kalici=True)
        if self._kanal is not None and sohbet_id == self._kanal.kanal_id and not self._kanal.hazir_mi():
            # Kanal doğrulanamadı: satır hata/deneme sayılmadan beklemeye alınır (bekle_saniye = 429 benzeri erteleme).
            raise GonderimHatasi("log channel is not ready", bekle_saniye=300.0)
        metin = bicim.bildirim_metni(tur, veri)
        try:
            for parca in bicim.bol(metin):
                await self._yap(
                    sohbet_id,
                    lambda p=parca: self.bot.send_message(
                        chat_id=sohbet_id,
                        text=p,
                        parse_mode="HTML",
                        link_preview_options=_ONIZLEME_KAPALI,
                    ),
                    OUTBOX_AZAMI_UYKU,
                )
        except GonderimHatasi as h:
            # Kanal erişimi/yetkisi gönderim sırasında kaybolduysa satır KALICI hata olmaz (bildirim kaybolmasın):
            # kanal 'hazır değil' işaretlenir (tarama duraklar, kurucuya bildirim gider) ve satır bekletilir.
            if self._kanal is not None and sohbet_id == self._kanal.kanal_id and kanal_sorunu_mu(h.__cause__):
                self._kanal.hazir_degil_isaretle(f"sending failed: {h}"[:200])
                raise GonderimHatasi("log channel is not ready", bekle_saniye=300.0) from h
            raise

    # ---- etkileşimli yanıtlar (RetryAfter kısaysa bir kez bekleyip yeniden dener)
    async def _etkilesim(self, sohbet_id: int, cagri: Callable[[], Awaitable]):
        try:
            return await self._yap(sohbet_id, cagri)
        except GonderimHatasi as h:
            if h.bekle_saniye is not None and h.bekle_saniye <= ETKILESIM_AZAMI_BEKLEME:
                await self._uyku(h.bekle_saniye)
                return await self._yap(sohbet_id, cagri)
            raise

    async def yanit_gonder(
        self,
        sohbet_id: int,
        metin: str,
        klavye: Klavye | None = None,
        *,
        duzenle_mesaj_id: int | None = None,
    ) -> None:
        parcalar = bicim.bol(metin)
        markup_son = klavye_markup(klavye)
        for i, parca in enumerate(parcalar):
            son = i == len(parcalar) - 1
            markup = markup_son if son else None
            if i == 0 and duzenle_mesaj_id is not None:
                try:
                    await self._etkilesim(
                        sohbet_id,
                        lambda p=parca, m=markup: self.bot.edit_message_text(
                            chat_id=sohbet_id,
                            message_id=duzenle_mesaj_id,
                            text=p,
                            parse_mode="HTML",
                            reply_markup=m,
                            link_preview_options=_ONIZLEME_KAPALI,
                        ),
                    )
                except GonderimHatasi as h:
                    # Düzenlenemeyen mesaj (silinmiş/aynı içerik): sessizce geç; kullanıcı callback
                    # yanıtını zaten aldı. Başka hata türlerinde yeniden fırlat.
                    if getattr(getattr(h, "siniflama", None), "tur", "") == "kalici":
                        log.info("mesaj düzenlenemedi: %s", h)
                        continue
                    raise
                continue
            await self._etkilesim(
                sohbet_id,
                lambda p=parca, m=markup: self.bot.send_message(
                    chat_id=sohbet_id,
                    text=p,
                    parse_mode="HTML",
                    reply_markup=m,
                    link_preview_options=_ONIZLEME_KAPALI,
                ),
            )

    async def callback_yanitla(self, query_id: str, metin: str | None = None, goster: bool = False) -> bool:
        """answerCallbackQuery. Başarısızlık akışı bozmaz; True = iletildi."""
        try:
            await self.bot.answer_callback_query(
                callback_query_id=query_id, text=(metin[:200] if metin else None), show_alert=goster
            )
            return True
        except Exception as e:
            log.info("answerCallbackQuery başarısız: %s", siniflandir(e).aciklama)
            return False

    async def mesaj_sil(self, sohbet_id: int, mesaj_id: int) -> bool:
        try:
            await self._yap(sohbet_id, lambda: self.bot.delete_message(chat_id=sohbet_id, message_id=mesaj_id))
            return True
        except GonderimHatasi as h:
            log.info("mesaj silinemedi: %s", type(h).__name__)
            return False
