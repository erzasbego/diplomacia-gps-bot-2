"""Tur E düzeltmeleri (Opus/QA kalan bulgular). MOCK testler; canlı doğrulama değildir."""
import asyncio

import pytest

from conftest import G1, KURUCU, bildirimler, run

from dipbot.kaynak import TokenGecersiz, ZamanlayiciKorumali
from dipbot.zamanlayici import TaramaZamanlayici


def _oyuncu(servis, src, grup, oid):
    src.oyuncu(oid, oid.upper())
    run(servis.oyuncu_ekle(grup, oid, src))


# ------------------------------------------------------------------ E1
def test_ucustaki_eski_401_token_yenilendikten_sonra_duraklatmaz_tarama(conn, src, saat, servis):
    _oyuncu(servis, src, G1, "p1")
    z = TaramaZamanlayici(conn, src, KURUCU, saat)

    async def sen():
        kapi = asyncio.Event()
        src.kapilar[("oyuncu", "p1")] = kapi
        gorevler = z.tick()
        await asyncio.sleep(0.02)
        z.token_devam_ettir()  # yeni token kaydedildi (istek eski token ile uçuşta)
        src.hatalar[("*", "*")] = TokenGecersiz("401")
        kapi.set()
        await asyncio.gather(*gorevler)

    run(sen())
    assert not z.token_duraklatildi
    assert bildirimler(conn, "token_gecersiz") == []


def test_kusak_degismediyse_401_duraklatir_tek_bildirim(conn, src, saat, servis):
    _oyuncu(servis, src, G1, "p1")
    src.hatalar[("*", "*")] = TokenGecersiz("401")
    z = TaramaZamanlayici(conn, src, KURUCU, saat)
    run(z.tick_ve_bekle())
    assert z.token_duraklatildi
    assert len(bildirimler(conn, "token_gecersiz")) == 1


def test_arayuz_yolu_ucustaki_eski_401_duraklatmaz(conn, src, saat, servis):
    z = TaramaZamanlayici(conn, src, KURUCU, saat)
    k = ZamanlayiciKorumali(z.kaynak, z)
    src.oyuncu("p9", "P9")

    async def sen():
        kapi = asyncio.Event()
        src.kapilar[("oyuncu", "p9")] = kapi
        t = asyncio.ensure_future(k.fetch_player("p9"))
        await asyncio.sleep(0.02)
        z.token_devam_ettir()
        src.hatalar[("*", "*")] = TokenGecersiz("401")
        kapi.set()
        with pytest.raises(TokenGecersiz):
            await t

    run(sen())
    assert not z.token_duraklatildi
    assert bildirimler(conn, "token_gecersiz") == []


# ------------------------------------------------------------------ E2 / E4 / E9
JWT_SAHTE = "eyJSAHTE.SAHTE.SAHTE"


def _tok():
    from test_telegram_arayuz import SahteDogrulayici, _token_ui
    return SahteDogrulayici, _token_ui


def test_e2_suresi_dolmus_beklemeden_sonra_gelen_token_silinmeye_calisilir(conn, src, saat, tmp_path):
    from test_telegram_arayuz import _token_mesaji, dugme
    SD, tui = _tok()
    d = SD()
    ui, depo, z = tui(conn, src, saat, tmp_path, d)
    run(ui.buton(dugme("tn")))
    saat.ilerle(6 * 60)
    silinen = []
    y = _token_mesaji(ui, JWT_SAHTE, silinen)
    assert silinen == [1], "süresi dolmuş beklemeden sonra gelen token mesajı silinmeye çalışılmalı"
    assert "süresi doldu" in y.metin and JWT_SAHTE not in y.metin and d.alinan == []
    assert depo.oku() == "eski-token-XYZ"


