"""Saf ayrıştırıcılar: gerçek Diplomacia yanıt gövdesi (dict) -> normalize iç model. Ağ yok.

Yalnız API-ARASTIRMA.md §6'daki DOĞRULANMIŞ şemaya dayanır. Bir zorunlu anahtar yoksa veya
tür beklenenden farklıysa EksikVeri fırlatılır; sessiz varsayılan/boş sonuç üretilmez.

Alan kararları:
- ad = `username` (strip). `display_name` ASLA ad olmaz: parti etiketini içerir
  ("[TEKNO] Ad"); oyuncu parti değiştirince display_name değişir ve sahte "ad değişti"
  bildirimi üretirdi.
- bölge = `province_name`; vatandaşlık = `citizenship_country_name` (+ `citizenship_country_id`
  yalnız kimlik tutarlılığı için okunur). `country_*` alanları KULLANILMAZ.
- `province_name` / `citizenship_*` null veya boş string -> None ("bilinmiyor, değişiklik
  sayma"). null'ın anlamı (kayıt yok mu, kaldırıldı mı) DOĞRULANMADI; bu yüzden çekirdeğin
  None semantiğine eşlenir. Alan ANAHTARI hiç yoksa veya türü string/null değilse EksikVeri.
- Oyuncu yanıtında numara YOKTUR; çağıran URL numarasını girdi olarak verir ve
  `oyuncu_id` o numaradır. API'nin UUID'si OyuncuAyrinti.uuid içinde ayrıca taşınır.
"""
from __future__ import annotations

import math
import uuid as _uuid
from dataclasses import dataclass

from ..kaynak import EksikVeri, OyuncuAnlik, PartiAnlik, PartiUyesi

# Üye listesi yanıtında (üst düzey) bunlardan biri varsa tamlık doğrulanamaz.
SAYFALAMA_ANAHTARLARI = frozenset(
    {
        "next", "next_page", "nextPage", "next_cursor", "previous", "prev", "page", "pages",
        "page_size", "per_page", "limit", "offset", "cursor", "has_more", "hasMore", "total",
        "total_count", "total_pages", "links",
    }
)


@dataclass(frozen=True)
class OyuncuAyrinti:
    anlik: OyuncuAnlik
    uuid: str  # API'nin kalıcı oyuncu kimliği (tutarlılık denetimi için)
    vatandaslik_ulkesi_id: str | None = None


@dataclass(frozen=True)
class PartiBilgi:
    parti_id: str  # str(party_number)
    uuid: str
    party_number: int
    ad: str
    member_count: int


# Diplomacia /members uç noktası büyük partilerde TEK İSTEKTE 100 üye döndürür (gerçek VPS günlükleri: "100 != 329",
# "100 != 129"). Bu bir OKUMA sınırıdır, takip politikası DEĞİLDİR: parti başına oyuncu sayısı SINIRLANMAZ (tek
# kriter XP eşiği). Gerçek sayfalama yöntemi DOĞRULANMADI; kanıtlanmamış page/offset/limit parametresi
# GÖNDERİLMEZ. 100 kayıt + member_count > 100 -> "kısmi okuma" (geçerli ama EKSİK; tam liste sayılmaz).
# Sıralama (XP) varsayılmaz; yanıttaki xp alanları yalnız bilgi amaçlı denetlenir.
API_UYE_OKUMA_BOYUTU = 100
XP_UST = 10**12  # makul üst sınır; bunun üstü geçersiz sayılır


@dataclass(frozen=True)
class UyeListesi:
    uyeler: tuple[PartiUyesi, ...]
    uuidler: dict  # {player_number(str): player_id UUID}
    tamam: bool
    neden: str = ""
    kismi: bool = False  # geçerli ama EKSİK (API tek istekte API_UYE_OKUMA_BOYUTU üye döndürdü)
    xp_sirali: bool | None = None


