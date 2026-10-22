"""Gerçek Diplomacia kaynağı henüz yok: yer tutucu. Hiçbir çağrıda sahte veri üretmez."""
from __future__ import annotations

from ..kaynak import OzellikUygulanmadi


class KaynakYapilandirilmadi(OzellikUygulanmadi):
    """Diplomacia bağlantısı (veri kaynağı) henüz yapılandırılmadı."""


_MESAJ = "Diplomacia connection is not configured"


class YapilandirilmamisKaynak:
    """DiplomaciaSource yer tutucusu: her çağrı KaynakYapilandirilmadi fırlatır."""

    yapilandirildi = False

    async def fetch_player(self, oyuncu_id):
        raise KaynakYapilandirilmadi(_MESAJ)

    async def fetch_party(self, parti_id):
        raise KaynakYapilandirilmadi(_MESAJ)

    async def list_active_wars(self):
        raise KaynakYapilandirilmadi(_MESAJ)

    async def get_war_detail(self, savas_id):
        raise KaynakYapilandirilmadi(_MESAJ)


def yapilandirildi_mi(kaynak) -> bool:
    """Kaynak (SinirliKaynak ile sarılı olsa da) gerçek bir bağlantı mı? Bayrak yoksa True varsayılır."""
    ic = getattr(kaynak, "_ic", None)  # SinirliKaynak sarmalayıcısını aç
    if ic is not None:
        kaynak = ic
    return bool(getattr(kaynak, "yapilandirildi", True))
