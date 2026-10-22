"""Tur D: C1 savaş bildirim seli/tanınmayan durum/güvence notu, C2 parti_ekle bloklamaz. MOCK."""
from conftest import G1, bildirimler, run, takipte

from dipbot import config
from dipbot.kaynak import SavasOzet


def _s(i, durum="active", tani=False):
    return SavasOzet(f"id-{i:03d}", f"Savas {i}", durum=durum, durum_taninmiyor=tani, war_number=1000 + i)


# ---------------------------------------------------------------- C1
def test_c1_baseline_sonrasi_secim_buyurse_tek_ozet(conn, servis, src):
    src.savaslar = [_s(0)]
    run(servis.savaslari_tara(G1, src))
    src.savaslar = [_s(i) for i in range(13)]  # 12 yeni
    run(servis.savaslari_tara(G1, src))
    assert config.YENI_SAVAS_BILDIRIM_UST == 5
    assert bildirimler(conn, "yeni_savas") == []
    ozet = bildirimler(conn, "yeni_savas_ozet")
    assert len(ozet) == 1 and ozet[0]["veri"]["sayi"] == 12
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 13
    run(servis.savaslari_tara(G1, src))  # tekrar bildirim yok
    assert len(bildirimler(conn, "yeni_savas_ozet")) == 1


def test_c1_sinir_altinda_ayri_bildirim(conn, servis, src):
    src.savaslar = [_s(0)]
    run(servis.savaslari_tara(G1, src))
    src.savaslar = [_s(i) for i in range(6)]  # 5 yeni = sınır
    run(servis.savaslari_tara(G1, src))
    assert len(bildirimler(conn, "yeni_savas")) == 5
    assert bildirimler(conn, "yeni_savas_ozet") == []


def test_c1_taninmayan_durum_yeni_savas_bildirimi_uretmez(conn, servis, src):
    src.savaslar = [_s(0)]
    run(servis.savaslari_tara(G1, src))
    src.savaslar = [_s(0), _s(1, "ended", True), _s(2)]
    r = run(servis.savaslari_tara(G1, src))
    assert [b["veri"]["savas_id"] for b in bildirimler(conn, "yeni_savas")] == ["id-002"]
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE savas_id='id-001'").fetchone()[0] == 1
    assert any("unrecognized" in u for u in r.uyarilar)
    sayac = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (f"savas_taninmayan:{G1}",)).fetchone()
    assert sayac and int(sayac[0]) == 1
    run(servis.savaslari_tara(G1, src))  # tek seferlik: sayaç artmaz
    sayac = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (f"savas_taninmayan:{G1}",)).fetchone()
    assert int(sayac[0]) == 1


# ---------------------------------------------------------------- C1c / C6a arayüz
import pytest  # noqa: E402
from tg_fakes import KURUCU, YETKILI, grup_komut  # noqa: E402

from dipbot import yonetim  # noqa: E402
from dipbot.kaynak import SavasDetay, SavasTaraf  # noqa: E402
from dipbot.tg.arayuz import Arayuz  # noqa: E402


@pytest.fixture
def ui(conn, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return Arayuz(conn, src, KURUCU, saat=saat)


def test_c1c_savas_listesi_ve_durumda_guvence_notu(ui, src):
    src.savaslar = [_s(1)]
    liste = run(ui.komut(grup_komut("savaslar"))).metin
    assert "the list is assumed to be unpaginated (unverified)" in liste
    durum = run(ui.komut(grup_komut("durum"))).metin
    # the redesigned status screen intentionally no longer carries the pagination note
    assert "unpaginated" not in durum


def test_c1c_guc_etiketi_hasar_notu(ui, src):
    src.savas_detay["s1"] = SavasDetay("s1", "Savaş", "Aktif", (SavasTaraf("A", 10), SavasTaraf("B", 20)))
    y = run(ui.buton(__import__("tg_fakes").grup_dugme("sd:s1"))).metin
    assert "Power" in y and "has not been verified that the 'Power' value represents damage" in y


def test_c1b_durum_ekraninda_tanınmayan_sayaci(ui, conn, servis, src):
    src.savaslar = [_s(0)]
    run(servis.savaslari_tara(G1, src))
    src.savaslar = [_s(0), _s(1, "ended", True)]
    run(servis.savaslari_tara(G1, src))
    assert "Wars with an unrecognized status: 1" in run(ui.komut(grup_komut("durum"))).metin


def test_c6a_durum_toplu_bulunamiyor_ozeti(ui, conn, servis, src):
    for o in ("1", "2", "3"):
        src.oyuncu(o, "A" + o)
        run(servis.oyuncu_ekle(G1, o, src))
    del src.oyuncular["2"]
    del src.oyuncular["3"]
    for _ in range(config.NOT_FOUND_STREAK_THRESHOLD):
        run(servis.oyuncular_tara(G1, src))
    assert servis.bulunamayan_oyuncular(G1) == ["2", "3"]
    assert "Not found (consecutive 404s): 2 players" in run(ui.komut(grup_komut("durum"))).metin


# ---------------------------------------------------------------- C2
def test_c2_parti_ekle_uye_ilk_verisini_sinirli_alir_bloke_etmez(conn, servis, src):
    """BİLİNÇLİ DEĞİŞİKLİK (kullanıcı talimatı: parti eklerken ilk oyuncu verileri): önceki davranış üye başına
    HİÇ fetch_player yapmıyordu. Artık en çok PARTI_EKLE_ILK_VERI_UST üye için, sınırlı eşzamanlılıkla ilk veri
    alınır; kalanı ilk taramaya bırakılır (ayrıntı: tests/test_ilk_gozlem.py)."""
    uyeler = [(str(100 + i), f"U{i}") for i in range(40)]
    for oid, ad in uyeler:
        src.oyuncu(oid, ad)
    src.parti("5830", "TEKNO", uyeler)
    s = run(servis.parti_ekle(G1, "5830", src))
    assert src.cagri_say("oyuncu") == 40 <= config.PARTI_EKLE_ILK_VERI_UST and src.cagri_say("parti") == 1
    assert (s.durum, s.uye_sayisi, s.yeni_eklenen, s.zaten_takipte) == ("eklendi", 40, 40, 0)
    assert (s.ilk_veri_alinan, s.ilk_veri_bekleyen) == (40, 0)
    assert len(takipte(conn, G1)) == 40
    assert bildirimler(conn) == []
    # ilk veri zaten alındı: ilk tarama değişiklik/bildirim üretmez
    r = run(servis.oyuncular_tara(G1, src))
    assert r.ok and bildirimler(conn) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=? AND baseline_tamam=1 AND bolge IS NOT NULL", (G1,)
    ).fetchone()[0] == 40


