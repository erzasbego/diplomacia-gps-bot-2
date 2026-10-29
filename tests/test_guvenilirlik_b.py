"""Grup B güvenilirlik testleri: O8 (outbox talep), O6 (engellenen), O1-O3, k13/k18/k20 güçlendirme.

MOCK kaynak/gönderici; canlı doğrulama DEĞİLDİR.
"""
import asyncio
import logging
from datetime import timedelta

import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte

from dipbot import config, outbox, yetki, yonetim
from dipbot.db import islem
from dipbot.kaynak import IstekSiniri, OyuncuAnlik
from dipbot.outbox import GonderimHatasi
from dipbot.takip import OyuncuOzeti
from dipbot.zamanlayici import TaramaZamanlayici
from tg_fakes import YETKILI, YABANCI, dugme, gercek_grup_dugme, grup_dugme, grup_komut, onay_kimligi
from fakes import SahteGonderici


def _kuyruk(conn, saat, grup=G1, anahtar="k1"):
    with islem(conn):
        return outbox.kuyruga_ekle(
            conn, hedef_sohbet=grup, grup_id=grup, tur="t", idem_anahtari=anahtar, veri={"x": 1}, simdi=saat()
        )


def _satir(conn):
    return conn.execute("SELECT * FROM outbox ORDER BY id").fetchone()


class HataGonderici:
    def __init__(self, hata_fabrikasi):
        self.f = hata_fabrikasi
        self.cagri = 0

    async def gonder(self, sohbet_id, tur, veri):
        self.cagri += 1
        raise self.f()


# ---------------------------------------------------------------- O8
class YavasGonderici:
    def __init__(self):
        self.gonderilen = []
        self.kapi = None

    async def gonder(self, sohbet_id, tur, veri):
        self.gonderilen.append((sohbet_id, tur))
        await self.kapi.wait()


def test_o8_esdeger_cagrilar_ayni_satiri_iki_kez_gondermez(conn, saat):
    _kuyruk(conn, saat)

    async def sen():
        g = YavasGonderici()
        g.kapi = asyncio.Event()
        t1 = asyncio.ensure_future(outbox.bekleyenleri_gonder(conn, g, saat))
        await asyncio.sleep(0)
        t2 = asyncio.ensure_future(outbox.bekleyenleri_gonder(conn, g, saat))
        t3 = asyncio.ensure_future(outbox.bekleyenleri_gonder(conn, g, saat))
        await asyncio.sleep(0.01)
        g.kapi.set()
        sonuc = await asyncio.gather(t1, t2, t3)
        return g, sonuc

    g, sonuc = run(sen())
    assert len(g.gonderilen) == 1
    assert sum(o.gonderilen for o in sonuc) == 1
    assert _satir(conn)["durum"] == "gonderildi"


def test_o8_gonderim_sirasinda_cokme_talep_suresi_dolunca_kurtarilir(conn, saat):
    """'gonderildi' işaretinden önce çökme: talep süresi dolana dek tekrar yok, sonra TEKRAR gönderilir
    (en-az-bir-kez teslim; belgelenmiş sınır)."""
    _kuyruk(conn, saat)

    class CokenGonderici:
        def __init__(self):
            self.gonderilen = 0

        async def gonder(self, sohbet_id, tur, veri):
            self.gonderilen += 1
            raise asyncio.CancelledError()  # mesaj gitti ama süreç 'gonderildi' yazamadan öldü

    g = CokenGonderici()

    async def dene():
        try:
            await outbox.bekleyenleri_gonder(conn, g, saat)
        except asyncio.CancelledError:
            pass

    run(dene())
    assert g.gonderilen == 1 and _satir(conn)["durum"] == "bekliyor"
    run(dene())
    assert g.gonderilen == 1, "talep (kilit) süresi dolmadan tekrar gönderilmemeli"
    saat.ilerle(config.OUTBOX_CLAIM_LEASE_SECONDS + 1)
    run(dene())
    assert g.gonderilen == 2, "takılı kalan satır süre sonunda kurtarılıp yeniden gönderilir (belgeli sınır)"
    ok = SahteGonderici()
    saat.ilerle(config.OUTBOX_CLAIM_LEASE_SECONDS + 1)
    assert run(outbox.bekleyenleri_gonder(conn, ok, saat)).gonderilen == 1
    assert _satir(conn)["durum"] == "gonderildi"


