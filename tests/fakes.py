"""Test yardımcıları: SAHTE kaynak, sahte gönderici, sahte saat.

Bunlar MOCK'tur; gerçek Diplomacia/Telegram davranışını doğrulamaz.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from dipbot.kaynak import (
    OyuncuAnlik,
    PartiAnlik,
    PartiUyesi,
    SavasDetay,
    SavasListesi,
    SavasOzet,
    VarlikBulunamadi,
)
from dipbot.outbox import GonderimHatasi


class SahteSaat:
    """Duvar saati + ona bağlı sahte monotonic saat (D7). `ilerle` ikisini birlikte (gerçek zaman akışı),
    `duvar_atla` yalnız duvar saatini (NTP/elle saat değişimi) oynatır."""

    def __init__(self):
        self.simdi = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
        self._mono = 1000.0

    def __call__(self) -> datetime:
        return self.simdi

    def monotonic(self) -> float:
        return self._mono

    def ilerle(self, saniye: float) -> None:
        self.simdi += timedelta(seconds=saniye)
        self._mono += saniye

    def duvar_atla(self, saniye: float) -> None:
        self.simdi += timedelta(seconds=saniye)


class SahteKaynak:
    def __init__(self):
        self.oyuncular: dict[str, OyuncuAnlik] = {}
        self.partiler: dict[str, PartiAnlik] = {}
        self.savaslar: list[SavasOzet] = []
        self.savas_tamam = True
        self.savas_detay: dict[str, SavasDetay] = {}
        self.hatalar: dict[tuple, Exception] = {}  # (tür, kimlik) -> hata; ("*","*") hepsi
        self.kapilar: dict[tuple, asyncio.Event] = {}
        self.cagrilar: list[tuple] = []
        self.gecikme = 0.0
        self.aktif = 0
        self.azami = 0

    # ---- kurulum yardımcıları
    def oyuncu(self, oid, ad="Ad", bolge="Varna", ulke="Ulke1"):
        self.oyuncular[oid] = OyuncuAnlik(oid, ad, bolge, ulke)

    def parti(self, pid, ad, uyeler, tamam=True, toplam=None, xp_sirali=None):
        """uyeler: (id, ad) veya (id, ad, xp). xp verilmezse 3.000.000 (XP süzgecini GEÇEN değer); (id, ad, None) =
        XP bilgisi eksik. toplam: partinin GERÇEK üye sayısı; uyeler'den büyükse liste SINIRLI (ilk N) sayılır."""
        kismi = toplam is not None and toplam > len(uyeler)
        uy = tuple(PartiUyesi(u[0], u[1], u[2] if len(u) > 2 else 3_000_000) for u in uyeler)
        self.partiler[pid] = PartiAnlik(
            pid, ad, uy, tamam and not kismi,
            kismi=kismi, toplam_uye=toplam if toplam is not None else len(uyeler), xp_sirali=xp_sirali,
        )

    # ---- protokol
    async def _gir(self, tur, kimlik=None):
        self.cagrilar.append((tur, kimlik))
        self.aktif += 1
        self.azami = max(self.azami, self.aktif)
        try:
            kapi = self.kapilar.get((tur, kimlik))
            if kapi is not None:
                await kapi.wait()
            if self.gecikme:
                await asyncio.sleep(self.gecikme)
            hata = self.hatalar.get(("*", "*")) or self.hatalar.get((tur, kimlik))
            if hata is not None:
                raise hata
        finally:
            self.aktif -= 1

    async def fetch_player(self, oyuncu_id):
        await self._gir("oyuncu", oyuncu_id)
        if oyuncu_id not in self.oyuncular:
            raise VarlikBulunamadi(oyuncu_id)
        return self.oyuncular[oyuncu_id]

    async def fetch_party(self, parti_id):
        await self._gir("parti", parti_id)
        if parti_id not in self.partiler:
            raise VarlikBulunamadi(parti_id)
        return self.partiler[parti_id]

    async def list_active_wars(self):
        await self._gir("savaslar")
        return SavasListesi(tuple(self.savaslar), self.savas_tamam)

    async def get_war_detail(self, savas_id):
        await self._gir("savas", savas_id)
        if savas_id not in self.savas_detay:
            raise VarlikBulunamadi(savas_id)
        return self.savas_detay[savas_id]

    def cagri_say(self, tur):
        return sum(1 for t, _ in self.cagrilar if t == tur)


class SahteGonderici:
    def __init__(self, hata_sayisi=0, kalici=False):
        self.gonderilen: list[tuple] = []
        self.kalan_hata = hata_sayisi
        self.kalici = kalici

    async def gonder(self, sohbet_id, tur, veri):
        if self.kalan_hata > 0:
            self.kalan_hata -= 1
            raise GonderimHatasi("sahte geçici hata", kalici=self.kalici)
        self.gonderilen.append((sohbet_id, tur, veri))
