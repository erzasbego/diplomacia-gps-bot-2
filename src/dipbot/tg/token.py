"""Diplomacia oyun hesabı token'ı: doğrulayıcı arayüzü ve güvenli dosya deposu.

Bu, Telegram BOT_TOKEN'ı DEĞİL, veri kaynağına erişen oyun hesabı token'ıdır.

- Token veritabanına ve loga yazılmaz; ayrı yerel dosyada tutulur.
- Dosya yazımı atomiktir (aynı dizinde geçici dosya + fsync + os.replace) ve izin 0600'dür.
  Windows'ta POSIX izinleri uygulanmaz (os.chmod yalnız salt-okunur bayrağını etkiler); orada
  koruma NTFS dizin ACL'lerine bağlıdır. Üretim hedefi Linux'tur (systemd UMask=0077 de uygular).
- Yeni token doğrulanmadan mevcut token DEĞİŞTİRİLMEZ. Yazmadan önce mevcut dosya varsa 0600
  `<dosya>.prev` olarak (atomik) yedeklenir; .prev de bir sırdır (eski token) ve aynı izinle korunur.
- .prev SÜRESİZ TUTULMAZ: yeni token ilk başarılı yetkili taramayla teyit edilince VEYA yaşı
  config.TOKEN_PREV_OMRU_SANIYE (24 sa) dolunca zamanlayıcı onu siler (yalnız unlink; üzerine yazma yok).
  Geri dönüş (silinmeden önce): `.prev` dosyasını elle token dosyasının üzerine taşıyın. Politika kodu
  zamanlayicida (prev_politikasi_uygula); bu sınıf yalnız yaş/silme ilkellerini sağlar.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from ..maske import sir_kaydet


@dataclass(frozen=True)
class DogrulamaSonucu:
    durum: str  # "gecerli" | "gecersiz" | "yapilandirilmadi" | "hata"
    aciklama: str = ""

    @property
    def gecerli(self) -> bool:
        return self.durum == "gecerli"


@runtime_checkable
class TokenDogrulayici(Protocol):
    """Yeni token'ın gerçekten çalıştığını kaynakta sınar. Gerçek uygulama api ajanının işi."""

    async def dogrula(self, token: str) -> DogrulamaSonucu: ...


class YapilandirilmamisDogrulayici:
    """Varsayılan: doğrulama yapamaz, bu yüzden değiştirmeyi REDDETTİRİR. Sahte başarı yok."""

    yapilandirildi = False

    async def dogrula(self, token: str) -> DogrulamaSonucu:
        return DogrulamaSonucu("yapilandirilmadi", "Token verifier is not configured")


class TokenDeposu:
    def __init__(self, yol: str | Path):
        self.yol = Path(yol)

    def var_mi(self) -> bool:
        return self.yol.is_file()

    def son_degisiklik(self) -> float | None:
        try:
            return self.yol.stat().st_mtime
        except OSError:
            return None

    def oku(self) -> str | None:
        """Kaynak uygulaması (ileride) token'ı buradan okur. Dönen değer loglanmamalıdır."""
        try:
            t = self.yol.read_text(encoding="utf-8-sig").strip() or None
        except FileNotFoundError:
            return None
        sir_kaydet(t)  # log/istisna metinlerinde maskelensin (yalnız bellekte)
        return t

    @property
    def yedek_yolu(self) -> Path:
        return self.yol.with_name(self.yol.name + ".prev")

    def _atomik_yaz(self, hedef: Path, icerik: bytes) -> None:
        dizin = hedef.parent
        fd, gecici = tempfile.mkstemp(prefix=".token-", dir=str(dizin))  # mkstemp: 0600 oluşturur
        try:
            try:
                os.chmod(gecici, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "wb") as f:
                f.write(icerik)
                f.flush()
                os.fsync(f.fileno())
            os.replace(gecici, hedef)
        except BaseException:
            try:
                os.unlink(gecici)
            except OSError:
                pass
            raise
        try:
            os.chmod(hedef, 0o600)
        except OSError:
            pass

    def prev_yasi(self, simdi_ts: float) -> float | None:
        """`.prev` yedeğinin yaşı (sn; dosya değişiklik zamanından). Yedek yoksa None. Negatif: dosya zamanı
        gelecekte (saat geri gitmiş/dosya taşınmış) -> çağıran belirsiz sayar."""
        try:
            return simdi_ts - self.yedek_yolu.lstat().st_mtime
        except OSError:
            return None

    def prev_sil(self) -> bool:
        """`.prev` yedeğini siler (yalnız unlink; sembolik bağ ise bağın kendisi). True: silindi."""
        try:
            self.yedek_yolu.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def eski_gecicileri_temizle(self) -> int:
        """Açılışta: yarım kalmış '.token-*' geçici dosyalarını (yalnız token dizininde) siler. Dönüş: silinen sayı."""
        n = 0
        try:
            adaylar = list(self.yol.parent.glob(".token-*"))
        except OSError:
            return 0
        for p in adaylar:
            try:
                if p.is_file() and not p.is_symlink():
                    p.unlink()
                    n += 1
            except OSError:
                pass
        return n

    def yaz(self, token: str) -> None:
        """Atomik ve 0600 izinli yazım. Mevcut dosya varsa önce 0600 `.prev` yedeği alınır; yedek
        alınamazsa yazım yapılmaz. Hata durumunda mevcut dosya olduğu gibi kalır."""
        if not token or any(c.isspace() for c in token):
            raise ValueError("token boş olamaz ve boşluk içeremez")
        dizin = self.yol.parent
        dizin.mkdir(parents=True, exist_ok=True)
        if self.yol.is_file():
            self._atomik_yaz(self.yedek_yolu, self.yol.read_bytes())
        self._atomik_yaz(self.yol, token.encode("utf-8"))
        sir_kaydet(token)