# ---------------------------------------------------------------- O1 / k18
def test_o1_gecici_hata_8_denemeden_sonra_basarisiz_olmaz_yas_sinirinda_olur(conn, saat):
    _kuyruk(conn, saat)
    g = HataGonderici(lambda: GonderimHatasi("gecici"))
    for _ in range(12):
        run(outbox.bekleyenleri_gonder(conn, g, saat))
        saat.ilerle(config.OUTBOX_BACKOFF_MAX_SECONDS + 1)
    r = _satir(conn)
    assert g.cagri == 12 and r["durum"] == "bekliyor" and r["deneme"] == 12
    # yaş sınırı aşılınca kalıcı başarısız
    saat.ilerle(config.OUTBOX_MAX_AGE_SECONDS)
    o = run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert o.basarisiz == 1 and _satir(conn)["durum"] == "basarisiz"
    assert g.cagri == 13


def test_o1_k18_429_deneme_sayacini_artirmaz_ve_bekleme_ustten_sinirlidir(conn, saat):
    _kuyruk(conn, saat)
    g = HataGonderici(lambda: GonderimHatasi("429", bekle_saniye=45))
    for _ in range(20):
        run(outbox.bekleyenleri_gonder(conn, g, saat))
        r = _satir(conn)
        assert r["deneme"] == 0 and r["durum"] == "bekliyor"
        assert r["sonraki_deneme"] > saat().strftime("%Y-%m-%dT%H:%M:%S")  # ileri bir ana ertelendi
        saat.ilerle(46)
    # bekle_saniye aşırı büyükse sınırlanır (bekleme 1 saatten uzun olamaz)
    g2 = HataGonderici(lambda: GonderimHatasi("429", bekle_saniye=10**9))
    run(outbox.bekleyenleri_gonder(conn, g2, saat))
    saat.ilerle(config.OUTBOX_RETRY_AFTER_MAX_SECONDS + 1)
    run(outbox.bekleyenleri_gonder(conn, g2, saat))
    assert g2.cagri == 2


def test_k18_kalici_hata_hemen_basarisiz_ve_gecici_geri_cekilme_sinirli(conn, saat):
    _kuyruk(conn, saat)
    assert run(outbox.bekleyenleri_gonder(conn, SahteGonderici(1, kalici=True), saat)).basarisiz == 1
    assert _satir(conn)["durum"] == "basarisiz" and _satir(conn)["deneme"] == 1
    # basarisiz satır bir daha denenmez
    ok = SahteGonderici()
    saat.ilerle(10**6)
    assert run(outbox.bekleyenleri_gonder(conn, ok, saat)).gonderilen == 0 and ok.gonderilen == []
    # geri çekilme üst sınırı
    assert outbox._geri_cekilme(500) == config.OUTBOX_BACKOFF_MAX_SECONDS


# ---------------------------------------------------------------- O2
def test_o2_son_hata_token_ve_yetki_basligi_maskeler(conn, saat):
    _kuyruk(conn, saat)
    sir = "123456789:AAEsahteSAHTEsahteSAHTEsahteSAHTE0"  # tamamen uydurma
    g = HataGonderici(
        lambda: RuntimeError(
            f"POST https://api.telegram.org/bot{sir}/sendMessage failed; Authorization: Bearer abc.DEF-123 x"
        )
    )
    run(outbox.bekleyenleri_gonder(conn, g, saat))
    hata = _satir(conn)["son_hata"]
    assert hata and sir not in hata and "AAEsahteSAHTE" not in hata
    assert "abc.DEF-123" not in hata and "Bearer abc" not in hata
    assert "RuntimeError" in hata


def test_o2_maskele_desenleri():
    from dipbot.maske import maskele

    assert "SIR" not in maskele("url /bot12345:SIRSIRSIR_abc/getMe")
    assert "SIR" not in maskele("Authorization=Bearer SIRSIR")
    assert "SIR" not in maskele("authorization: SIRSIR")
    assert "SIR" not in maskele("header bearer SIRSIR.tok")
    assert maskele("normal metin 123") == "normal metin 123"


