"""Bildirim (log) kanalı doğrulaması: kanal Telegram API'sinde VAR MI ve bot ORADA MESAJ GÖNDEREBİLİR Mİ?

Mesaj göndermeden doğrular (kanala test mesajı atılmaz): `getChat` (kanal var, türü 'channel') + `getMe` +
`getChatMember(kanal, bot)` (bot kanal sahibi VEYA yönetici ve `can_post_messages` açık). Sonuç hazır değilse
nedeni açık İngilizce metindir; token/sır içermez. Ağ hatası geçici sayılır (kanal sorunu varsayılmaz ama hazır da
sayılmaz); kısa aralıkla yeniden denenir. Hazır değilken tarama DURAKLATILIR ve kanal satırları gönderilmez
(değişiklik bildirimsiz yutulmasın)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut

from ..zaman import Saat, simdi_utc

log = logging.getLogger("dipbot.tg.kanal")

HAZIR_YENIDEN_KONTROL = timedelta(hours=6)  # hazırken: bu sıklıkta yeniden doğrula
HAZIR_DEGIL_YENIDEN_KONTROL = timedelta(minutes=5)  # hazır değilken: bu sıklıkta yeniden dene


@dataclass(frozen=True)
class KanalDurumu:
    hazir: bool
    neden: str  # hazırsa "ok"; değilse açık İngilizce neden
    gecici: bool = False  # True: ağ/geçici hata (kanal sorunu olduğu kanıtlanmadı)
    baslik: str | None = None  # kanal adı (kamuya açık bilgi)


class KanalDogrulayici:
    def __init__(self, bot, kanal_id: int, saat: Saat = simdi_utc):
        self.bot = bot
        self.kanal_id = kanal_id
        self.saat = saat
        self.durum: KanalDurumu | None = None
        self._son: datetime | None = None

    async def dogrula(self) -> KanalDurumu:
        """Telegram API ile doğrular ve sonucu saklar (hata fırlatmaz)."""
        try:
            self.durum = await self._dogrula()
        except Exception as e:  # beklenmeyen hata da "hazır değil" sayılır (güvenli taraf); sır loga yazılmaz
            self.durum = KanalDurumu(False, f"unexpected error while checking the channel ({type(e).__name__})", gecici=True)
        self._son = self.saat()
        if self.durum.hazir:
            log.info("bildirim kanalı doğrulandı (id=%s)", self.kanal_id)
        else:
            log.error("bildirim kanalı HAZIR DEĞİL (id=%s): %s", self.kanal_id, self.durum.neden)
        return self.durum

    async def _dogrula(self) -> KanalDurumu:
        try:
            sohbet = await self.bot.get_chat(self.kanal_id)
        except BadRequest as e:
            return KanalDurumu(False, f"channel not found or not accessible to the bot ({_kisa(e)})")
        except Forbidden as e:
            return KanalDurumu(False, f"the bot has no access to the channel ({_kisa(e)})")
        except (TimedOut, NetworkError) as e:
            return KanalDurumu(False, f"could not reach Telegram ({type(e).__name__})", gecici=True)
        except TelegramError as e:
            return KanalDurumu(False, f"Telegram error while checking the channel ({type(e).__name__})", gecici=True)
        tur = getattr(sohbet, "type", None)
        baslik = getattr(sohbet, "title", None)
        if tur != "channel":
            return KanalDurumu(False, f"the configured chat is not a channel (type: {tur})", baslik=baslik)
        try:
            ben = await self.bot.get_me()
            uye = await self.bot.get_chat_member(self.kanal_id, ben.id)
        except (BadRequest, Forbidden) as e:
            return KanalDurumu(False, f"could not read the bot's membership in the channel ({_kisa(e)})", baslik=baslik)
        except (TimedOut, NetworkError) as e:
            return KanalDurumu(False, f"could not reach Telegram ({type(e).__name__})", gecici=True, baslik=baslik)
        except TelegramError as e:
            return KanalDurumu(False, f"Telegram error while checking membership ({type(e).__name__})", gecici=True, baslik=baslik)
        durum = getattr(uye, "status", None)
        if durum == "creator":
            return KanalDurumu(True, "ok", baslik=baslik)
        if durum != "administrator":
            return KanalDurumu(False, f"the bot is not an administrator of the channel (status: {durum})", baslik=baslik)
        if getattr(uye, "can_post_messages", None) is not True:
            return KanalDurumu(False, "the bot is an administrator but lacks the 'Post messages' permission", baslik=baslik)
        return KanalDurumu(True, "ok", baslik=baslik)

    def hazir_mi(self) -> bool:
        return bool(self.durum and self.durum.hazir)

    async def hazirla(self) -> bool:
        """Gerekirse (hiç doğrulanmadıysa veya yeniden kontrol vakti geldiyse) doğrular. Dönüş: hazır mı."""
        simdi = self.saat()
        aralik = HAZIR_YENIDEN_KONTROL if self.hazir_mi() else HAZIR_DEGIL_YENIDEN_KONTROL
        if self.durum is None or self._son is None or simdi - self._son >= aralik:
            await self.dogrula()
        return self.hazir_mi()


def _kisa(e: BaseException) -> str:
    return str(e)[:80]