# ---------------------------------------------------------------- C5
SAHTE_JWT = "eyJSAHTE1.SAHTE2xx.SAHTE3xx"


def test_c5_jwt_kalibi_ve_calisma_aninda_okunan_token_maskelenir(tmp_path, caplog):
    import logging

    from dipbot import maske
    from dipbot.tg.gunluk import SirFiltresi
    from dipbot.tg.token import TokenDeposu

    assert SAHTE_JWT not in maske.maskele(f"hata: istek reddedildi {SAHTE_JWT} ayrıntı")
    # JWT kalıbına uymayan, özel başlıkla giden değer: depodan okununca filtre bellekte öğrenir
    ozel = "opak-sahte-deger-1234567890"
    d = TokenDeposu(tmp_path / "t")
    d.yaz(ozel)
    d.oku()
    assert ozel not in maske.maskele(f"X-Ozel: {ozel} reddedildi")
    log = logging.getLogger("c5test")
    f = SirFiltresi()
    log.addFilter(f)
    with caplog.at_level(logging.ERROR, logger="c5test"):
        try:
            raise RuntimeError(f"401 {SAHTE_JWT} {ozel}")
        except RuntimeError:
            log.exception("istek hatası %s", SAHTE_JWT)
    for r in caplog.records:
        metin = r.getMessage() + (r.exc_text or "")
        assert SAHTE_JWT not in metin and ozel not in metin
    assert ozel not in repr(d) and ozel not in repr(vars(d))


# ---------------------------------------------------------------- C4 / C6b
from conftest import KURUCU as _K  # noqa: E402
from dipbot.tg.modeller import Olay  # noqa: E402
from tg_fakes import dugme, grup_dugme, onay_kimligi  # noqa: E402

JWT_SAHTE = "eyJSAHTE.SAHTE.SAHTE"


def _metin_olay(metin, sil, kul=_K, tur="private"):
    return Olay(sohbet_id=kul, sohbet_turu=tur, kullanici_id=kul, metin=metin, mesaj_id=7, sil=sil)


def test_c4a_beklenmeyen_jwt_silinir_islenmez_ve_deger_sizmaz(ui, conn):
    silindi = []

    async def sil():
        silindi.append(1)
        return True

    y = run(ui.metin(_metin_olay("eyJSAHTExx.SAHTExx.SAHTExx", sil)))
    assert y is not None and silindi == [1]
    assert "First choose Renew Token from the menu; this message was not processed" in y.metin
    assert "SAHTExx" not in y.metin
    dump = "".join(str(tuple(r)) for t in ("outbox", "sistem_durumu", "yetkisiz_denemeler")
                   for r in conn.execute(f"SELECT * FROM {t}"))
    assert "SAHTExx" not in dump
    # JWT olmayan düz metin: yok sayılır, silinmez
    silindi.clear()
    assert run(ui.metin(_metin_olay("merhaba", sil))) is None and silindi == []