def test_e4_aday_token_dogrulama_sirasinda_maskelenir(conn, src, saat, tmp_path):
    from test_telegram_arayuz import _token_mesaji, dugme
    from dipbot import maske
    from dipbot.tg.token import DogrulamaSonucu
    gorulen = []

    class D:
        async def dogrula(self, token):
            gorulen.append(token in maske._CALISMA_SIRLARI)
            return DogrulamaSonucu("gecerli", "x")

    _, tui = _tok()
    tok = "eyJMASKE1.MASKE22.MASKE33"
    ui, depo, z = tui(conn, src, saat, tmp_path, D())
    maske._CALISMA_SIRLARI.discard(tok)
    run(ui.buton(dugme("tn")))
    _token_mesaji(ui, tok)
    assert gorulen == [True], "aday token doğrulamadan ÖNCE sır kümesine kaydedilmeli"
    maske._CALISMA_SIRLARI.discard(tok)


def test_e9_bekleme_penceresinde_jwt_olmayan_metin_token_sayilmaz(conn, src, saat, tmp_path):
    from test_telegram_arayuz import _token_mesaji, dugme
    SD, tui = _tok()
    d = SD()
    ui, depo, z = tui(conn, src, saat, tmp_path, d)
    run(ui.buton(dugme("tn")))
    silinen = []
    y = _token_mesaji(ui, "merhaba-bu-duz-metin", silinen)
    assert d.alinan == [] and silinen == [1]
    assert "token biçimi geçersiz" in y.metin.lower() and "merhaba-bu-duz-metin" not in y.metin
    assert depo.oku() == "eski-token-XYZ"


def test_e9_token_dosyasi_bom_ve_satir_sonu_tolere_edilir(tmp_path):
    from dipbot.tg.token import TokenDeposu
    yol = tmp_path / "t.token"
    yol.write_bytes(b"\xef\xbb\xbf" + JWT_SAHTE.encode() + b"\r\n")
    assert TokenDeposu(yol).oku() == JWT_SAHTE


# ------------------------------------------------------------------ E3
def _dogrulayici(tmp_path, isleyici, **kw):
    import httpx
    from dipbot.diplomacia.dogrulayici import OyuncuDogrulayici
    from dipbot.diplomacia.istemci import OrtamKimlik
    from dipbot.tg.token import TokenDeposu
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz("ESKI-TOKEN-ABC")
    return OyuncuDogrulayici(OrtamKimlik(depo, {}), transport=httpx.MockTransport(isleyici), **kw)


def test_e3_dogrulama_oyuncusu_env_ile_secilir_ve_istek_o_yola_gider(tmp_path):
    import httpx
    from dipbot.tg.yapilandirma import TelegramAyarlari
    yollar = []

    def isleyici(r):
        yollar.append(r.url.path)
        return httpx.Response(404)

    ortam = {"BOT_TOKEN": "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll", "OWNER_TELEGRAM_ID": "1000",
             "DIPLOMACIA_DOGRULAMA_OYUNCU": "555"}
    ta = TelegramAyarlari.from_env(ortam)
    from dipbot.tg import calistir as c
    d = c.dogrulayici_olustur(tmp_path / "t", {}, oyuncu_no=ta.dogrulama_oyuncusu)
    d._transport = httpx.MockTransport(isleyici)
    s = run(d.dogrula(JWT_SAHTE))
    assert yollar == ["/api/players/555"]
    assert s.durum == "hata"
    assert "doğrulama oyuncusu bulunamadı" in s.aciklama and "geçersiz demek değildir" in s.aciklama
    assert "DIPLOMACIA_DOGRULAMA_OYUNCU" in s.aciklama


def test_e3_varsayilan_72400_ve_gecersiz_deger_yapilandirma_hatasi():
    from dipbot.tg.yapilandirma import TelegramAyarlari, YapilandirmaHatasi
    temel = {"BOT_TOKEN": "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll", "OWNER_TELEGRAM_ID": "1000"}
    assert TelegramAyarlari.from_env(temel).dogrulama_oyuncusu == "72400"
    for kotu in ("abc", "0", "012", "1/2", "../x", "1 2"):
        with pytest.raises(YapilandirmaHatasi):
            TelegramAyarlari.from_env({**temel, "DIPLOMACIA_DOGRULAMA_OYUNCU": kotu})


