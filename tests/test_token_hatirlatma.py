"""Öncelik 5: haftalık token hatırlatması (gerçek token ömrü DOĞRULANMADI). MOCK."""
import httpx

from conftest import KURUCU, bildirimler, run

from dipbot.diplomacia.dogrulayici import ProfilDogrulayici
from dipbot.diplomacia.istemci import OrtamKimlik
from dipbot.tg.arayuz import Arayuz
from dipbot.tg.bicim import bildirim_metni
from dipbot.tg.kaynak_yok import YapilandirilmamisKaynak
from dipbot.tg.modeller import Olay
from dipbot.tg.token import TokenDeposu
from dipbot.zaman import iso
from dipbot.zamanlayici import TaramaZamanlayici

GUN = 86400


def _z(conn, src, saat, tmp_path, var=True):
    depo = TokenDeposu(tmp_path / "t")
    if var:
        depo.yaz("TOK")
    return TaramaZamanlayici(conn, src, KURUCU, saat, token_deposu=depo), depo


def _durum(conn):
    r = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='token_son_guncelleme'").fetchone()
    return r[0] if r else None


def test_ilk_acilista_baslangic_kaydedilir_hatirlatma_yok(conn, src, saat, tmp_path):
    z, _ = _z(conn, src, saat, tmp_path)
    z.token_hatirlat()
    assert _durum(conn) is not None and bildirimler(conn, "token_hatirlatma") == []


def test_yedi_gunden_once_yok_sonra_yalniz_kurucuya_tek(conn, src, saat, tmp_path):
    z, _ = _z(conn, src, saat, tmp_path)
    z.token_hatirlat()
    saat.ilerle(6 * GUN)
    z.token_hatirlat()
    assert bildirimler(conn, "token_hatirlatma") == []
    saat.ilerle(1 * GUN + 5)
    for _ in range(5):  # tick yağmuru: tek mesaj
        z.token_hatirlat()
    b = bildirimler(conn, "token_hatirlatma")
    assert len(b) == 1 and b[0]["hedef_sohbet"] == KURUCU and b[0]["grup_id"] is None
    saat.ilerle(7 * GUN)  # ertesi hafta yeni mesaj
    z.token_hatirlat()
    assert len(bildirimler(conn, "token_hatirlatma")) == 2


def test_uzun_kapali_kalma_tek_mesaj(conn, src, saat, tmp_path):
    z, _ = _z(conn, src, saat, tmp_path)
    z.token_hatirlat()
    saat.ilerle(30 * GUN)
    z.token_hatirlat()
    z.token_hatirlat()
    assert len(bildirimler(conn, "token_hatirlatma")) == 1


def test_token_gecersizken_ve_dosya_yokken_hatirlatma_yok(conn, src, saat, tmp_path):
    z, _ = _z(conn, src, saat, tmp_path)
    z.token_hatirlat()
    saat.ilerle(8 * GUN)
    z._token_dur = True
    z.token_hatirlat()
    assert bildirimler(conn, "token_hatirlatma") == []
    z2, _ = _z(conn, src, saat, tmp_path / "bos", var=False)
    z2.token_hatirlat()
    assert bildirimler(conn, "token_hatirlatma") == []


def test_menuyle_guncelleme_sayaci_sifirlar(conn, src, saat, tmp_path):
    z, depo = _z(conn, src, saat, tmp_path)
    z.token_hatirlat()
    saat.ilerle(6 * GUN)

    class Dog:
        yapilandirildi = True

        async def dogrula(self, t):
            from dipbot.tg.token import DogrulamaSonucu
            return DogrulamaSonucu("gecerli")

    ui = Arayuz(conn, YapilandirilmamisKaynak(), KURUCU, saat=saat, zamanlayici=z, dogrulayici=Dog(), token_deposu=depo)
    y = run(ui._token_isle(Olay(sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU, metin="YENI")))
    assert "kaydedildi" in y.metin and _durum(conn) == iso(saat())
    saat.ilerle(3 * GUN)  # eski sayaca göre 9. gün
    z.token_hatirlat()
    assert bildirimler(conn, "token_hatirlatma") == []


def test_mesaj_metni_omur_dogrulanmadi_notu():
    m = bildirim_metni("token_hatirlatma", {"gun": 7})
    assert "doğrulanmad" in m.lower() and "token" in m.lower()