def test_c4b_token_bekleme_yeniden_baslatmada_gecersiz(conn, src, saat):
    """BİLİNÇLİ DEĞİŞİKLİK (Tur F2b/D7, kullanıcı kararı): yeniden başlatmada bekleyen token-bekleme
    GEÇERSİZDİR (önceden sürerdi). Monotonic başlangıç kurulamadığı için geçerlilik belirsizdir; kullanıcı
    menüden yeniden başlatır. Ayrıntılı D7 testleri: tests/test_tur_f2b.py."""
    ui1 = Arayuz(conn, src, KURUCU, saat=saat)
    run(ui1.buton(dugme("tn")))
    ui2 = Arayuz(conn, src, KURUCU, saat=saat)  # süreç yeniden başladı
    assert run(ui2.metin(_metin_olay("iki kelime", None))) is None  # bekleme yok: düz metin yok sayılır
    assert conn.execute("SELECT COUNT(*) FROM sistem_durumu WHERE anahtar='token_bekleme'").fetchone()[0] == 0


def test_c4b_suresi_dolan_bekleme_yeniden_baslatmada_gecersiz(conn, src, saat):
    ui1 = Arayuz(conn, src, KURUCU, saat=saat)
    run(ui1.buton(dugme("tn")))
    saat.ilerle(3600)
    assert run(Arayuz(conn, src, KURUCU, saat=saat).metin(_metin_olay("iki kelime", None))) is None


def test_c6b_onay_aninda_yetkisi_alinan_kullanici_butonla_parti_kaldiramaz(ui, conn, src, saat):
    for o in ("1", "2"):
        src.oyuncu(o, "A" + o)
    src.parti("5830", "TEKNO", [("1", "A1"), ("2", "A2")])
    run(ui.komut(grup_komut("parti_ekle", "5830", kul=YETKILI)))
    y = run(ui.komut(grup_komut("parti_sil", "5830", kul=YETKILI)))
    kid = onay_kimligi(y)
    yonetim.yetki_al(conn, saat, KURUCU, KURUCU, YETKILI)
    run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))
    assert takipte(conn, G1) == {"1", "2"}
    assert conn.execute("SELECT COUNT(*) FROM takip_partiler WHERE group_id=?", (G1,)).fetchone()[0] == 1


# ---------------------------------------------------------------- C7 (belgeleyici sınır testleri)
def test_c7_o7_donem_sayac_tabanli_saat_geri_atlasa_da_taze_satir_gider(conn, saat, gonderici):
    """BİLİNÇLİ DEĞİŞİKLİK (Tur F2b/D7, kullanıcı kararı): onay dönemi artık zaman damgası
    karşılaştırması (`olusturuldu >= onaylandi`) değil artan tamsayı sayaçtır. Önceki belgeleyici test,
    saat geri atlayınca TAZE satırın bayat sayılıp iptal edilmesini 'bilinen sınır' diye sabitliyordu;
    artık güncel dönemde üretilen her satır, duvar saati ne olursa olsun gider. Bayat dönem testleri:
    tests/test_tur_f2b.py."""
    from dipbot import outbox, yonetim

    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    saat.ilerle(60)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="ayni",
                        veri={"savas_id": "sA"}, simdi=saat())  # aynı damga
    saat.duvar_atla(-30)  # duvar saati geri atladı
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="geri",
                        veri={"savas_id": "sB"}, simdi=saat())
    saat.duvar_atla(30)
    o = run(outbox.bekleyenleri_gonder(conn, gonderici, saat))
    assert sorted(g[2]["savas_id"] for g in gonderici.gonderilen) == ["sA", "sB"]
    assert o.gonderilen == 2 and o.iptal == 0


def test_c7_cok_parcali_mesajda_ikinci_parca_hatasi_birinci_parcayi_tekrarlar():
    """Bilinen sınır: gonder() parçaları sırayla yollar; 2. parça geçici hata verirse satır yeniden
    denenir ve 1. parça TEKRAR gider (parça düzeyinde kalıcı ilerleme yok)."""
    from telegram.error import NetworkError
    from tg_fakes import SahteBot, hizli_gonderici

    bot = SahteBot()
    g = hizli_gonderici(bot)
    kat = [{"oyuncu_id": str(i), "ad": f"Oyuncu {i} " + "ü" * 40} for i in range(300)]
    veri = {"parti_id": "1", "parti_ad": "P", "katilanlar": kat, "ayrilanlar": [],
            "tespit": "2026-10-19T12:00:00.000000Z"}
    bot.hatalar = [None]  # yer tutucu kaldırılacak
    bot.hatalar.clear()

    async def dene():
        # 1. çağrı başarılı, 2. çağrı NetworkError
        orj = bot._kayit
        n = {"i": 0}

        async def kayit(ad, **kw):
            n["i"] += 1
            if n["i"] == 2:
                bot.cagrilar.append((ad, kw))
                raise NetworkError("gecici")
            return await orj(ad, **kw)

        bot._kayit = kayit
        try:
            await g.gonder(G1, "parti_uyelik", veri)
        except Exception:
            pass
        bot._kayit = orj
        await g.gonder(G1, "parti_uyelik", veri)

    run(dene())
    metinler = [kw["text"] for a, kw in bot.cagrilar if a == "send_message"]
    assert metinler.count(metinler[0]) == 2  # 1. parça iki kez gönderildi
