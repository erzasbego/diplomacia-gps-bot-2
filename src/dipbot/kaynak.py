"""Veri kaynağı arayüzü ve NORMALİZE iç model.

Buradaki alan adları gerçek Diplomacia JSON şeması DEĞİLDİR. Gerçek yanıtların bu
modele eşlenmesi sonra `api` ajanı tarafından, şema örnekleri geldikten sonra yapılacak.
Kaynak gerçeğinde eksik/belirsiz bir durumda EksikVeri fırlatmalı; "boş" döndürüp
sessizce değişiklik gibi görünmemelidir.

Savaş listesi sözleşmesi (list_active_wars): `SavasListesi(savaslar, tamam)` döner.
`tamam=True` YALNIZ tüm sayfalar başarıyla okunduysa verilir. Sayfalama yarıda kaldıysa,
yanıt kesildiyse veya şüpheli ise adaptör ya EksikVeri fırlatmalı ya da `tamam=False`
döndürmelidir; çekirdek tamam=False listeyi baseline/bildirim için KULLANMAZ.
Boş ama tamam=True liste "gerçekten aktif savaş yok" demektir.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from . import config


# ---- Hata sınıfları -------------------------------------------------------
class KaynakHatasi(Exception):
    """Tüm kaynak hatalarının tabanı."""


class TokenGecersiz(KaynakHatasi):
    """Token geçersiz veya süresi dolmuş."""


class AgZamanAsimi(KaynakHatasi):
    """Ağ zaman aşımı."""


class SiteErisilemez(KaynakHatasi):
    """Site geçici olarak erişilemiyor."""


class ErisimReddedildi(SiteErisilemez):
    """HTTP 403: token geçersiz DEĞİL (anlamı doğrulanmadı: kimlik biçimi veya site engeli olabilir).
    Geri çekilme uygulanır; global token duraklatması ve 'token geçersiz' bildirimi YOK."""


class IstekSiniri(KaynakHatasi):
    """Site istek sınırı (429 benzeri)."""

    def __init__(self, mesaj: str = "", bekle_saniye: float | None = None):
        super().__init__(mesaj)
        self.bekle_saniye = bekle_saniye


IstekSsiniri = IstekSiniri  # görev metnindeki yazım için takma ad


class EksikVeri(KaynakHatasi):
    """Yanıt eksik, beklenmeyen veya sayfalaması tamamlanmamış."""


class OzellikUygulanmadi(KaynakHatasi):
    """Kaynak bu işi (henüz) desteklemiyor/doğrulanmadı: geçici DEĞİL, yapılandırma/uygulama eksiği.
    Zamanlayıcı bunu grup geri çekilmesine saymaz ve log yağmuru üretmez."""


class TokenYok(OzellikUygulanmadi):
    """Token dosyası yok/boş/geçersiz biçimde: HİÇ istek atılmaz. TokenGecersiz (401) DEĞİLDİR;
    yapılandırma sınıfıdır (geri çekilme yok, iş tipi başına tek uyarı)."""


class VarlikBulunamadi(KaynakHatasi):
    """Oyuncu/parti/savaş doğrulanmış biçimde artık yok."""


_ACIKLAMA = {
    TokenYok: "token dosyası yok veya geçersiz biçimde (istek atılmadı)",
    ErisimReddedildi: "erişim reddedildi (403): kimlik biçimi veya site engeli olabilir",
    TokenGecersiz: "site token'ı reddetti (401)",
    AgZamanAsimi: "ağ zaman aşımı",
    SiteErisilemez: "site geçici olarak erişilemiyor",
    IstekSiniri: "site istek sınırına takıldı",
    EksikVeri: "eksik veya beklenmeyen veri",
    VarlikBulunamadi: "kayıt bulunamadı",
}


def hata_aciklamasi(exc: BaseException) -> str:
    ozel = getattr(exc, "kullanici_mesaji", None)  # arayüz ön kontrolü açık kullanıcı mesajı ekleyebilir
    if ozel:
        return ozel
    for tip, metin in _ACIKLAMA.items():
        if isinstance(exc, tip):
            return metin
    return "bilinmeyen kaynak hatası"


# ---- Sonuç tipleri --------------------------------------------------------
@dataclass(frozen=True)
class OyuncuAnlik:
    oyuncu_id: str
    ad: str | None
    bolge: str | None  # mevcut bölge
    vatandaslik_ulkesi: str | None  # bölgeden AYRI bilgi
    uid: str | None = None  # kaynağın kalıcı kimliği (UUID); yoksa None. Numara-UUID uyuşmazlığı tespiti için.


@dataclass(frozen=True)
class PartiUyesi:
    oyuncu_id: str
    ad: str | None = None
    # Üyenin XP'si (yanıttaki `xp`). None = eksik/geçersiz -> XP BİLİNMİYOR (otomatik takibe ALINMAZ; 0 değildir).
    xp: int | None = None


@dataclass(frozen=True)
class PartiAnlik:
    parti_id: str
    ad: str | None
    uyeler: tuple[PartiUyesi, ...]
    uyeler_tamam: bool  # tüm sayfalar okundu mu (SINIRLI listede False)
    # Büyük parti: API yalnız ilk N (şu an 100) üyeyi döndürür. sinirli=True "geçerli ama kısmi" demektir:
    # liste bozuk/mükerrer/sayfalamalı DEĞİL ve len(uyeler) < toplam_uye. Bu liste partinin TÜM üyeleriyle EŞDEĞER
    # sayılmaz: ondan ayrılma/katılma çıkarılamaz (sıralama değişimi ≠ üyelik değişimi).
    sinirli: bool = False
    toplam_uye: int | None = None  # partinin gerçek üye sayısı (member_count)
    xp_sirali: bool | None = None  # yanıttaki xp alanları azalmayan sırada mı (None: doğrulanamadı)


@dataclass(frozen=True)
class SavasOzet:
    """savas_id = kalıcı kimlik (UUID); war_number ayrı alan (bağlantı için). None = veri yok (0 değil)."""

    savas_id: str
    ad: str | None = None
    durum: str | None = None  # ham status
    durum_taninmiyor: bool = False
    tur: str | None = None  # ham war_type
    tur_taninmiyor: bool = False
    war_number: int | None = None
    attacker_name: str | None = None
    defender_name: str | None = None
    attacker_power: int | None = None
    defender_power: int | None = None


@dataclass(frozen=True)
class SavasListesi:
    savaslar: tuple[SavasOzet, ...]
    tamam: bool  # tüm sayfalar okundu mu (False: liste eksik olabilir)

    def __post_init__(self):
        object.__setattr__(self, "savaslar", tuple(self.savaslar))


def metin_temizle(v) -> str | None:
    """Kaynak metin alanı normalizasyonu (TEK yer): None/boş/yalnız boşluk -> None, aksi halde strip."""
    if v is None:
        return None
    t = str(v).strip()
    return t or None


@dataclass(frozen=True)
class SavasTaraf:
    ad: str | None
    guc: int | None = None  # kaynaktaki 'power'; None = bilinmiyor (0 değildir). Hasar OLDUĞU doğrulanmadı.
    hasar: int | None = None  # eski önbellek kayıtları için; kaynak adaptörü doldurmaz, ekranda gösterilmez


@dataclass(frozen=True)
class SavasDetay:
    savas_id: str
    ad: str | None
    durum: str | None
    taraflar: tuple[SavasTaraf, ...] = field(default_factory=tuple)
    alinma_zamani: datetime | None = None
    war_number: int | None = None
    tur: str | None = None
    durum_taninmiyor: bool = False
    tur_taninmiyor: bool = False


@runtime_checkable
class DiplomaciaSource(Protocol):
    async def fetch_player(self, oyuncu_id: str) -> OyuncuAnlik: ...

    async def fetch_party(self, parti_id: str) -> PartiAnlik: ...

    async def list_active_wars(self) -> SavasListesi: ...

    async def get_war_detail(self, savas_id: str) -> SavasDetay: ...


# ---- Eşzamanlılık sınırı + zaman aşımı sarmalayıcısı ----------------------
class SinirliKaynak:
    """Herhangi bir DiplomaciaSource'u eşzamanlı istek sınırı ve zaman aşımıyla sarar."""

    def __init__(
        self,
        ic: DiplomaciaSource,
        esler: int = config.SOURCE_MAX_CONCURRENT_REQUESTS,
        zaman_asimi: float = config.SOURCE_REQUEST_TIMEOUT_SECONDS,
        kusak_fn=None,
    ):
        self._ic = ic
        self._sem = asyncio.Semaphore(esler)
        self._zaman_asimi = zaman_asimi
        self._kusak_fn = kusak_fn  # token kuşağı sayacı (zamanlayıcıdan); None = izlenmez

    async def _cagir(self, coro_fn, *args):
        # Kuşak istek BAŞLAMADAN yakalanır: uçuştaki eski tokenın 401'i yeni tokenı duraklatmasın.
        kusak = self._kusak_fn() if self._kusak_fn is not None else None
        async with self._sem:
            try:
                return await asyncio.wait_for(coro_fn(*args), self._zaman_asimi)
            except asyncio.TimeoutError as e:
                raise AgZamanAsimi("istek zaman aşımına uğradı") from e
            except TokenGecersiz as e:
                e.kusak = kusak  # type: ignore[attr-defined]
                raise

    async def fetch_player(self, oyuncu_id):
        return await self._cagir(self._ic.fetch_player, oyuncu_id)

    async def fetch_party(self, parti_id):
        return await self._cagir(self._ic.fetch_party, parti_id)

    async def list_active_wars(self):
        return await self._cagir(self._ic.list_active_wars)

    async def get_war_detail(self, savas_id):
        return await self._cagir(self._ic.get_war_detail, savas_id)


