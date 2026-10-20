"""Gerçek Diplomacia API istemcisi (DiplomaciaSource): oyuncu + parti + savaş listesi.

DOĞRULANAN (kullanıcı örnekleri / ağ izlemesi / DevTools): uç nokta adresleri, 200 yanıt şemaları
(bkz. ayristir.py), başlıksız isteğin 401 alması, kimlik biçimi: `Authorization: Bearer <token>` ve
`X-Client-Platform: web` (kullanıcı DevTools'ta doğruladı; CANLI BAĞLANTI BU KODLA TEST EDİLMEDİ).
DOĞRULANMADI (standart HTTP varsayımı; test adlarında `varsayim` etiketi var): 404 anlamı,
429 ve Retry-After, 403 anlamı (ErisimReddedildi: token geçersiz sayılmaz), diğer 4xx/5xx gövdeleri,
/members sayfalaması.
`DIPLOMACIA_AUTH_BASLIK` / `DIPLOMACIA_AUTH_SEMA` yalnız İSTEĞE BAĞLI override'dır (varsayılan Bearer).

Gizlilik: token yalnız istek anında TokenDeposu.oku() ile okunur; log/exception/repr'e
girmez. Hata mesajları sabit metindir (httpx istisna metni aktarılmaz). Token dosyası
yok/boş/geçersiz biçimdeyse HİÇ istek atılmaz (TokenYok; TokenGecersiz=401 DEĞİL).

Eşzamanlılık/zaman aşımı: bu sınıf kendi semaforunu KULLANMAZ; dışarıda tek kez
`SinirliKaynak(IstemciKaynagi(...))` ile sarılmalıdır (çift sarma yok). İstek başına
httpx zaman aşımı yine de vardır (varsayılan 10 sn).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from ..kaynak import (
    AgZamanAsimi,
    EksikVeri,
    ErisimReddedildi,
    IstekSiniri,
    KaynakHatasi,
    OyuncuAnlik,
    OzellikUygulanmadi,
    PartiAnlik,
    SavasDetay,
    SavasListesi,
    SiteErisilemez,
    TokenGecersiz,
    TokenYok,
    VarlikBulunamadi,
)
from ..tg.kaynak_yok import KaynakYapilandirilmadi
from ..tg.token import TokenDeposu
from . import ayristir, savas

log = logging.getLogger(__name__)

TABAN_ADRES = "https://diplomacia.com.tr"
VARSAYILAN_ZAMAN_ASIMI = 10.0
RETRY_AFTER_UST_SINIR = 300.0
MAKS_GOVDE_BAYT = 2_000_000
ENV_BASLIK = "DIPLOMACIA_AUTH_BASLIK"
ENV_SEMA = "DIPLOMACIA_AUTH_SEMA"
VARSAYILAN_BASLIK = "Authorization"
VARSAYILAN_SEMA = "Bearer"
PLATFORM_BASLIGI = {"X-Client-Platform": "web"}

_NUMARA = re.compile(r"[1-9][0-9]{0,11}")  # \d DEĞİL; baştaki sıfır reddedilir (kanonik biçim)
_BASLIK_ADI = re.compile(r"[A-Za-z0-9-]{1,64}")
_SEMA = re.compile(r"[A-Za-z0-9._~+/-]{0,32}")
_TOKEN_UYGUN = re.compile(r"[\x21-\x7e]+")  # görünür ASCII, boşluk/CR/LF yok
_BAGLANTI_YOLU = re.compile(r"/(player|party)/([1-9][0-9]{0,11})")


class KimlikYapilandirilmadi(KaynakYapilandirilmadi):
    """Kimlik doğrulama biçimi (başlık adı/şema) yapılandırılmadı/doğrulanmadı."""


class GecersizNumara(KaynakHatasi):
    """Yerel hata: numara yalnız rakam olmalı. Hiçbir istek atılmaz."""


class SavasSemasiDogrulanmadi(EksikVeri, OzellikUygulanmadi):
    """Savaş gövde şeması hiç görülmedi; uygulama yapılmadı."""


@runtime_checkable
class KimlikSaglayici(Protocol):
    yapilandirildi: bool

    def basliklar(self) -> dict[str, str]:
        """İstek başlıkları; yapılandırılmamışsa KimlikYapilandirilmadi, token yoksa TokenGecersiz."""
        ...


class OrtamKimlik:
    """Varsayılan: `Authorization: Bearer <token>` + `X-Client-Platform: web`.
    Başlık adı/şema ortam değişkenleriyle İSTEĞE BAĞLI override edilir. Token TokenDeposu'ndan okunur."""

    def __init__(self, depo: TokenDeposu, ortam: Mapping[str, str] | None = None):
        self._depo = depo
        self._ortam = os.environ if ortam is None else ortam

    def __repr__(self) -> str:
        return "OrtamKimlik(...)"

    def _biçim(self) -> tuple[str, str]:
        ad = (self._ortam.get(ENV_BASLIK) or "").strip() or VARSAYILAN_BASLIK
        sema_ham = self._ortam.get(ENV_SEMA)
        sema = VARSAYILAN_SEMA if sema_ham is None else sema_ham.strip()  # ayarlı-boş: yalnız token
        if not _BASLIK_ADI.fullmatch(ad) or not _SEMA.fullmatch(sema):
            raise KimlikYapilandirilmadi("kimlik doğrulama biçimi geçersiz (başlık adı/şema override)")
        return ad, sema

    @property
    def yapilandirildi(self) -> bool:
        try:
            self._biçim()
        except KimlikYapilandirilmadi:
            return False
        return True

    @property
    def token_hazir(self) -> bool:
        """Token dosyası var, boş değil ve biçimi uygun mu (istek atılabilir mi)? Token döndürmez."""
        t = self._depo.oku()
        return bool(t) and _TOKEN_UYGUN.fullmatch(t) is not None

    def basliklar(self) -> dict[str, str]:
        return self.basliklar_icin(self._depo.oku())

    def basliklar_icin(self, token: str | None) -> dict[str, str]:
        """Verilen (aday) token için başlıklar; depoya dokunmaz. Geçersiz override -> KimlikYapilandirilmadi;
        token yok/boş/biçim bozuk -> TokenYok (istek atılmaz)."""
        ad, sema = self._biçim()
        if not token:
            raise TokenYok("Diplomacia token'ı kayıtlı değil")
        if not _TOKEN_UYGUN.fullmatch(token):
            raise TokenYok("Diplomacia token'ı geçersiz karakter içeriyor")
        return {ad: f"{sema} {token}" if sema else token, **PLATFORM_BASLIGI}


