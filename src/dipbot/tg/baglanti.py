"""Oyuncu/parti kimliği ve Diplomacia bağlantısı ayrıştırma.

Kabul: düz pozitif sayı (1-12 hane, baştaki sıfır yok) VEYA güvenilir alan adındaki (diplomacia.com.tr,
www.diplomacia.com.tr; http/https veya şemasız) şu yollar:
    /player/ID   /profile/player/ID   /press/player/ID      (oyuncu)
    /party/ID    /profile/party/ID    /press/party/ID       (parti)
`profile` ve `press` yalnız bağlantının geldiği sayfa bağlamıdır; kimlik aynıdır. Sorgu (?...) ve parça (#...)
yok sayılır. Kullanıcı bilgisi (user@), port, yüzde-kodlama, ek yol parçası, başka alan adı REDDEDİLİR.
Oyuncu komutuna parti bağlantısı (veya tersi) açık hatayla reddedilir. Hata mesajları girdiyi yansıtmaz.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

GUVENILIR_ALAN_ADLARI = frozenset({"diplomacia.com.tr", "www.diplomacia.com.tr"})
_KIMLIK = r"[1-9][0-9]{0,11}"
_HAM = re.compile(_KIMLIK)
_YOL = re.compile(rf"/(?:(?:profile|press)/)?(player|party)/({_KIMLIK})/?")
_ADLAR = {"player": "player", "party": "party"}
_ORNEK = {
    "player": "72400 veya https://diplomacia.com.tr/player/72400",
    "party": "1244 veya https://diplomacia.com.tr/party/1244",
}


class BaglantiHatasi(ValueError):
    """Kullanıcıya gösterilebilir Türkçe açıklama (girdi içermez)."""


def _gecersiz(beklenen: str) -> BaglantiHatasi:
    ad = _ADLAR[beklenen]
    return BaglantiHatasi(f"Enter a valid {ad} ID or Diplomacia {ad} link (e.g. {_ORNEK[beklenen]}).")


def kimlik_coz_ayrintili(arg: str, beklenen: str) -> str:
    """Kimliği döner; geçersizse BaglantiHatasi. beklenen: 'player' | 'party'."""
    if beklenen not in _ADLAR:
        raise ValueError("beklenen tür player veya party olmalı")
    ham = (arg or "").strip()
    if not ham or any(c.isspace() for c in ham):
        raise _gecersiz(beklenen)
    if _HAM.fullmatch(ham):
        return ham
    if "://" in ham:
        url = ham
    elif ham.lower().startswith(("diplomacia.com.tr/", "www.diplomacia.com.tr/")):
        url = "https://" + ham
    else:
        raise _gecersiz(beklenen)
    try:
        parca = urlsplit(url)
    except ValueError:
        raise _gecersiz(beklenen) from None
    # netloc kullanıcı bilgisi / port / ters eğik çizgi içerirse güvenilir listede olmaz.
    if parca.scheme.lower() not in ("http", "https") or parca.netloc.lower() not in GUVENILIR_ALAN_ADLARI:
        raise _gecersiz(beklenen)
    if "%" in parca.path:
        raise _gecersiz(beklenen)
    m = _YOL.fullmatch(parca.path)
    if m is None:
        raise _gecersiz(beklenen)
    tur, kimlik = m.group(1), m.group(2)
    if tur != beklenen:
        raise BaglantiHatasi(
            f"This is a {_ADLAR[tur]} link; this command expects a {_ADLAR[beklenen]} link or ID "
            f"(e.g. {_ORNEK[beklenen]})."
        )
    return kimlik
