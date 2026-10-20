"""Telegram katmanı test yardımcıları: SAHTE bot, sahte zaman, olay üreticiler.

Bunlar MOCK'tur; gerçek Telegram davranışını doğrulamaz. Ağ çağrısı yapılmaz.
"""
from __future__ import annotations

from types import SimpleNamespace

from dipbot.tg.gonderici import HizSiniri, TelegramGonderici
from dipbot.tg.modeller import Olay

KURUCU = 1000
YETKILI = 55
YABANCI = 77
G1 = -1001
G2 = -1002
G3 = -1003  # onaysız grup


class SahteBot:
    """telegram.Bot'un kullanılan yöntemlerinin kayıt tutan taklidi."""

    def __init__(self):
        self.cagrilar: list[tuple[str, dict]] = []
        self.hatalar: list[BaseException] = []  # sıradaki çağrılarda FIFO fırlatılır

    async def _kayit(self, ad: str, **kw):
        self.cagrilar.append((ad, kw))
        if self.hatalar:
            raise self.hatalar.pop(0)
        return SimpleNamespace(message_id=1)

    async def send_message(self, **kw):
        return await self._kayit("send_message", **kw)

    async def edit_message_text(self, **kw):
        return await self._kayit("edit_message_text", **kw)

    async def answer_callback_query(self, **kw):
        return await self._kayit("answer_callback_query", **kw)

    async def delete_message(self, **kw):
        return await self._kayit("delete_message", **kw)

    def say(self, ad: str) -> int:
        return sum(1 for a, _ in self.cagrilar if a == ad)

    def son(self, ad: str) -> dict:
        return [kw for a, kw in self.cagrilar if a == ad][-1]


class SahteZaman:
    """HizSiniri için sanal saat; uyku saati ilerletir."""

    def __init__(self):
        self.t = 0.0
        self.uykular: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def uyku(self, s: float) -> None:
        self.uykular.append(s)
        self.t += s


def hizli_gonderici(bot: SahteBot, **kw) -> TelegramGonderici:
    z = SahteZaman()
    return TelegramGonderici(bot, siniri=HizSiniri(saat=z, uyku=z.uyku), uyku=z.uyku, **kw)


def komut(ad, arg="", *, sohbet=None, tur="private", kul=KURUCU) -> Olay:
    return Olay(
        sohbet_id=kul if sohbet is None else sohbet,
        sohbet_turu=tur,
        kullanici_id=kul,
        komut=ad,
        arg=arg,
        mesaj_id=10,
    )


def grup_komut(ad, arg="", *, grup=G1, kul=KURUCU) -> Olay:
    return komut(ad, arg, sohbet=grup, tur="supergroup", kul=kul)


def dugme(veri, *, sohbet=None, tur="private", kul=KURUCU) -> Olay:
    return Olay(
        sohbet_id=kul if sohbet is None else sohbet,
        sohbet_turu=tur,
        kullanici_id=kul,
        veri=veri,
        mesaj_id=20,
    )


def grup_dugme(veri, *, grup=G1, kul=KURUCU) -> Olay:
    return dugme(veri, sohbet=grup, tur="supergroup", kul=kul)


def veriler(yanit) -> list[str]:
    """Bir yanıttaki tüm callback_data değerleri."""
    return [d.veri for satir in (yanit.klavye or []) for d in satir if d.veri is not None]


def onay_kimligi(yanit) -> str:
    """Yanıttaki 'Onayla' düğmesinin işlem kimliği."""
    for v in veriler(yanit):
        if v.startswith("c:e:"):
            return v.split(":")[2]
    raise AssertionError("onay düğmesi yok: " + repr(veriler(yanit)))