def numara_dogrula(deger) -> str:
    if not isinstance(deger, str) or not _NUMARA.fullmatch(deger):
        raise GecersizNumara("numara 1-12 haneli pozitif sayı olmalı (baştaki sıfır olmadan)")
    return deger


def baglanti_ayristir(metin: str) -> tuple[str, str] | None:
    """`https://diplomacia.com.tr/player/{n}` veya `/party/{n}` -> ("player"|"party", n).

    Host tam eşleşmeli, şema https, sorgu/parça/kullanıcı bilgisi/port/ek yol yok. Aksi None.
    (tg/arayuz.py'nin gevşek ayrıştırıcısından daha katıdır; tg katmanı kendi kuralını korur.)
    """
    if not isinstance(metin, str):
        return None
    try:
        u = urlsplit(metin.strip())
        port = u.port
    except ValueError:
        return None
    if u.scheme != "https" or u.hostname != "diplomacia.com.tr" or port is not None:
        return None
    if "@" in u.netloc or u.query or u.fragment:
        return None
    m = _BAGLANTI_YOLU.fullmatch(u.path)
    return (m.group(1), m.group(2)) if m else None


def _retry_after(deger: str | None) -> float | None:
    """Yalnız tamsayı saniye. HTTP-date vb. -> None. Üst sınır var. (Davranış doğrulanmadı.)"""
    if not deger or not re.fullmatch(r"[0-9]{1,6}", deger.strip()):
        return None
    return float(min(int(deger.strip()), RETRY_AFTER_UST_SINIR))


