"""python-telegram-bot bağlayıcısı: Update -> Olay -> Arayuz -> Yanit -> Telegram.

Bu katman yalnız çeviri yapar; yetki ve iş mantığı `Arayuz`dadır. Her komut ve her düğme
Arayuz içinde yeniden yetkilendirilir.
"""
from __future__ import annotations

import logging
from typing import Callable

from telegram import BotCommand, Update
from telegram.error import Conflict, InvalidToken, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..outbox import GonderimHatasi
from .arayuz import KOMUTLAR, Arayuz
from .gonderici import TelegramGonderici
from .modeller import Olay, Yanit

log = logging.getLogger("dipbot.tg.uygulama")

# my_chat_member açıkça eklenmeli: allowed_updates verildiğinde varsayılanlar geçerli olmaz.
IZINLI_GUNCELLEMELER = ["message", "callback_query", "my_chat_member"]

CIKIS_YAPILANDIRMA = 2
CIKIS_TELEGRAM_FATAL = 3  # geçersiz bot token'ı veya Conflict (başka örnek)


def bot_komutlari() -> list[BotCommand]:
    return [BotCommand(ad, aciklama) for ad, aciklama in KOMUTLAR.items()]


class Baglayici:
    def __init__(self, arayuz: Arayuz, gonderici: TelegramGonderici, fatal: Callable[[int], None] | None = None):
        self.arayuz = arayuz
        self.gonderici = gonderici
        self._fatal = fatal

    # ---- yardımcılar
    def _olay(self, update: Update, **kw) -> Olay | None:
        sohbet = update.effective_chat
        if sohbet is None:
            return None
        kullanici = update.effective_user
        return Olay(
            sohbet_id=sohbet.id,
            sohbet_turu=sohbet.type,
            kullanici_id=kullanici.id if kullanici else None,
            **kw,
        )

    async def _teslim(self, o: Olay, y: Yanit | None, query_id: str | None = None) -> None:
        if y is None:
            return
        if o.dugme_mi and query_id and not o.acked:
            await self.gonderici.callback_yanitla(query_id, y.uyari, y.uyari_goster)
        if not y.metin or y.sessiz:
            return
        duzenle = o.mesaj_id if (o.dugme_mi and y.duzenle) else None
        try:
            await self.gonderici.yanit_gonder(o.sohbet_id, y.metin, y.klavye, duzenle_mesaj_id=duzenle)
        except GonderimHatasi as h:
            log.warning("yanıt gönderilemedi (%s): %s", o.sohbet_id, h)

    # ---- işleyiciler
    async def komut_isle(self, update: Update, context: ContextTypes.DEFAULT_TYPE, ad: str) -> None:
        mesaj = update.effective_message
        if mesaj is None or not mesaj.text:
            return
        parcalar = mesaj.text.split(None, 1)
        o = self._olay(update, komut=ad, arg=parcalar[1] if len(parcalar) > 1 else "", mesaj_id=mesaj.message_id)
        if o is None:
            return
        await self._teslim(o, await self.arayuz.komut(o))

    async def buton_isle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        mesaj = q.message
        o = self._olay(update, veri=q.data or "", mesaj_id=mesaj.message_id if mesaj else None)
        if o is None:
            # Sohbet bilinmiyor (satır içi/erişilemez mesaj): yine de callback'i yanıtla.
            await self.gonderici.callback_yanitla(q.id, "Bu düğme artık geçerli değil.", True)
            return
        o.ack = lambda: self._ack(q.id)
        y = await self.arayuz.buton(o)
        await self._teslim(o, y, query_id=q.id)

    async def _ack(self, query_id: str) -> None:
        await self.gonderici.callback_yanitla(query_id)

    async def metin_isle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mesaj = update.effective_message
        if mesaj is None or not mesaj.text:
            return
        o = self._olay(update, metin=mesaj.text, mesaj_id=mesaj.message_id)
        if o is None:
            return
        sohbet_id, mesaj_id = o.sohbet_id, mesaj.message_id
        o.sil = lambda: self.gonderici.mesaj_sil(sohbet_id, mesaj_id)
        await self._teslim(o, await self.arayuz.metin(o))

    async def uyelik_isle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        d = update.my_chat_member
        if d is None:
            return
        self.arayuz.bot_uyelik_degisti(
            d.chat.id,
            d.chat.type,
            d.old_chat_member.status if d.old_chat_member else None,
            d.new_chat_member.status if d.new_chat_member else None,
            d.from_user.id if d.from_user else None,
        )

    async def goc_isle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        if m is None:
            return
        if m.migrate_to_chat_id:
            self.arayuz.grup_goc(m.chat.id, m.migrate_to_chat_id)
        elif m.migrate_from_chat_id:
            self.arayuz.grup_goc(m.migrate_from_chat_id, m.chat.id)

    async def hata_isle(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, InvalidToken):
            log.critical("Telegram bot token'ı geçersiz; kapatılıyor")
            if self._fatal:
                self._fatal(CIKIS_TELEGRAM_FATAL)
        elif isinstance(err, Conflict):
            log.critical("Telegram Conflict: aynı token ile başka bir örnek getUpdates çalıştırıyor; kapatılıyor")
            if self._fatal:
                self._fatal(CIKIS_TELEGRAM_FATAL)
        elif isinstance(err, (TimedOut, NetworkError)):
            log.warning("geçici Telegram ağ hatası: %s", type(err).__name__)
        else:
            log.error("işlenmeyen hata: %s", type(err).__name__, exc_info=err)


def kayit_et(app: Application, b: Baglayici) -> None:
    """Tüm komut/düğme/üyelik işleyicilerini kaydeder."""
    for ad in KOMUTLAR:
        async def isleyici(update, context, _ad=ad):
            await b.komut_isle(update, context, _ad)

        app.add_handler(CommandHandler(ad, isleyici))
    app.add_handler(CallbackQueryHandler(b.buton_isle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, b.metin_isle))
    app.add_handler(ChatMemberHandler(b.uyelik_isle, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, b.goc_isle))
    app.add_error_handler(b.hata_isle)