# ---- yardımcılar ----------------------------------------------------------
def _nesne(v, ne: str) -> dict:
    if not isinstance(v, dict):
        raise EksikVeri(f"{ne}: nesne bekleniyordu")
    return v


def _zorunlu(d: dict, anahtar: str, ne: str):
    if anahtar not in d:
        raise EksikVeri(f"{ne}: '{anahtar}' alanı yok")
    return d[anahtar]


def _uuid_al(d: dict, anahtar: str, ne: str) -> str:
    v = _zorunlu(d, anahtar, ne)
    if not isinstance(v, str):
        raise EksikVeri(f"{ne}: '{anahtar}' metin olmalı")
    try:
        return str(_uuid.UUID(v))
    except (ValueError, AttributeError):
        raise EksikVeri(f"{ne}: '{anahtar}' geçerli UUID değil") from None


def _tamsayi(d: dict, anahtar: str, ne: str) -> int:
    v = _zorunlu(d, anahtar, ne)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise EksikVeri(f"{ne}: '{anahtar}' negatif olmayan tamsayı olmalı")
    return v


def _dolu_metin(d: dict, anahtar: str, ne: str) -> str:
    v = _zorunlu(d, anahtar, ne)
    if not isinstance(v, str) or not v.strip():
        raise EksikVeri(f"{ne}: '{anahtar}' dolu metin olmalı")
    return v.strip()


def _bos_olabilir_metin(d: dict, anahtar: str, ne: str) -> str | None:
    """Anahtar ZORUNLU; değer null/boş -> None (bilinmiyor); metin dışı tür -> EksikVeri."""
    v = _zorunlu(d, anahtar, ne)
    if v is None:
        return None
    if not isinstance(v, str):
        raise EksikVeri(f"{ne}: '{anahtar}' metin veya null olmalı")
    return v.strip() or None


def _json_nesne(govde, ne: str) -> dict:
    return _nesne(govde, ne)


# ---- oyuncu ---------------------------------------------------------------
def oyuncu_ayristir(govde, numara: str) -> OyuncuAyrinti:
    """`{"player": {...}}` gövdesi. `numara` = isteğin URL numarası (yanıtta yok)."""
    p = _nesne(_zorunlu(_json_nesne(govde, "oyuncu yanıtı"), "player", "oyuncu yanıtı"), "player")
    uid = _uuid_al(p, "id", "player")
    ad = _dolu_metin(p, "username", "player")  # display_name KULLANILMAZ
    bolge = _bos_olabilir_metin(p, "province_name", "player")
    ulke = _bos_olabilir_metin(p, "citizenship_country_name", "player")
    ulke_id = _bos_olabilir_metin(p, "citizenship_country_id", "player")
    return OyuncuAyrinti(
        anlik=OyuncuAnlik(oyuncu_id=str(numara), ad=ad, bolge=bolge, vatandaslik_ulkesi=ulke, uid=uid),
        uuid=uid,
        vatandaslik_ulkesi_id=ulke_id,
    )


# ---- parti ----------------------------------------------------------------
def parti_ayristir(govde) -> PartiBilgi:
    """`{"party": {...}}` gövdesi: id, party_number, name, member_count zorunlu."""
    p = _nesne(_zorunlu(_json_nesne(govde, "parti yanıtı"), "party", "parti yanıtı"), "party")
    return PartiBilgi(
        parti_id=str(_tamsayi(p, "party_number", "party")),
        uuid=_uuid_al(p, "id", "party"),
        party_number=_tamsayi(p, "party_number", "party"),
        ad=_dolu_metin(p, "name", "party"),
        member_count=_tamsayi(p, "member_count", "party"),
    )


