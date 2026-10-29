"""Çok hesaplı mimari çekirdeği: hesaplar (DB, gizli olmayan bilgi), sır deposu (0600 dosyalar), oyuncu görev ataması, hesap kaynağı.

SIR POLİTİKASI
- JWT token ve proxy (adres, kullanıcı adı, parola) YALNIZ `<veri dizini>/hesaplar/<hesap_id>/` altındaki dosyalarda tutulur
  (dizinler 0700, dosyalar 0600). SQLite'ta, loglarda, hata metinlerinde, Telegram yanıtlarında ve kanalda ASLA görünmezler.
- Token/proxy değişiminde ÖNCE aday yapılandırma gerçekten sınanır (aday proxy üzerinden, aday token ile); başarısızsa mevcut geçerli
  yapılandırma ve dosyalar DEĞİŞMEZ.
- Proxy'li bir hesap proxy'ye ulaşamazsa ProxyErisilemez fırlatılır; ASLA doğrudan bağlantıya geçilmez.

GÖREV ATAMASI (HESAP_KAPASITESI = 150, sabit görev kapasitesi; API kotası DEĞİLDİR)
- Hesaplar hesap_id sırasıyla DOLDURULUR: 205 oyuncu -> 150/55; 301 oyuncu ve 2 hesap -> 150/150 + 1 beklemede; 3. hesap eklenince 150/150/1.
- Kapasite yoksa oyuncu silinmez: atama satırı hesap_id=NULL (beklemede) kalır; hesap eklenince otomatik atanır. Hesap OTOMATİK oluşturulmaz.
- Hesaplar arasında devralma yoktur: arızalı hesabın görevleri atanmış olduğu hesapta bekler. Atama değişikliği oyuncu geçmişini sıfırlamaz.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import config
from .db import islem
from .kaynak import (
    AgZamanAsimi,
    ErisimReddedildi,
    IstekSiniri,
    KaynakHatasi,
    OzellikUygulanmadi,
    ProxyErisilemez,
    SiteErisilemez,
    TokenGecersiz,
    TokenYok,
)
from .maske import sir_kaydet
from .tg.token import DogrulamaSonucu, TokenDeposu
from .zaman import Saat, iso, iso_coz, simdi_utc

log = logging.getLogger("dipbot.hesaplar")

DURUM_HAZIR = "hazir"
DURUM_TOKEN_YOK = "token_yok"
DURUM_TOKEN_GECERSIZ = "token_gecersiz"
DURUM_PROXY_HATASI = "proxy_hatasi"
DURUM_ERISIM_REDDI = "erisim_reddi"
DURUM_RATE_LIMIT = "rate_limit"
DURUM_HATA = "hata"
SORUNLU_DURUMLAR = frozenset({DURUM_TOKEN_YOK, DURUM_TOKEN_GECERSIZ, DURUM_PROXY_HATASI, DURUM_ERISIM_REDDI, DURUM_RATE_LIMIT, DURUM_HATA})
DURUM_ETIKET = {
    DURUM_HAZIR: "ready",
    DURUM_TOKEN_YOK: "no token",
    DURUM_TOKEN_GECERSIZ: "token rejected (401)",
    DURUM_PROXY_HATASI: "proxy unreachable",
    DURUM_ERISIM_REDDI: "access denied (403) backoff",
    DURUM_RATE_LIMIT: "rate limit cooldown",
    DURUM_HATA: "error",
}

HESAP_AZAMI = 12
_AD_DESENI = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}")


class HesapHatasi(Exception):
    """Kullanıcıya gösterilebilir, SIR İÇERMEYEN hesap/yapılandırma hatası."""


# ---------------------------------------------------------------- doğrulayıcılar
def ad_dogrula(ad: str) -> str:
    ad = (ad or "").strip()
    if not _AD_DESENI.fullmatch(ad):
        raise HesapHatasi("Account names use 1-32 letters, digits, spaces, dots, underscores or dashes (e.g. Main or Account 2).")
    return ad


@dataclass(frozen=True)
class ProxyAyar:
    url: str = ""  # SIR: kullanıcı adı/parola içerebilir; repr/log/yanıtlara girmez
    sema: str = ""
    egress: str = ""  # host:port özeti (paylaşılan IP kotası için kıyas); geri çevrilemez, gösterilmez

    def __repr__(self) -> str:
        return f"ProxyAyar(sema={self.sema!r})"


def proxy_ayristir(metin: str) -> ProxyAyar:
    """`http://[kullanici:parola@]host:port` veya https. Yol/sorgu/parça yok; port ZORUNLU. Hata metni girdiyi YANSITMAZ."""
    hata = HesapHatasi("Send the proxy as http://user:password@host:port (http or https, port required, nothing else).")
    metin = (metin or "").strip()
    if not metin or any(c.isspace() for c in metin) or len(metin) > 512:
        raise hata
    try:
        u = urlsplit(metin)
        port = u.port
    except ValueError:
        raise hata from None
    if u.scheme not in ("http", "https") or not u.hostname or port is None or not (0 < port < 65536):
        raise hata
    if u.path not in ("", "/") or u.query or u.fragment:
        raise hata
    kimlik = ""
    if u.username is not None:
        kimlik = quote(unquote(u.username), safe="")
        if u.password is not None:
            kimlik += ":" + quote(unquote(u.password), safe="")
        kimlik += "@"
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    url = f"{u.scheme}://{kimlik}{host}:{port}"
    try:
        httpx.Proxy(url)
    except Exception:
        raise hata from None
    if u.password:
        sir_kaydet(unquote(u.password))
    sir_kaydet(url)
    egress = hashlib.sha256(f"{u.hostname.lower()}:{port}".encode()).hexdigest()[:12]
    return ProxyAyar(url, u.scheme, egress)


def token_biçimi_dogrula(token: str) -> str:
    token = (token or "").strip()
    if not re.fullmatch(r"[\x21-\x7e]{16,4096}", token):
        raise HesapHatasi("That does not look like a JWT token (one line, no spaces).")
    return token


# ---------------------------------------------------------------- sır deposu (0600 dosyalar, 0700 dizinler)
class HesapDeposu:
    def __init__(self, kok: str | Path):
        self.kok = Path(kok)

    def dizin(self, hesap_id: int) -> Path:
        return self.kok / str(int(hesap_id))

    def hazirla(self, hesap_id: int) -> Path:
        """Kök ve hesap dizini 0700 olarak var olur/yeniden kısıtlanır."""
        for d in (self.kok, self.dizin(hesap_id)):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        return self.dizin(hesap_id)

    def token(self, hesap_id: int) -> TokenDeposu:
        return TokenDeposu(self.dizin(hesap_id) / "token")

    def proxy_yolu(self, hesap_id: int) -> Path:
        return self.dizin(hesap_id) / "proxy"

    def proxy_oku(self, hesap_id: int) -> ProxyAyar | None:
        try:
            veri = json.loads(self.proxy_yolu(hesap_id).read_text(encoding="utf-8"))
            return proxy_ayristir(veri["url"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, HesapHatasi):
            return None  # bozuk proxy dosyası: yok sayma DEĞİL, çağıran 'proxy_var' ile uyuşmazlığı hata sayar

    def proxy_yaz(self, hesap_id: int, ayar: ProxyAyar) -> None:
        self.hazirla(hesap_id)
        yol = self.proxy_yolu(hesap_id)
        TokenDeposu(yol)._atomik_yaz(yol, json.dumps({"url": ayar.url}).encode("utf-8"))

    def proxy_sil(self, hesap_id: int) -> None:
        try:
            self.proxy_yolu(hesap_id).unlink()
        except FileNotFoundError:
            pass

    def hesabi_sil_dosyalari(self, hesap_id: int) -> None:
        d = self.dizin(hesap_id)
        if d.is_dir():
            for p in d.iterdir():
                try:
                    p.unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass

    def izin_sorunlari(self) -> list[str]:
        """Beklenen: dizinler 0700, dosyalar 0600. Sorun varsa sır İÇERMEYEN kısa açıklamalar (yol + mod)."""
        sorun = []
        if not self.kok.exists():
            return sorun
        for p in [self.kok, *self.kok.rglob("*")]:
            try:
                mod = p.lstat().st_mode & 0o777
            except OSError:
                continue
            beklenen = 0o700 if p.is_dir() else 0o600
            if mod & 0o077 or mod != beklenen and not p.is_dir():
                sorun.append(f"{p}: mode {oct(mod)} (expected {oct(beklenen)})")
        return sorun


# ---------------------------------------------------------------- hız/kota koruması (hesap + genel)
class GenelSinir:
    """TÜM hesapların ORTAK sınırı: toplam eşzamanlı istek ve istekler arası en az süre. Hesap eklemek genel kotayı ARTIRMAZ.
    (Rate limit/erişim kontrollerini aşmaya yönelik IP/hesap değiştirme YOKTUR.)"""

    def __init__(self, esler: int = config.GENEL_ESZAMANLI, aralik: float = config.GENEL_ISTEK_ARALIGI_SN):
        self.sem = asyncio.Semaphore(max(1, esler))
        self.aralik = aralik
        self._son = 0.0
        self._kilit = asyncio.Lock()

    async def sira(self) -> None:
        async with self._kilit:
            bekle = self._son + self.aralik - time.monotonic()
            if bekle > 0:
                await asyncio.sleep(bekle)
            self._son = time.monotonic()


class HesapKaynagi:
    """Tek hesabın DiplomaciaSource'u: genel + hesap eşzamanlılığı, istekler arası pacing, zaman aşımı, istatistik. Başarısızlıkta hata
    olduğu gibi (token kuşağıyla) yükselir; BAŞKA HESABA veya doğrudan bağlantıya GEÇİLMEZ."""

    def __init__(self, yonetici: "HesapYoneticisi", hesap_id: int, ic, genel: GenelSinir, *,
                 esler: int = config.HESAP_ESZAMANLI, aralik: float = config.HESAP_ISTEK_ARALIGI_SN,
                 zaman_asimi: float = config.SOURCE_REQUEST_TIMEOUT_SECONDS):
        self._yon = yonetici
        self.hesap_id = hesap_id
        self._ic = ic
        self._genel = genel
        self._sem = asyncio.Semaphore(max(1, esler))
        self._aralik = aralik
        self._son = 0.0
        self._kilit = asyncio.Lock()
        self._zaman_asimi = zaman_asimi
        self.token_kusagi = 0

    def __repr__(self) -> str:
        return f"HesapKaynagi({self.hesap_id})"

    @property
    def yapilandirildi(self) -> bool:
        return bool(getattr(self._ic, "yapilandirildi", True))

    @property
    def token_hazir(self) -> bool:
        return bool(getattr(self._ic, "token_hazir", True))

    async def aclose(self) -> None:
        kapat = getattr(self._ic, "aclose", None)
        if kapat is not None:
            await kapat()

    async def _cagir(self, ad: str, *args):
        kusak = self.token_kusagi
        async with self._genel.sem, self._sem:
            async with self._kilit:
                bekle = self._son + self._aralik - time.monotonic()
                if bekle > 0:
                    await asyncio.sleep(bekle)
                self._son = time.monotonic()
            await self._genel.sira()
            try:
                sonuc = await asyncio.wait_for(getattr(self._ic, ad)(*args), self._zaman_asimi)
            except asyncio.TimeoutError as e:
                raise AgZamanAsimi("request timed out") from e
            except TokenGecersiz as e:
                e.kusak = kusak  # type: ignore[attr-defined]
                raise
        self._yon._api_basari(self.hesap_id)
        return sonuc

    async def fetch_player(self, oyuncu_id):
        return await self._cagir("fetch_player", oyuncu_id)

    async def fetch_party(self, parti_id):
        return await self._cagir("fetch_party", parti_id)

    async def list_active_wars(self):
        return await self._cagir("list_active_wars")

    async def get_war_detail(self, savas_id):
        return await self._cagir("get_war_detail", savas_id)


class BirincilKaynak:
    """Etkileşimli istekler (komutlar, parti/savaş taramaları) için: hesap_id'si EN KÜÇÜK olan hesabın kaynağı. Hesap yoksa TokenYok
    (yapılandırma sınıfı: istek atılmaz). Birincil hesap bozuksa BAŞKA hesaba GEÇİLMEZ."""

    def __init__(self, yonetici: "HesapYoneticisi"):
        self._yon = yonetici

    def _k(self):
        h = self._yon.birincil()
        if h is None:
            raise TokenYok("No Diplomacia account is configured (Account Management → Add Account)")
        return self._yon.kaynak(h["hesap_id"])

    @property
    def yapilandirildi(self) -> bool:
        return True

    @property
    def token_hazir(self) -> bool:
        h = self._yon.birincil()
        return h is not None and self._yon.token_var(h["hesap_id"])

    @property
    def _ic(self):
        return self

    async def fetch_player(self, oyuncu_id):
        return await self._k().fetch_player(oyuncu_id)

    async def fetch_party(self, parti_id):
        return await self._k().fetch_party(parti_id)

    async def list_active_wars(self):
        return await self._k().list_active_wars()

    async def get_war_detail(self, savas_id):
        return await self._k().get_war_detail(savas_id)

    async def aclose(self) -> None:
        await self._yon.kapat()


# ---------------------------------------------------------------- yönetici
def _sirali_pozisyon_anahtari(r) -> tuple:
    return (r["eklendi"] or "", len(r["oyuncu_id"]), r["oyuncu_id"])


class HesapYoneticisi:
    def __init__(self, conn: sqlite3.Connection, saat: Saat = simdi_utc, kok: str | Path = "hesaplar", *,
                 istemci_fabrikasi=None, dogrulayici_fabrikasi=None, kapasite: int | None = None,
                 genel_sinir: GenelSinir | None = None, hesap_aralik: float | None = None, zaman_asimi: float | None = None,
                 dogrulama_oyuncusu: str = "72400"):
        self.conn = conn
        self.dogrulama_oyuncusu = dogrulama_oyuncusu  # token/proxy doğrulamasında sorgulanan herkese açık oyuncu (DIPLOMACIA_DOGRULAMA_OYUNCU)
        self.saat = saat
        self.depo = HesapDeposu(kok)
        self.kapasite = kapasite if kapasite is not None else config.HESAP_KAPASITESI
        self._istemci_fabrikasi = istemci_fabrikasi or self._varsayilan_istemci
        self._dogrulayici_fabrikasi = dogrulayici_fabrikasi or self._varsayilan_dogrulayici
        self._genel = genel_sinir or GenelSinir()
        self._hesap_aralik = hesap_aralik if hesap_aralik is not None else config.HESAP_ISTEK_ARALIGI_SN
        self._zaman_asimi = zaman_asimi if zaman_asimi is not None else config.SOURCE_REQUEST_TIMEOUT_SECONDS
        self._kaynaklar: dict[int, HesapKaynagi] = {}
        self._token_kusaklari: dict[int, int] = {}

    # ---- fabrikalar (testlerde sahte)
    @staticmethod
    def _varsayilan_istemci(hesap_id: int, proxy: ProxyAyar | None, token_deposu: TokenDeposu):
        from .diplomacia.istemci import IstemciKaynagi, OrtamKimlik

        return IstemciKaynagi(OrtamKimlik(token_deposu), proxy=proxy.url if proxy else None)

    def _varsayilan_dogrulayici(self, proxy: ProxyAyar | None):
        from .diplomacia.dogrulayici import OyuncuDogrulayici
        from .diplomacia.istemci import OrtamKimlik

        return OyuncuDogrulayici(OrtamKimlik(TokenDeposu(os.devnull)), proxy=proxy.url if proxy else None,
                                 oyuncu_no=self.dogrulama_oyuncusu)

    # ---- okuma
    def listele(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM hesaplar ORDER BY hesap_id").fetchall()

    def getir(self, hesap_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM hesaplar WHERE hesap_id=?", (hesap_id,)).fetchone()

    def birincil(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM hesaplar ORDER BY hesap_id LIMIT 1").fetchone()

    def token_var(self, hesap_id: int) -> bool:
        return self.depo.token(hesap_id).var_mi()

    def egress_anahtari(self, hesap_id: int) -> str:
        p = self.depo.proxy_oku(hesap_id)
        return p.egress if p else "direct"

    # ---- kaynak
    def kaynak(self, hesap_id: int) -> HesapKaynagi:
        k = self._kaynaklar.get(hesap_id)
        if k is None:
            proxy = self.depo.proxy_oku(hesap_id)
            h = self.getir(hesap_id)
            if h is not None and h["proxy_var"] and proxy is None:
                # proxy yapılandırılmış ama okunamıyor: DOĞRUDAN bağlantıya düşme; bağlantı kurulamaz say
                raise ProxyErisilemez("the stored proxy configuration could not be read")
            ic = self._istemci_fabrikasi(hesap_id, proxy, self.depo.token(hesap_id))
            k = HesapKaynagi(self, hesap_id, ic, self._genel, aralik=self._hesap_aralik, zaman_asimi=self._zaman_asimi)
            k.token_kusagi = self._token_kusaklari.get(hesap_id, 0)
            self._kaynaklar[hesap_id] = k
        return k

    async def kaynagi_yenile(self, hesap_id: int) -> None:
        """Token/proxy değişti: eski istemci kapatılır, sonraki istekte yenisi kurulur (token kuşağı artar)."""
        self._token_kusaklari[hesap_id] = self._token_kusaklari.get(hesap_id, 0) + 1
        k = self._kaynaklar.pop(hesap_id, None)
        if k is not None:
            try:
                await k.aclose()
            except Exception:
                log.warning("hesap istemcisi kapatılamadı")

    async def kapat(self) -> None:
        for hid in list(self._kaynaklar):
            k = self._kaynaklar.pop(hid)
            try:
                await k.aclose()
            except Exception:
                pass

    # ---- durum yazımı
    def durum_yaz(self, hesap_id: int, durum: str, ayrinti: str | None = None) -> bool:
        """Durumu yazar; DEĞİŞTİYSE True (geçiş bildirimi için)."""
        h = self.getir(hesap_id)
        if h is None:
            return False
        degisti = h["durum"] != durum
        with islem(self.conn):
            self.conn.execute(
                "UPDATE hesaplar SET durum=?, durum_ayrinti=?, durum_zamani=? WHERE hesap_id=?",
                (durum, (ayrinti or "")[:120] or None, iso(self.saat()) if degisti else h["durum_zamani"], hesap_id),
            )
        return degisti

    def _api_basari(self, hesap_id: int) -> None:
        try:
            with islem(self.conn):
                self.conn.execute("UPDATE hesaplar SET son_api_basari=? WHERE hesap_id=?", (iso(self.saat()), hesap_id))
        except sqlite3.Error:
            pass

    def cooldown_ayarla(self, hesap_id: int, saniye: float, *, rate_limit: bool = False) -> str:
        bitis = iso(self.saat() + timedelta(seconds=max(0.0, saniye)))
        with islem(self.conn):
            if rate_limit:
                self.conn.execute("UPDATE hesaplar SET cooldown_bitis=?, son_rate_limit=? WHERE hesap_id=?",
                                  (bitis, iso(self.saat()), hesap_id))
            else:
                self.conn.execute("UPDATE hesaplar SET cooldown_bitis=? WHERE hesap_id=?", (bitis, hesap_id))
        return bitis

    def cooldown_kalan(self, hesap_id: int) -> float:
        """Hesabın AKTİF bekleme süresi (sn): kendi bekleme penceresi VEYA aynı çıkış noktasını (aynı proxy/doğrudan IP) paylaşan
        başka bir hesabın bekleme penceresi. 0 = bekleme yok."""
        simdi = self.saat()
        egress = self.egress_anahtari(hesap_id)
        kalan = 0.0
        for r in self.conn.execute("SELECT hesap_id, cooldown_bitis FROM hesaplar WHERE cooldown_bitis IS NOT NULL"):
            if r["hesap_id"] != hesap_id and self.egress_anahtari(r["hesap_id"]) != egress:
                continue
            try:
                kalan = max(kalan, (iso_coz(r["cooldown_bitis"]) - simdi).total_seconds())
            except ValueError:
                continue
        return max(0.0, kalan)

    # ---- hesap oluşturma / güncelleme (sır önce SINANIR)
    def _sayi(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM hesaplar").fetchone()[0]

    async def hesap_olustur(self, ad: str, token: str, proxy_metni: str | None) -> tuple[int | None, DogrulamaSonucu]:
        """Aday token (ve varsa aday proxy) ÖNCE sınanır; yalnız geçerliyse hesap, dizin ve dosyalar oluşturulur. Hata: hiçbir şey oluşmaz."""
        ad = ad_dogrula(ad)
        token = token_biçimi_dogrula(token)
        if self._sayi() >= HESAP_AZAMI:
            raise HesapHatasi(f"At most {HESAP_AZAMI} accounts are supported.")
        if self.conn.execute("SELECT 1 FROM hesaplar WHERE lower(ad)=lower(?)", (ad,)).fetchone():
            raise HesapHatasi("An account with that name already exists.")
        proxy = proxy_ayristir(proxy_metni) if proxy_metni else None
        sonuc = await self._dogrulayici_fabrikasi(proxy).dogrula(token)
        sir_kaydet(token)
        if not sonuc.gecerli:
            return None, sonuc
        with islem(self.conn):
            cur = self.conn.execute(
                "INSERT INTO hesaplar(ad, olusturuldu, proxy_var, proxy_sema, durum, proxy_durum, token_guncelleme, durum_zamani)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (ad, iso(self.saat()), 1 if proxy else 0, proxy.sema if proxy else None, DURUM_HAZIR,
                 "calisiyor" if proxy else "yok", iso(self.saat()), iso(self.saat())),
            )
            hesap_id = cur.lastrowid
            try:
                self.depo.hazirla(hesap_id)
                self.depo.token(hesap_id).yaz(token)
                if proxy:
                    self.depo.proxy_yaz(hesap_id, proxy)
            except BaseException:
                self.depo.hesabi_sil_dosyalari(hesap_id)
                raise  # transaction geri alınır: satır oluşmaz
        self.dagit()
        return hesap_id, sonuc

    async def token_guncelle(self, hesap_id: int, token: str) -> DogrulamaSonucu:
        """Aday token hesabın MEVCUT proxy'si üzerinden sınanır; başarısızsa dosya/durum DEĞİŞMEZ."""
        h = self.getir(hesap_id)
        if h is None:
            raise HesapHatasi("Unknown account.")
        token = token_biçimi_dogrula(token)
        proxy = self.depo.proxy_oku(hesap_id) if h["proxy_var"] else None
        if h["proxy_var"] and proxy is None:
            raise HesapHatasi("The stored proxy configuration is unreadable; set the proxy again first.")
        sonuc = await self._dogrulayici_fabrikasi(proxy).dogrula(token)
        sir_kaydet(token)
        if not sonuc.gecerli:
            return sonuc
        self.depo.hazirla(hesap_id)
        self.depo.token(hesap_id).yaz(token)
        with islem(self.conn):
            self.conn.execute(
                "UPDATE hesaplar SET durum=?, durum_ayrinti=NULL, durum_zamani=?, token_guncelleme=?, ardisik_hata=0,"
                " sonraki_tarama=NULL, cooldown_bitis=NULL WHERE hesap_id=?",
                (DURUM_HAZIR, iso(self.saat()), iso(self.saat()), hesap_id),
            )
        await self.kaynagi_yenile(hesap_id)
        return sonuc

    async def proxy_guncelle(self, hesap_id: int, proxy_metni: str) -> DogrulamaSonucu:
        """Aday proxy, hesabın MEVCUT token'ıyla o proxy üzerinden sınanır; başarısızsa mevcut proxy/dosya DEĞİŞMEZ."""
        h = self.getir(hesap_id)
        if h is None:
            raise HesapHatasi("Unknown account.")
        proxy = proxy_ayristir(proxy_metni)
        token = self.depo.token(hesap_id).oku()
        if not token:
            raise HesapHatasi("This account has no token yet; update the JWT token first so the proxy can be verified.")
        sonuc = await self._dogrulayici_fabrikasi(proxy).dogrula(token)
        if not sonuc.gecerli:
            return sonuc
        self.depo.proxy_yaz(hesap_id, proxy)
        with islem(self.conn):
            self.conn.execute(
                "UPDATE hesaplar SET proxy_var=1, proxy_sema=?, proxy_durum='calisiyor', durum=?, durum_ayrinti=NULL, durum_zamani=?,"
                " ardisik_hata=0, sonraki_tarama=NULL, cooldown_bitis=NULL WHERE hesap_id=?",
                (proxy.sema, DURUM_HAZIR, iso(self.saat()), hesap_id),
            )
        await self.kaynagi_yenile(hesap_id)
        return sonuc

    async def proxy_kaldir(self, hesap_id: int) -> None:
        """Proxy'yi AÇIKÇA kaldırır (kurucu onayıyla): hesap bundan sonra doğrudan bağlanır. Otomatik geçiş DEĞİLDİR."""
        if self.getir(hesap_id) is None:
            raise HesapHatasi("Unknown account.")
        self.depo.proxy_sil(hesap_id)
        with islem(self.conn):
            self.conn.execute(
                "UPDATE hesaplar SET proxy_var=0, proxy_sema=NULL, proxy_durum='yok', durum=?, durum_ayrinti=NULL, ardisik_hata=0,"
                " sonraki_tarama=NULL, cooldown_bitis=NULL WHERE hesap_id=? AND durum IN (?, ?)",
                (DURUM_HAZIR, hesap_id, DURUM_PROXY_HATASI, DURUM_HAZIR),
            )
            self.conn.execute("UPDATE hesaplar SET proxy_var=0, proxy_sema=NULL, proxy_durum='yok' WHERE hesap_id=?", (hesap_id,))
        await self.kaynagi_yenile(hesap_id)

    async def baglanti_testi(self, hesap_id: int) -> DogrulamaSonucu:
        """Kayıtlı token + kayıtlı proxy ile doğrulama isteği. Sonuç hesabın bağlantı durumuna işlenir (token yazılmaz/değişmez)."""
        h = self.getir(hesap_id)
        if h is None:
            raise HesapHatasi("Unknown account.")
        token = self.depo.token(hesap_id).oku()
        if not token:
            self.durum_yaz(hesap_id, DURUM_TOKEN_YOK)
            return DogrulamaSonucu("hata", "this account has no token")
        proxy = self.depo.proxy_oku(hesap_id) if h["proxy_var"] else None
        if h["proxy_var"] and proxy is None:
            return DogrulamaSonucu("proxy_hatasi", "the stored proxy configuration is unreadable (no direct connection was attempted)")
        sonuc = await self._dogrulayici_fabrikasi(proxy).dogrula(token)
        if sonuc.gecerli:
            self.durum_yaz(hesap_id, DURUM_HAZIR)
            self._api_basari(hesap_id)
            with islem(self.conn):
                self.conn.execute("UPDATE hesaplar SET proxy_durum=? WHERE hesap_id=?", ("calisiyor" if proxy else "yok", hesap_id))
        elif sonuc.durum == "gecersiz":
            self.durum_yaz(hesap_id, DURUM_TOKEN_GECERSIZ, "token rejected (401)")
        elif sonuc.durum == "proxy_hatasi":
            self.durum_yaz(hesap_id, DURUM_PROXY_HATASI, "proxy unreachable")
            with islem(self.conn):
                self.conn.execute("UPDATE hesaplar SET proxy_durum='hata' WHERE hesap_id=?", (hesap_id,))
        return sonuc

    async def eski_token_ice_aktar(self, eski_yol: str | Path) -> tuple[int | None, DogrulamaSonucu]:
        """Kurucunun AÇIK isteğiyle: önceki tek-hesaplı sürümün token dosyasını 'Account 1' olarak içe aktarır (yalnız hiç hesap yokken)."""
        if self._sayi() > 0:
            raise HesapHatasi("Accounts already exist.")
        token = TokenDeposu(eski_yol).oku()
        if not token:
            raise HesapHatasi("No previous token file was found.")
        return await self.hesap_olustur("Account 1", token, None)

    # ---- görev ataması
    def dagit(self) -> dict:
        """İdempotent. Merkezi oyuncular ile atama tablosunu eşitler ve hesapları SIRAYLA doldurur (kapasite: self.kapasite).
        Silinmiş oyuncuların satırları kaldırılır; yenileri en sona eklenir; boşalan yerler sonraki hesaplardan MİNİMUM taşımayla
        sıraya dizilir (oyuncu geçmişi tabloları etkilenmez). Dönüş: {'atanan', 'bekleyen', 'hesap_sayisi'}."""
        M = config.MERKEZ_ID
        simdi = iso(self.saat())
        with islem(self.conn):
            self.conn.execute("DELETE FROM oyuncu_atamalari WHERE oyuncu_id NOT IN (SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=?)", (M,))
            self.conn.execute(
                "INSERT OR IGNORE INTO oyuncu_atamalari(oyuncu_id, hesap_id) SELECT oyuncu_id, NULL FROM takip_oyuncular WHERE group_id=?", (M,))
            hesaplar = [r[0] for r in self.conn.execute("SELECT hesap_id FROM hesaplar ORDER BY hesap_id")]
            # bilinmeyen hesaba atanmış satırlar beklemeye döner (hesap kaydı yok)
            self.conn.execute("UPDATE oyuncu_atamalari SET hesap_id=NULL WHERE hesap_id IS NOT NULL AND hesap_id NOT IN (SELECT hesap_id FROM hesaplar)")
            # ata: sırası = oyuncunun eklenme zamanı, sonra kimlik
            sira = {r["oyuncu_id"]: _sirali_pozisyon_anahtari(r) for r in self.conn.execute(
                "SELECT t.oyuncu_id, t.eklendi FROM takip_oyuncular t WHERE t.group_id=?", (M,))}
            sayac = {h: 0 for h in hesaplar}
            for r in self.conn.execute("SELECT hesap_id, COUNT(*) FROM oyuncu_atamalari WHERE hesap_id IS NOT NULL GROUP BY 1"):
                sayac[r[0]] = r[1]
            # taşkın (kapasite küçüldüyse) en son eklenenler beklemeye
            for h in hesaplar:
                fazla = sayac[h] - self.kapasite
                if fazla > 0:
                    idler = sorted((r[0] for r in self.conn.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id=?", (h,))),
                                   key=lambda o: sira.get(o, ("", 0, o)), reverse=True)[:fazla]
                    self.conn.executemany("UPDATE oyuncu_atamalari SET hesap_id=NULL WHERE oyuncu_id=?", [(i,) for i in idler])
                    sayac[h] = self.kapasite
            beklemede = sorted((r[0] for r in self.conn.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id IS NULL")),
                               key=lambda o: sira.get(o, ("", 0, o)))
            # 1) kompaktlama: boş yer olan en erken hesabı, SONRAKİ hesapların en son atananlarıyla doldur (en az taşıma)
            for i, h in enumerate(hesaplar):
                bos = self.kapasite - sayac[h]
                if bos <= 0:
                    continue
                for h2 in reversed(hesaplar[i + 1:]):
                    if bos <= 0:
                        break
                    if sayac[h2] <= 0:
                        continue
                    al = min(bos, sayac[h2])
                    idler = sorted((r[0] for r in self.conn.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id=?", (h2,))),
                                   key=lambda o: sira.get(o, ("", 0, o)), reverse=True)[:al]
                    self.conn.executemany("UPDATE oyuncu_atamalari SET hesap_id=?, atandi=? WHERE oyuncu_id=?", [(h, simdi, i_) for i_ in idler])
                    sayac[h] += al
                    sayac[h2] -= al
                    bos -= al
            # 2) beklemedekileri sırayla ata
            for oid in beklemede:
                for h in hesaplar:
                    if sayac[h] < self.kapasite:
                        self.conn.execute("UPDATE oyuncu_atamalari SET hesap_id=?, atandi=? WHERE oyuncu_id=?", (h, simdi, oid))
                        sayac[h] += 1
                        break
        bekleyen = self.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id IS NULL").fetchone()[0]
        toplam = self.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari").fetchone()[0]
        return {"atanan": toplam - bekleyen, "bekleyen": bekleyen, "hesap_sayisi": len(hesaplar)}

    def atanan_idler(self, hesap_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT oyuncu_id FROM oyuncu_atamalari WHERE hesap_id=? ORDER BY CAST(oyuncu_id AS INTEGER)", (hesap_id,))]

    def oyuncu_sonucu(self, oyuncu_id: str, durum: str, aciklama: str = "") -> None:
        """Tarama sonucu bildirimi (takip.oyuncular_tara geri çağrısı)."""
        simdi = iso(self.saat())
        with islem(self.conn):
            if durum == "ok":
                self.conn.execute("UPDATE oyuncu_atamalari SET son_deneme=?, son_basari=?, son_hata=NULL WHERE oyuncu_id=?", (simdi, simdi, oyuncu_id))
            else:
                self.conn.execute("UPDATE oyuncu_atamalari SET son_deneme=?, son_hata=? WHERE oyuncu_id=?",
                                  (simdi, (aciklama or durum)[:80], oyuncu_id))

    # ---- sorgular (menü/istatistik)
    def oyuncu_sayilari(self) -> dict[int | None, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT hesap_id, COUNT(*) FROM oyuncu_atamalari GROUP BY 1")}

    def oyuncu_sayfasi(self, hesap_id: int, sayfa: int, boyut: int, arama: str | None = None) -> tuple[list[sqlite3.Row], int]:
        """Hesaba ATANMIŞ oyuncular (yalnız bu hesabınkiler). arama: oyuncu ID'sinin başı/tamamı veya adın içinde geçen (büyük/küçük harf duyarsız)."""
        M = config.MERKEZ_ID
        kosul, arg = "a.hesap_id=? AND t.group_id=?", [hesap_id, M]
        if arama:
            q = arama.strip()
            kosul += " AND (a.oyuncu_id LIKE ? ESCAPE '\\' OR lower(COALESCE(t.ad,'')) LIKE ? ESCAPE '\\')"
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            arg += [esc + "%", "%" + esc.lower() + "%"]
        toplam = self.conn.execute(
            f"SELECT COUNT(*) FROM oyuncu_atamalari a JOIN takip_oyuncular t ON t.oyuncu_id=a.oyuncu_id WHERE {kosul}", arg).fetchone()[0]
        satirlar = self.conn.execute(
            f"SELECT a.oyuncu_id, t.ad, a.son_basari, a.son_deneme, a.son_hata, t.baseline_tamam FROM oyuncu_atamalari a"
            f" JOIN takip_oyuncular t ON t.oyuncu_id=a.oyuncu_id WHERE {kosul}"
            f" ORDER BY CAST(a.oyuncu_id AS INTEGER) LIMIT ? OFFSET ?", [*arg, boyut, sayfa * boyut]).fetchall()
        return satirlar, toplam

    def istatistik(self, hesap_id: int) -> dict:
        h = self.getir(hesap_id)
        aralik = self.conn.execute("SELECT tarama_araligi_dk FROM grup_ayarlari WHERE group_id=?", (config.MERKEZ_ID,)).fetchone()
        dk = aralik[0] if aralik else config.DEFAULT_SCAN_INTERVAL_MINUTES
        esik = iso(self.saat() - timedelta(minutes=2 * dk))
        atanan = self.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=?", (hesap_id,)).fetchone()[0]
        basarili = self.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=? AND son_basari IS NOT NULL AND son_basari>=?",
                                     (hesap_id, esik)).fetchone()[0]
        hatali = self.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id=? AND son_hata IS NOT NULL", (hesap_id,)).fetchone()[0]
        return {
            "hesap": h, "atanan": atanan, "kapasite": self.kapasite, "basarili": basarili, "bekleyen_gorev": atanan - basarili,
            "hatali": hatali, "cooldown_kalan": self.cooldown_kalan(hesap_id), "aralik_dk": dk,
        }

    def kapasite_ozeti(self) -> dict:
        d = self.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari WHERE hesap_id IS NULL").fetchone()[0]
        toplam = self.conn.execute("SELECT COUNT(*) FROM oyuncu_atamalari").fetchone()[0]
        return {"bekleyen": d, "toplam": toplam, "hesap_sayisi": self._sayi(), "kapasite": self.kapasite}