def test_e3_404_kurucuya_ayri_aciklama_token_yazilmaz(conn, src, saat, tmp_path):
    import httpx
    from dipbot.tg.arayuz import Arayuz
    from dipbot.tg.modeller import Olay
    d = _dogrulayici(tmp_path, lambda r: httpx.Response(404))
    depo = d._kimlik._depo if hasattr(d._kimlik, "_depo") else None
    from dipbot.tg.token import TokenDeposu
    depo = TokenDeposu(tmp_path / "t")
    ui = Arayuz(conn, src, KURUCU, saat=saat, dogrulayici=d, token_deposu=depo)
    y = run(ui._token_isle(Olay(sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU, metin=JWT_SAHTE)))
    assert "doğrulama oyuncusu bulunamadı" in y.metin and "DEĞİŞTİRİLMEDİ" in y.metin
    assert depo.oku() == "ESKI-TOKEN-ABC"


# ------------------------------------------------------------------ E5
def test_e5_hata_yolu_ucustaki_gonderim_iptalini_ezmez(conn, saat):
    from dipbot import outbox
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="x1",
                        veri={"war_number": 1, "ad": "x"}, simdi=saat())

    class G:
        async def gonder(self, sohbet, tur, veri):
            outbox.grup_bekleyenlerini_iptal_et(conn, G1)  # uçuş sırasında /grup_kaldir
            raise outbox.GonderimHatasi("kalici", kalici=True)

    run(outbox.bekleyenleri_gonder(conn, G(), saat))
    assert conn.execute("SELECT durum FROM outbox").fetchone()[0] == "iptal"


# ------------------------------------------------------------------ E6
def test_e6_mukerrer_uid_zaten_takipte_yaniti_asil_numaraya_baglanir(conn, src, saat, servis):
    from tg_fakes import grup_komut
    from dipbot.kaynak import OyuncuAnlik
    from dipbot.tg.arayuz import Arayuz
    src.oyuncular["11"] = OyuncuAnlik("11", "A", "Varna", "U1", uid="UUID-A")
    src.oyuncular["12"] = OyuncuAnlik("12", "A", "Varna", "U1", uid="UUID-A")
    run(servis.oyuncu_ekle(G1, "11", src))
    ui = Arayuz(conn, src, KURUCU, saat=saat)
    y = run(ui.komut(grup_komut("oyuncu_ekle", "12")))
    assert "zaten takipte" in y.metin
    assert "/player/11" in y.metin and "/player/12" not in y.metin


# ------------------------------------------------------------------ E7
def test_e7_401_metinleri_kesin_degil_ve_dogrulanmadi_yorumu():
    from dipbot.tg import bicim
    m = bicim.bildirim_metni("token_gecersiz", {"zaman": "2026-10-20T10:00:00+00:00"})
    assert "geçersiz veya süresi dolmuş" not in m and "reddetti (401)" in m
    from pathlib import Path
    kaynak = (Path(__file__).parent.parent / "src/dipbot/diplomacia/istemci.py").read_text(encoding="utf-8")
    assert "DOĞRULANMADI" in kaynak and "# DOĞRULANDI (başlıksız istek 401 aldı)" not in kaynak
    from dipbot.kaynak import hata_aciklamasi, TokenGecersiz
    assert "geçersiz veya süresi dolmuş" not in hata_aciklamasi(TokenGecersiz("401"))


# ------------------------------------------------------------------ E8
def test_e8_grup_goc_bildirimi_celiskisiz():
    from dipbot.tg import bicim
    m = bicim.bildirim_metni("grup_goc", {"eski": -100111, "yeni": -100222, "zaman": "2026-10-20T10:00:00+00:00"})
    assert "/grup_onayla -100222" in m
    assert "/grup_kaldir" not in m, "eski kimlik zaten otomatik düşürüldü; kaldırma önerisi çelişkili"
    assert "otomatik düşürüldü" in m


