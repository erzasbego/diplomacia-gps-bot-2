"""Yetki katmanı: SAF fonksiyonlar. Yalnızca sayısal Telegram kimlikleriyle çalışır.

Kurallar:
- GRUP SOHBETLERİNDE KOMUT/DÜĞME İŞLENMEZ (bildirimler yalnız log kanalına gider; yönetim yalnız kurucunun özel sohbetinde).
- Özel sohbet yalnızca kurucuya açık (diğer yetkililere de kapalı).
- Kurucunun yetkisi alınamaz.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

GRUP_TURLERI = frozenset({"group", "supergroup"})


@dataclass(frozen=True)
class YetkiDurumu:
    kurucu_id: int
    yetkililer: frozenset[int]
    onayli_gruplar: frozenset[int]


@dataclass(frozen=True)
class Karar:
    izin: bool
    neden: str  # "ok" | "grup_onaysiz" | "kullanici_yetkisiz" | "ozel_sohbet_kapali" | ...

    def __bool__(self) -> bool:
        return self.izin


def kurucu_mu(d: YetkiDurumu, kullanici_id: int | None) -> bool:
    return kullanici_id is not None and kullanici_id == d.kurucu_id


def yetkili_mi(d: YetkiDurumu, kullanici_id: int | None) -> bool:
    """Kurucu her zaman yetkilidir; diğerleri listede olmalı."""
    if kullanici_id is None:
        return False
    return kullanici_id == d.kurucu_id or kullanici_id in d.yetkililer


def karar_ver(
    d: YetkiDurumu, *, sohbet_id: int, sohbet_turu: str, kullanici_id: int | None
) -> Karar:
    """Komut/buton işleminin yapılıp yapılamayacağı. Her çağrıda GÜNCEL durumla çağrılmalı."""
    if sohbet_turu == "private":
        if kurucu_mu(d, kullanici_id) and sohbet_id == kullanici_id:
            return Karar(True, "ok")
        return Karar(False, "ozel_sohbet_kapali")
    if sohbet_turu in GRUP_TURLERI:
        # Eski gruplar artık komut işlemez (onaylı olsalar da, yetkili kullanıcı da olsa): sessizce yok sayılır.
        return Karar(False, "grup_komutlari_kapali")
    return Karar(False, "desteklenmeyen_sohbet")


# Yalnız kurucunun yapabildiği eylemler (yetkili kullanıcı yapamaz).
KURUCU_EYLEMLERI = frozenset({"grup_kaldir", "grup_tasi"})


def eylem_karar_ver(
    d: YetkiDurumu, *, sohbet_id: int, sohbet_turu: str, kullanici_id: int | None, eylem: str
) -> Karar:
    """Eylem bazlı karar: genel kuralların üstüne, KURUCU_EYLEMLERI için yalnız kurucu.

    Kurucu bu eylemleri kendi özel sohbetinde VEYA onaylı grupta çalıştırabilir.
    """
    k = karar_ver(d, sohbet_id=sohbet_id, sohbet_turu=sohbet_turu, kullanici_id=kullanici_id)
    if k and eylem in KURUCU_EYLEMLERI and not kurucu_mu(d, kullanici_id):
        return Karar(False, "yalniz_kurucu")
    return k


def token_yonetimi_izinli_mi(
    d: YetkiDurumu, *, sohbet_id: int, sohbet_turu: str, kullanici_id: int | None
) -> Karar:
    """Token görüntüleme/değiştirme: yalnız kurucu, yalnız kendi özel sohbetinde."""
    if sohbet_turu == "private" and kurucu_mu(d, kullanici_id) and sohbet_id == kullanici_id:
        return Karar(True, "ok")
    return Karar(False, "yalniz_kurucu_ozel_sohbet")


def yetki_alinabilir_mi(d: YetkiDurumu, hedef_id: int) -> Karar:
    if hedef_id == d.kurucu_id:
        return Karar(False, "kurucu_yetkisi_alinamaz")
    return Karar(True, "ok")


def bildirim_gerekli_mi(
    son_bildirim: datetime | None, simdi: datetime, aralik: timedelta
) -> bool:
    """Hız sınırı: son bildirimden bu yana `aralik` geçmediyse bildirme."""
    return son_bildirim is None or simdi - son_bildirim >= aralik