class IstemciKaynagi:
    """DiplomaciaSource: oyuncu + parti gerçek; savaş reddedilir."""

    canli_dogrulanmadi = True  # arayüz durum ekranı bunu dürüstçe gösterir

    def __init__(
        self,
        kimlik: KimlikSaglayici,
        *,
        taban: str = TABAN_ADRES,
        transport: httpx.AsyncBaseTransport | None = None,
        zaman_asimi: float = VARSAYILAN_ZAMAN_ASIMI,
    ):
        self._kimlik = kimlik
        self._http = httpx.AsyncClient(
            base_url=taban,
            transport=transport,
            timeout=httpx.Timeout(zaman_asimi),
            follow_redirects=False,
            trust_env=False,
        )
        self._uuidler: dict[tuple[str, str], str] = {}  # yalnız bellek; yeniden başlatmada silinir

    @property
    def yapilandirildi(self) -> bool:
        """kaynak_yok.yapilandirildi_mi() bunu okur: kimlik biçimi yoksa 'bağlı değil'."""
        return bool(getattr(self._kimlik, "yapilandirildi", True))

    @property
    def token_hazir(self) -> bool:
        return bool(getattr(self._kimlik, "token_hazir", True))

    def __repr__(self) -> str:
        return "IstemciKaynagi(...)"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- HTTP ----
    async def _getir(self, yol: str):
        basliklar = self._kimlik.basliklar()  # yapılandırılmamışsa BURADA fırlar, istek yok
        hata = None
        try:
            r = await self._http.get(yol, headers=basliklar)
        except httpx.TimeoutException:
            hata = AgZamanAsimi("istek zaman aşımına uğradı")
        except httpx.HTTPError:
            hata = SiteErisilemez("site bağlantısı kurulamadı")
        if hata is not None:  # except dışında: __context__ zinciri httpx istisnasını taşımaz
            raise hata
        s = r.status_code
        if s == 200:
            if len(r.content) > MAKS_GOVDE_BAYT:
                raise EksikVeri("yanıt beklenenden büyük")
            try:
                return r.json()
            except ValueError:
                raise EksikVeri("yanıt JSON değil") from None
        if s == 401:  # DOĞRULANMADI: yalnız başlıksız istek 401 aldı; geçerli formatlı süresi dolmuş/sahte token'ın 401 verdiği doğrulanmadı
            raise TokenGecersiz("Diplomacia kimlik doğrulaması reddedildi (401)")
        if s == 403:  # anlamı doğrulanmadı: token geçersiz SAYILMAZ (kimlik biçimi veya site engeli olabilir)
            raise ErisimReddedildi("Diplomacia erişimi reddedildi (403)")
        if s == 404:  # doğrulanmadı, standart HTTP varsayımı
            raise VarlikBulunamadi("kayıt bulunamadı (404)")
        if s == 429:  # doğrulanmadı, standart HTTP varsayımı
            raise IstekSiniri("site istek sınırı (429)", _retry_after(r.headers.get("Retry-After")))
        if s >= 500:
            raise SiteErisilemez(f"site hatası ({s})")
        raise EksikVeri(f"beklenmeyen yanıt kodu ({s})")

    def _uuid_denetle(self, tur: str, numara: str, uuid: str) -> None:
        eski = self._uuidler.setdefault((tur, numara), uuid)
        if eski != uuid:
            raise EksikVeri("aynı numara için farklı UUID geldi (veri tutarsız)")

    # ---- DiplomaciaSource ----
    async def fetch_player(self, oyuncu_id: str) -> OyuncuAnlik:
        n = numara_dogrula(oyuncu_id)
        ayr = ayristir.oyuncu_ayristir(await self._getir(f"/api/players/{n}"), n)
        self._uuid_denetle("o", n, ayr.uuid)
        return ayr.anlik

    async def fetch_party(self, parti_id: str) -> PartiAnlik:
        n = numara_dogrula(parti_id)
        bilgi = ayristir.parti_ayristir(await self._getir(f"/api/parties/{n}"))
        if bilgi.parti_id != n:
            raise EksikVeri("yanıttaki parti numarası istekle uyuşmuyor")
        uyeler = ayristir.uyeler_ayristir(
            await self._getir(f"/api/parties/{n}/members"), bilgi.member_count
        )
        self._uuid_denetle("t", n, bilgi.uuid)
        for no, uid in uyeler.uuidler.items():
            self._uuid_denetle("o", no, uid)
        if not uyeler.tamam:
            log.info("parti %s üye listesi tam değil: %s", n, uyeler.neden)
        return ayristir.parti_birlestir(bilgi, uyeler)

    async def _savas_kayitlari(self) -> savas.SavasKayitListesi:
        return savas.savaslari_ayristir(await self._getir("/api/wars"))

    async def list_active_wars(self) -> SavasListesi:
        """GET /api/wars (kullanıcı örneğiyle şema; canlı doğrulama yok). Sayfalama anahtarı -> tamam=False."""
        return savas.savas_listesine_cevir(await self._savas_kayitlari())

    async def get_war_detail(self, savas_id: str) -> SavasDetay:
        """Ayrı ayrıntı adresi bilinmiyor: aynı `id` aktif listeden bulunur. Bulunamazsa
        VarlikBulunamadi('aktif listede yok'); 'silindi/bitti' DENMEZ (liste tamam değilse EksikVeri)."""
        liste = await self._savas_kayitlari()
        for k in liste.kayitlar:
            if k.savas_id == savas_id:
                return savas.savas_detayina_cevir(k)
        if not liste.tamam:
            raise EksikVeri("savaş listesi tamamlanmadı; savaş aranamadı")
        raise VarlikBulunamadi("savaş aktif listede yok")