def test_o2_log_suzgeci_yetki_basligini_maskeler():
    from dipbot.tg.gunluk import SirFiltresi

    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "istek Authorization: Bearer GIZLIJETON", (), None)
    SirFiltresi(None).filter(rec)
    assert "GIZLIJETON" not in rec.getMessage()


# ---------------------------------------------------------------- O3
def _z(conn, src, saat):
    return TaramaZamanlayici(conn, src, KURUCU, saat)


def test_o3_istek_siniri_sayaci_basarida_sifirlanir_ve_bekleme_sinirlidir(conn, src, saat, servis):
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G2)  # tek grup: başarı/hata sırası belirsiz olmasın
    src.oyuncu("p", "P")
    run(servis.oyuncu_ekle(G1, "p", src))
    z = _z(conn, src, saat)
    src.hatalar[("oyuncu", "p")] = IstekSiniri("429")  # bekle_saniye yok: üstel
    beklemeler = []
    for _ in range(3):
        run(z.tick_ve_bekle())
        beklemeler.append((z._engel_bitis - saat()).total_seconds())
        saat.ilerle(3 * 3600)
    assert beklemeler == [120, 240, 480]
    src.hatalar.clear()
    run(z.tick_ve_bekle())  # başarı
    saat.ilerle(3 * 3600)
    src.hatalar[("oyuncu", "p")] = IstekSiniri("429")
    run(z.tick_ve_bekle())
    assert (z._engel_bitis - saat()).total_seconds() == 120, "başarıdan sonra sayaç sıfırlanmalı"
    # sunucu çok büyük bekleme isterse sınırlanır
    saat.ilerle(3 * 3600)
    src.hatalar[("oyuncu", "p")] = IstekSiniri("429", bekle_saniye=10**7)
    run(z.tick_ve_bekle())
    assert (z._engel_bitis - saat()).total_seconds() <= config.RATE_LIMIT_MAX_WAIT_SECONDS


# ---------------------------------------------------------------- O6
def test_o6_kaldirma_sonucu_engellenen_ve_silinen_ayrintilari(conn, servis, src):
    for o, ad in (("a", "A"), ("b", "B"), ("c", "C")):
        src.oyuncu(o, ad)
    src.parti("P1", "Bir", [("a", "A"), ("b", "B")])
    src.parti("P2", "Iki", [("b", "B"), ("c", "C")])
    run(servis.parti_ekle(G1, "P1", src))
    run(servis.parti_ekle(G1, "P2", src))
    on = run(servis.parti_kaldirma_hazirla(G1, "P1", src))
    s = run(servis.parti_kaldirma_onayla(G1, "P1", src, on.ozet_anahtari))
    assert s.durum == "silindi" and set(s.silinen) == {"a", "b"}
    assert s.silinen_oyuncular == (OyuncuOzeti("a", "A"), OyuncuOzeti("b", "B"))
    # b başka takipli partide görünüyor: yeniden-ekleme engeli konuldu ve sonuçta bildirildi
    assert s.engellenen == (OyuncuOzeti("b", "B"),)
    assert conn.execute("SELECT neden FROM engeller WHERE group_id=? AND oyuncu_id='b'", (G1,)).fetchone()[0] == "parti_kaldirildi"


def test_o6_tg_sonuc_mesaji_engellenenleri_kacisli_gosterir(conn, saat, src):
    from dipbot.tg.arayuz import Arayuz

    ui = Arayuz(conn, src, KURUCU, saat=saat)
    src.oyuncu("1", "<b>X</b> & Y")
    src.oyuncu("2", "Z")
    src.parti("51", "Bir", [("1", "<b>X</b> & Y")])
    src.parti("52", "Iki", [("1", "<b>X</b> & Y"), ("2", "Z")])
    run(ui.komut(grup_komut("parti_ekle", "51")))
    run(ui.komut(grup_komut("parti_ekle", "52")))
    kid = onay_kimligi(run(ui.komut(grup_komut("parti_sil", "51"))))
    y = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert "tracking removed" in y.metin
    assert "&lt;b&gt;X&lt;/b&gt; &amp; Y (#1)" in y.metin, y.metin
    assert "<b>X</b> &" not in y.metin
    assert "will not be re-added" in y.metin or "block" in y.metin.lower()


