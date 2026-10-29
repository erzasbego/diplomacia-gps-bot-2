"""Süreç yaşam döngüsü: `python -m dipbot`.

- BOT_TOKEN / OWNER_TELEGRAM_ID / DB_PATH ortam değişkenlerinden (yapılandırma eksikse anlaşılır hata).
- Yalnız DB_PATH dizinine (üretimde /var/lib/dipbot) yazar; token dosyası da orada (DIPLOMACIA_TOKEN_DOSYASI
  ile değiştirilebilir ama yazılabilir bir yolda olmalıdır).
- SIGTERM/SIGINT: polling durur, devam eden taramalar iptal edilir, DB kapatılır.
- Diplomacia kaynağı: her zaman gerçek IstemciKaynagi (varsayılan Authorization: Bearer + X-Client-Platform: web;
  DIPLOMACIA_AUTH_BASLIK/SEMA yalnız isteğe bağlı override). Token HER istekte dosyadan okunur: dosya yoksa
  istek atılmaz (TokenYok), kurucu menüden token verince YENİDEN BAŞLATMA GEREKMEZ. Yer tutucu kaynak yalnız
  geçersiz override'da kullanılır. Canlı davranış DOĞRULANMADI. Durum ekranı gerçek durumu gösterir. Kapanışta httpx istemcisi kapatılır.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import os
import sys
from pathlib import Path
from typing import Mapping

from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application

from .. import config, db, outbox
from ..diplomacia.dogrulayici import OyuncuDogrulayici
from ..diplomacia.istemci import IstemciKaynagi, OrtamKimlik
from ..zamanlayici import TaramaZamanlayici
from ..zaman import simdi_utc
from .arayuz import Arayuz
from .gonderici import TelegramGonderici
from .gunluk import gunluk_kur
from .kanal import KanalDogrulayici
from .kaynak_yok import YapilandirilmamisKaynak
from .token import TokenDeposu, TokenDogrulayici, YapilandirilmamisDogrulayici
from .uygulama import (
    CIKIS_TELEGRAM_FATAL,
    CIKIS_YAPILANDIRMA,
    IZINLI_GUNCELLEMELER,
    Baglayici,
    bot_komutlari,
    kayit_et,
)
from .yapilandirma import TelegramAyarlari, YapilandirmaHatasi

log = logging.getLogger("dipbot")

TARAMA_KAPANIS_BEKLEME_SN = 10.0


async def gorevi_durdur(gorev: asyncio.Task | None, durdur_olayi: asyncio.Event, bekleme: float) -> bool:
    """Önce nazikçe durmasını ister; süre dolarsa İPTAL eder. Dönüş: iptal gerekti mi."""
    if gorev is None:
        return False
    durdur_olayi.set()
    try:
        await asyncio.wait_for(asyncio.shield(gorev), bekleme)
        return False
    except asyncio.TimeoutError:
        gorev.cancel()
        await asyncio.gather(gorev, return_exceptions=True)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("tarama görevi hatayla bitti")
        return False


def sinyalleri_bagla(loop: asyncio.AbstractEventLoop, olay: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, olay.set)
        except (NotImplementedError, RuntimeError):  # Windows'ta add_signal_handler yok
            signal.signal(sig, lambda *_a: loop.call_soon_threadsafe(olay.set))


def kaynak_olustur(token_dosyasi: str | Path, ortam: Mapping[str, str] | None = None):
    """Ham kaynak (SİNİRLİ SARILMAMIŞ; sarmayı tüketici TEK kez yapar).

    Kimlik yapılandırması (varsayılan Bearer) geçerliyse HER ZAMAN IstemciKaynagi (token dosyası başlangıçta
    olmak zorunda değil; her istekte okunur). Geçersiz override -> YapilandirilmamisKaynak.
    Ağ isteği atmaz; token okumaz."""
    ortam = os.environ if ortam is None else ortam
    kimlik = OrtamKimlik(TokenDeposu(token_dosyasi), ortam)
    return IstemciKaynagi(kimlik) if kimlik.yapilandirildi else YapilandirilmamisKaynak()


def dogrulayici_olustur(
    token_dosyasi: str | Path, ortam: Mapping[str, str] | None = None, *, oyuncu_no: str = "72400"
) -> TokenDogrulayici:
    """Kimlik yapılandırması geçerliyse OyuncuDogrulayici (GET /api/players/72400, gövde ayrıştırılır),
    değilse (geçersiz override) değiştirmeyi reddeden YapilandirilmamisDogrulayici."""
    ortam = os.environ if ortam is None else ortam
    kimlik = OrtamKimlik(TokenDeposu(token_dosyasi), ortam)
    return OyuncuDogrulayici(kimlik, oyuncu_no=oyuncu_no) if kimlik.yapilandirildi else YapilandirilmamisDogrulayici()


async def calistir(
    ta: TelegramAyarlari,
    *,
    kaynak=None,
    dogrulayici: TokenDogrulayici | None = None,
) -> int:
    conn = db.baglan(ta.ayarlar.db_path)
    kurucu = ta.ayarlar.owner_telegram_id
    kaynak = kaynak if kaynak is not None else kaynak_olustur(ta.token_dosyasi)
    token_deposu = TokenDeposu(ta.token_dosyasi)
    token_deposu.eski_gecicileri_temizle()
    # Eski gruplara bildirim YOK: bekleyen eski grup hedefli satırlar iptal edilir (silinmez); kanala GİTMEZ.
    h = outbox.bekleyenleri_hizala(conn, kurucu, ta.kanal_id)
    if h.eski_kapsam_iptal or h.hedef_iptal or h.kanala_yonlendirilen:
        log.warning("bekleyen bildirimler hizalandı: eski grup kapsamlı %d iptal (gönderilmeyecek), %d kanala "
                    "yönlendirildi, %d hedefsiz artık iptal", h.eski_kapsam_iptal, h.kanala_yonlendirilen, h.hedef_iptal)
    app = Application.builder().token(ta.bot_token).build()
    kanal = KanalDogrulayici(app.bot, ta.kanal_id)
    zamanlayici = TaramaZamanlayici(
        conn, kaynak, kurucu, token_deposu=token_deposu,
        kapsamlar=frozenset({config.MERKEZ_ID}), kanal_id=ta.kanal_id, kanal=kanal,
    )
    arayuz = Arayuz(
        conn,
        zamanlayici.kaynak,  # zamanlayıcıyla AYNI SinirliKaynak (tek eşzamanlılık sınırı)
        kurucu,
        zamanlayici=zamanlayici,
        dogrulayici=dogrulayici or dogrulayici_olustur(ta.token_dosyasi, oyuncu_no=ta.dogrulama_oyuncusu),
        token_deposu=token_deposu,
        kanal=kanal,
    )
    gonderici = TelegramGonderici(app.bot, izinli_hedefler={kurucu, ta.kanal_id}, kanal=kanal)
    durdur = asyncio.Event()
    sonuc = {"kod": 0}

    def fatal(kod: int) -> None:
        sonuc["kod"] = kod
        durdur.set()

    kayit_et(app, Baglayici(arayuz, gonderici, fatal))
    sinyalleri_bagla(asyncio.get_running_loop(), durdur)

    tarama_durdur = asyncio.Event()
    tarama: asyncio.Task | None = None
    baslatildi = polling = False
    try:
        await app.initialize()  # getMe: geçersiz token burada InvalidToken verir
        await app.start()
        baslatildi = True
        try:
            await app.bot.set_my_commands(bot_komutlari())
        except TelegramError as e:
            log.warning("komut listesi kaydedilemedi: %s", type(e).__name__)
        log.info("Bot kullanıcı adı: @%s; grup mesajlarının tümünü okur: %s",
                 app.bot.username, app.bot.can_read_all_group_messages)
        await kanal.dogrula()  # kanal var mı + botun gönderme yetkisi var mı (mesaj göndermeden); sonuç günlükte/durum ekranında
        await app.updater.start_polling(allowed_updates=IZINLI_GUNCELLEMELER, drop_pending_updates=False)
        polling = True
        tarama = asyncio.create_task(zamanlayici.calis(gonderici, tarama_durdur), name="tarama")
        log.info("Bot çalışıyor (Diplomacia bağlantısı: %s)",
                 "yapılandırılmadı" if isinstance(kaynak, YapilandirilmamisKaynak)
                 else "yapılandırıldı (Bearer + X-Client-Platform; kimlik biçimi kullanıcı DevTools'ta doğruladı, canlı bağlantı test edilmedi; token dosyası her istekte okunur)")
        await durdur.wait()
    except InvalidToken:
        log.critical("BOT_TOKEN geçersiz (Telegram reddetti)")
        sonuc["kod"] = CIKIS_TELEGRAM_FATAL
    finally:
        log.info("Kapanış başlıyor")
        try:
            if polling:
                await app.updater.stop()
            if await gorevi_durdur(tarama, tarama_durdur, TARAMA_KAPANIS_BEKLEME_SN):
                log.warning("devam eden tarama/gönderim görevi iptal edildi")
            if baslatildi:
                await app.stop()
            await app.shutdown()
        except Exception:
            log.exception("kapanış sırasında hata")
        finally:
            kapat = getattr(kaynak, "aclose", None)
            if kapat is not None:
                try:
                    await kapat()
                except Exception:
                    log.warning("kaynak istemcisi kapatılamadı")
            conn.close()
            log.info("Kapandı")
    return sonuc["kod"]


def main(env: Mapping[str, str] | None = None) -> int:
    try:
        ta = TelegramAyarlari.from_env(dict(env) if env is not None else None)
    except YapilandirmaHatasi as e:
        print(f"HATA: {e}", file=sys.stderr)
        return CIKIS_YAPILANDIRMA
    gunluk_kur(ta.bot_token)
    try:
        return asyncio.run(calistir(ta))
    except KeyboardInterrupt:
        return 130