def uyeler_ayristir(govde, beklenen_sayi: int) -> UyeListesi:
    """`{"members":[...]}` gövdesi. Bozuk üye -> EksikVeri (sessizce atlanmaz).

    tamam=True yalnız: sayfalama anahtarı yok, len==beklenen_sayi (BOYUTA bakılmaksızın, KESİLMEZ), player_number
    ve player_id mükerrer değil. kismi=True (tamam=False) yalnız: sayfalama anahtarı yok, mükerrer yok, TAM
    API_UYE_OKUMA_BOYUTU kayıt var ve beklenen_sayi bundan büyük (okunmayan üyeler VAR). Başka her uyuşmazlık
    tamam=False, kismi=False (açıklanamayan eksik liste reddedilir, çıkış sayılmaz).
    """
    g = _json_nesne(govde, "üye yanıtı")
    liste = _zorunlu(g, "members", "üye yanıtı")
    if not isinstance(liste, list):
        raise EksikVeri("üye yanıtı: 'members' dizi olmalı")
    uyeler: list[PartiUyesi] = []
    uuidler: dict = {}
    gorulen_no: set = set()
    gorulen_uuid: set = set()
    xp_listesi: list = []
    mukerrer = False
    for i, m in enumerate(liste):
        ne = f"members[{i}]"
        m = _nesne(m, ne)
        pid = _uuid_al(m, "player_id", ne)
        no = _tamsayi(m, "player_number", ne)
        ad = _dolu_metin(m, "username", ne)
        if no in gorulen_no or pid in gorulen_uuid:
            mukerrer = True
        gorulen_no.add(no)
        gorulen_uuid.add(pid)
        xp = _xp(m.get("xp"))
        uyeler.append(PartiUyesi(oyuncu_id=str(no), ad=ad, xp=xp))
        uuidler[str(no)] = pid
        xp_listesi.append(xp)
    neden = ""
    kismi = False
    sayfalama = sorted(SAYFALAMA_ANAHTARLARI & set(g))
    if sayfalama:
        neden = f"sayfalama anahtarı var {sayfalama}: tamlık doğrulanamaz"
    elif mukerrer:
        neden = "mükerrer üye"
    elif len(liste) != beklenen_sayi:
        if len(liste) == API_UYE_OKUMA_BOYUTU and beklenen_sayi > API_UYE_OKUMA_BOYUTU:
            kismi = True  # API tek istekte 100 üye döndürdü; kalanı için sayfa parametresi UYDURULMAZ
        else:
            neden = f"üye sayısı uyuşmuyor ({len(liste)} != {beklenen_sayi})"
    return UyeListesi(
        tuple(uyeler), uuidler, tamam=not neden and not kismi, neden=neden, kismi=kismi,
        xp_sirali=_xp_sirali(xp_listesi) if kismi else None,
    )


def _xp(v) -> int | None:
    """XP'yi güvenilir ayrıştırır: negatif olmayan tamsayı (veya tam sayı değerli sonlu kayan nokta) -> int.
    Eksik, bool, metin ('1500000'), negatif, kesirli, NaN/inf, aşırı büyük -> None (BİLİNMİYOR; 0 sayılmaz)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not math.isfinite(v) or not v.is_integer():
            return None
        v = int(v)
    if not isinstance(v, int) or v < 0 or v > XP_UST:
        return None
    return v


def _xp_sirali(xp: list) -> bool | None:
    """Tüm üyelerde tamsayı xp varsa azalmayan (büyükten küçüğe) sıra mı; eksik/bozuk xp -> None (bilinmiyor)."""
    if not xp or any(isinstance(x, bool) or not isinstance(x, int) for x in xp):
        return None
    return all(xp[i] >= xp[i + 1] for i in range(len(xp) - 1))


def parti_birlestir(bilgi: PartiBilgi, uyeler: UyeListesi) -> PartiAnlik:
    return PartiAnlik(
        parti_id=bilgi.parti_id, ad=bilgi.ad, uyeler=uyeler.uyeler, uyeler_tamam=uyeler.tamam,
        kismi=uyeler.kismi, toplam_uye=bilgi.member_count, xp_sirali=uyeler.xp_sirali,
    )