# ------------------------------------------------------------------ E10
def test_e10_savas_taninmayan_sayaci_guncel_durumu_yansitir(conn, servis, src):
    from dipbot.kaynak import SavasOzet

    def s(i, durum="active", tani=False):
        return SavasOzet(f"id-{i:03d}", f"S{i}", durum=durum, durum_taninmiyor=tani, war_number=1000 + i)

    def sayac():
        r = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (f"savas_taninmayan:{G1}",)).fetchone()
        return int(r[0]) if r else 0

    src.savaslar = [s(0)]
    run(servis.savaslari_tara(G1, src))
    src.savaslar = [s(0), s(1, "ended", True), s(2, "x", True)]
    run(servis.savaslari_tara(G1, src))
    assert sayac() == 2
    src.savaslar = [s(0), s(1, "ended", True)]  # biri listeden çıktı: şu an 1
    run(servis.savaslari_tara(G1, src))
    assert sayac() == 1
    src.savaslar = [s(0)]  # hepsi çıktı
    run(servis.savaslari_tara(G1, src))
    assert sayac() == 0


@pytest.mark.parametrize("hata", [
    ("kalici", dict(kalici=True)),
    ("gecici", dict()),
    ("retry_after", dict(bekle_saniye=600)),
])
def test_e5_iptal_satirina_hicbir_hata_yolu_dokunmaz(conn, saat, hata):
    from dipbot import outbox
    ad, kw = hata
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="x1",
                        veri={"war_number": 1, "ad": "x"}, simdi=saat())
    once = None

    class G:
        async def gonder(self, sohbet, tur, veri):
            nonlocal once
            outbox.grup_bekleyenlerini_iptal_et(conn, G1)
            once = tuple(conn.execute("SELECT * FROM outbox").fetchone())
            raise outbox.GonderimHatasi(ad, **kw)

    run(outbox.bekleyenleri_gonder(conn, G(), saat))
    assert tuple(conn.execute("SELECT * FROM outbox").fetchone()) == once
    assert conn.execute("SELECT durum FROM outbox").fetchone()[0] == "iptal"
    # sonraki turlarda da (claim/lease süresi dolsa bile) iptal satıra gönderim/değişiklik yok
    saat.ilerle(10_000)
    run(outbox.bekleyenleri_gonder(conn, G(), saat))
    assert tuple(conn.execute("SELECT * FROM outbox").fetchone()) == once


# ------------------------------------------------------------------ E11 (yalnız geçici dosya temizliği) / E12
def test_e11_acilista_eski_gecici_token_dosyalari_temizlenir(tmp_path):
    from dipbot.tg.token import TokenDeposu
    d = TokenDeposu(tmp_path / "diplomacia.token")
    d.yaz(JWT_SAHTE)
    (tmp_path / ".token-abc123").write_text("x")
    (tmp_path / ".token-zzz").write_text("y")
    (tmp_path / "baska.txt").write_text("z")
    assert d.eski_gecicileri_temizle() == 2
    assert not list(tmp_path.glob(".token-*"))
    assert (tmp_path / "baska.txt").exists() and d.oku() == JWT_SAHTE


def test_e12_es_zamanli_ikinci_token_gonderimi_reddedilir(conn, src, saat, tmp_path):
    from test_telegram_arayuz import _token_ui, dugme
    from dipbot.tg.modeller import Olay
    from dipbot.tg.token import DogrulamaSonucu
    kapi = asyncio.Event()
    cagri = []

    class D:
        async def dogrula(self, token):
            cagri.append(token)
            await kapi.wait()
            return DogrulamaSonucu("gecerli", "x")

    ui, depo, z = _token_ui(conn, src, saat, tmp_path, D())

    def olay(t):
        return Olay(sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU, metin=t)

    async def sen():
        await ui.buton(dugme("tn"))
        ilk = asyncio.ensure_future(ui._token_isle(olay(JWT_SAHTE)))
        await asyncio.sleep(0.02)
        ikinci = await ui._token_isle(olay("eyJIKINCI.TOKEN2.TOKEN3"))
        kapi.set()
        return await ilk, ikinci

    ilk, ikinci = run(sen())
    assert "Devam eden doğrulama" in ikinci.metin and cagri == [JWT_SAHTE]
    assert "kaydedildi" in ilk.metin