class ZamanlayiciKorumali:
    """Arayüz (kullanıcı kaynaklı) istekleri için: zamanlayıcının PAYLAŞILAN SinirliKaynak'ı üstünde,
    ağa çıkmadan önce zamanlayıcının duraklatma/istek-sınırı/403 geri çekilmesini denetler (açık
    kullanıcı mesajıyla hata fırlatır) ve yakalanan TokenGecersiz/IstekSiniri/ErisimReddedildi'yi
    zamanlayıcıya iletir (tek bildirim, ortak pencere)."""

    def __init__(self, sinirli, zamanlayici):
        self._sinirli = sinirli
        self._zam = zamanlayici

    @property
    def _ic(self):  # yapilandirildi_mi / token_hazir gibi ham kaynak bayrakları için şeffaf
        return self._sinirli._ic

    async def _cagir(self, ad, *args):
        self._zam.ag_onkontrol()
        try:
            return await getattr(self._sinirli, ad)(*args)
        except TokenGecersiz as e:
            self._zam.token_gecersiz_bildir(getattr(e, "kusak", None))
            raise
        except IstekSiniri as e:
            self._zam.istek_siniri_bildir(e)
            raise
        except ErisimReddedildi:
            self._zam.erisim_reddi_bildir()
            raise

    async def fetch_player(self, oyuncu_id):
        return await self._cagir("fetch_player", oyuncu_id)

    async def fetch_party(self, parti_id):
        return await self._cagir("fetch_party", parti_id)

    async def list_active_wars(self):
        return await self._cagir("list_active_wars")

    async def get_war_detail(self, savas_id):
        return await self._cagir("get_war_detail", savas_id)


class TaramaOnbellegi:
    """Tek tarama turu boyunca aynı oyuncu/parti isteğini gruplar arasında paylaşır.

    Yalnızca ham kaynak yanıtını paylaşır; gruplar kendi durumlarını ayrı tutar.
    """

    def __init__(self, ic: DiplomaciaSource):
        self._ic = ic
        self._g: dict[tuple, asyncio.Future] = {}

    async def _paylas(self, anahtar, coro_fn, *args):
        gorev = self._g.get(anahtar)
        if gorev is None:
            gorev = asyncio.ensure_future(coro_fn(*args))
            self._g[anahtar] = gorev
        return await asyncio.shield(gorev)

    async def fetch_player(self, oyuncu_id):
        return await self._paylas(("p", oyuncu_id), self._ic.fetch_player, oyuncu_id)

    async def fetch_party(self, parti_id):
        return await self._paylas(("t", parti_id), self._ic.fetch_party, parti_id)

    async def list_active_wars(self):
        return await self._paylas(("w",), self._ic.list_active_wars)

    async def get_war_detail(self, savas_id):
        return await self._paylas(("d", savas_id), self._ic.get_war_detail, savas_id)