# ---------------------------------------------------------------- k13
def test_k13_ozel_sohbette_sohbet_kullanici_ayni_olmali_ve_token_yalniz_kurucu(conn, saat, src):
    from dipbot.tg.arayuz import Arayuz

    d = yonetim.durum_yukle(conn, KURUCU)
    # kurucu bile sohbet_id != kullanici_id ise özel sohbette reddedilir (sahte/bozuk bağlam)
    assert not yetki.karar_ver(d, sohbet_id=999, sohbet_turu="private", kullanici_id=KURUCU)
    assert not yetki.token_yonetimi_izinli_mi(d, sohbet_id=999, sohbet_turu="private", kullanici_id=KURUCU)
    assert yetki.karar_ver(d, sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU)
    # tg: yetkili (kurucu olmayan) kullanıcı özel sohbette token menüsünü açamaz
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    ui = Arayuz(conn, src, KURUCU, saat=saat)
    for kimlik in (YETKILI, YABANCI):
        y = run(ui.buton(dugme("tk", sohbet=kimlik, tur="private", kul=kimlik)))
        assert y.sessiz and y.uyari_goster and not y.metin
    # kurucu, sohbet kimliği kendisininkinden farklı özel sohbette de açamaz
    y = run(ui.buton(dugme("tk", sohbet=555, tur="private", kul=KURUCU)))
    assert y.sessiz and not y.metin
    # kurucu grupta açamaz (grup düğmeleri sessizce yok sayılır: metin/uyarı yok); kendi özel sohbetinde açabilir
    y = run(ui.buton(gercek_grup_dugme("tk")))
    assert y.sessiz and not y.metin and y.uyari is None
    assert run(ui.buton(dugme("tk", sohbet=KURUCU, tur="private", kul=KURUCU))).metin


# ---------------------------------------------------------------- k20
def test_k20_parti_sonra_elle_iki_parti_tek_bildirim(conn, servis, src):
    for o, ad in (("a", "A"), ("b", "B"), ("c", "C")):
        src.oyuncu(o, ad, "Varna", "U")
    src.parti("P1", "Bir", [("a", "A"), ("b", "B")])
    src.parti("P2", "Iki", [("b", "B"), ("c", "C")])
    s1 = run(servis.parti_ekle(G1, "P1", src))
    assert (s1.yeni_eklenen, s1.zaten_takipte) == (2, 0)
    # önce parti, sonra elle ekleme: mükerrer yok, durum zaten_takipte
    assert run(servis.oyuncu_ekle(G1, "a", src)).durum == "zaten_takipte"
    s2 = run(servis.parti_ekle(G1, "P2", src))
    assert (s2.yeni_eklenen, s2.zaten_takipte) == (1, 1)  # b iki partide, tek takip
    assert takipte(conn, G1) == {"a", "b", "c"}
    # b iki partinin üyesi: parti_uyeleri'nde iki satır, takip_oyuncular'da TEK satır
    assert conn.execute("SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=? AND oyuncu_id='b'", (G1,)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=? AND oyuncu_id='b'", (G1,)).fetchone()[0] == 1
    run(servis.oyuncular_tara(G1, src))  # C2: baseline ilk taramada oturur (bildirimsiz)
    assert bildirimler(conn, "oyuncu_degisti") == []
    # b'nin bölgesi değişir: iki parti taransa da TEK bildirim
    src.oyuncu("b", "B", "Istanbul", "U")
    for _ in range(2):
        run(servis.partiler_tara(G1, src))
        run(servis.oyuncular_tara(G1, src))
    b = bildirimler(conn, "oyuncu_degisti")
    assert len(b) == 1 and b[0]["veri"]["oyuncu_id"] == "b"
    assert bildirimler(conn, "parti_uyelik") == []
