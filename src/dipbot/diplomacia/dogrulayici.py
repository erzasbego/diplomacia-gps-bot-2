"""Gerçek TokenDogrulayici: aday token'ı YALNIZ o istekte kullanarak GET /api/players/72400 çağırır.

KARAR: doğrulama isteği, kullanıcının DevTools'ta başarılı gördüğü oyuncu isteğidir (kimlik biçimi
`Authorization: Bearer` + `X-Client-Platform: web`, istemci.OrtamKimlik ile ORTAK). /api/players/profile
KULLANILMAZ (şeması bilinmiyor; yalnız durum koduna güvenmek sahte başarıya açıktı).
Kabul ölçütü: HTTP 200 VE gövde JSON nesnesi VE mevcut oyuncu ayrıştırıcısıyla (ayristir.oyuncu_ayristir)
başarıyla ayrıştırılabilmesi. HTML/{}/boş/ayrıştırılamayan gövde, 401, 403, 5xx, ağ hatası -> kabul YOK.
Sonuç durumları: 200+geçerli -> "gecerli"; 401 -> "gecersiz"; geri kalan her şey (403 dahil) -> "hata"
(doğrulanamadı; 403 anlamı doğrulanmadı, token'ın geçersiz olduğu SÖYLENMEZ). Çağıran reddeder.
"Geçerli" yalnız "aday token ile oyuncu isteği başarılı" demektir; canlı davranış garantisi değildir.

Mevcut token dosyasına dokunulmaz. Token loga/hata metnine/repr'e girmez; httpx istisna metni aktarılmaz.
"""
from __future__ import annotations

import httpx

from ..kaynak import TokenYok
from ..tg.token import DogrulamaSonucu
from . import ayristir
from .istemci import (
    MAKS_GOVDE_BAYT,
    TABAN_ADRES,
    VARSAYILAN_ZAMAN_ASIMI,
    KimlikYapilandirilmadi,
    OrtamKimlik,
    numara_dogrula,
)

DOGRULAMA_OYUNCU_NO = "72400"  # VARSAYILAN: kullanıcının DevTools'ta doğruladığı herkese açık oyuncu (sır değil)
DOGRULAMA_YOLU = f"/api/players/{DOGRULAMA_OYUNCU_NO}"


class OyuncuDogrulayici:
    def __init__(
        self,
        kimlik: OrtamKimlik,
        *,
        taban: str = TABAN_ADRES,
        transport: httpx.AsyncBaseTransport | None = None,
        zaman_asimi: float = VARSAYILAN_ZAMAN_ASIMI,
        oyuncu_no: str = DOGRULAMA_OYUNCU_NO,
    ):
        self._oyuncu_no = numara_dogrula(oyuncu_no)  # DIPLOMACIA_DOGRULAMA_OYUNCU
        self._kimlik = kimlik
        self._taban = taban
        self._transport = transport
        self._zaman_asimi = zaman_asimi

    def __repr__(self) -> str:
        return "OyuncuDogrulayici(...)"

    @property
    def yapilandirildi(self) -> bool:
        return bool(self._kimlik.yapilandirildi)

    async def dogrula(self, token: str) -> DogrulamaSonucu:
        try:
            basliklar = self._kimlik.basliklar_icin(token)
        except KimlikYapilandirilmadi:
            return DogrulamaSonucu("yapilandirilmadi", "Diplomacia credential format is not configured")
        except TokenYok:
            return DogrulamaSonucu("gecersiz", "token format is invalid")
        durum: int | None = None
        govde = None
        try:
            async with httpx.AsyncClient(
                base_url=self._taban,
                transport=self._transport,
                timeout=httpx.Timeout(self._zaman_asimi),
                follow_redirects=False,
                trust_env=False,
            ) as http:
                r = await http.get(f"/api/players/{self._oyuncu_no}", headers=basliklar)
                durum = r.status_code
                if durum == 200 and len(r.content) <= MAKS_GOVDE_BAYT:
                    try:
                        govde = r.json()
                    except ValueError:
                        govde = None
        except httpx.HTTPError:
            durum = None  # istisna metni (token içerebilir) aktarılmaz
        if durum == 200:
            if not isinstance(govde, dict):
                return DogrulamaSonucu("hata", "returned 200 but the body is not a JSON object; could not be verified")
            try:
                ayristir.oyuncu_ayristir(govde, self._oyuncu_no)
            except Exception:  # ayrıştırıcı hatası: kabul YOK (ayrıntı aktarılmaz)
                return DogrulamaSonucu("hata", "returned 200 but the body could not be parsed as player data; could not be verified")
            return DogrulamaSonucu("gecerli", "player request succeeded with the candidate token")
        if durum == 401:
            return DogrulamaSonucu("gecersiz", "the site rejected the token (401)")
        if durum == 403:
            return DogrulamaSonucu("hata", "access denied (403); token could not be verified (the ID format or a site block may be the cause)")
        if durum == 404:
            return DogrulamaSonucu(
                "hata",
                "verification player not found; this does not mean the token is invalid "
                "(provide a different player via DIPLOMACIA_DOGRULAMA_OYUNCU)",
            )
        if durum is None:
            return DogrulamaSonucu("hata", "could not reach the site for verification")
        return DogrulamaSonucu("hata", f"unexpected response code ({durum}); could not be verified")


ProfilDogrulayici = OyuncuDogrulayici  # eski ad (uyumluluk); artık profil uç noktası KULLANILMAZ
