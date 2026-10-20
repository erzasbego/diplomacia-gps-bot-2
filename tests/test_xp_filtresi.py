"""Parti üyeleri için XP süzgeci (>= 1.500.000): ayrıştırma, otomatik takip, kademeli çıkarma, korumalar,
bildirimler, başlangıç özeti ve şema v6. MOCK; gerçek Diplomacia davranışı DOĞRULANMADI."""
import copy
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte
from tg_fakes import grup_komut

from dipbot import config, db, yonetim
from dipbot.diplomacia import ayristir as a
from dipbot.takip import TakipServisi
from dipbot.tg.arayuz import Arayuz
from fakes import SahteKaynak, SahteSaat

FX = json.loads((Path(__file__).parent / "fixtures" / "diplomacia_ornek.json").read_text(encoding="utf-8"))
ESIK = 1_500_000


def test_esik_yapilandirmasi():
    assert config.PARTI_XP_ESIGI == ESIK
    assert config.PARTI_XP_ALTI_ARDISIK_TARAMA >= 2, "tek geçici yanıt silmemeli"


# ============================================================ A) XP ayrıştırma (adaptör)
def _govde(xp_listesi):
    taban = copy.deepcopy(FX["party_members_response"]["members"][0])
    uyeler = []
    for i, xp in enumerate(xp_listesi, start=1):
        m = copy.deepcopy(taban)
        m.update(player_id=str(uuid.UUID(int=20_000 + i)), player_number=i, username=f"U{i}")
        if xp == "YOK":
            m.pop("xp", None)
        else:
            m["xp"] = xp
        uyeler.append(m)
    return {"members": uyeler}


@pytest.mark.parametrize("ham,beklenen", [
    (1_500_000, 1_500_000), (0, 0), (1_499_999, 1_499_999), (2_214_953, 2_214_953),
    (1_500_000.0, 1_500_000),                        # tam sayı değerli kayan nokta
    ("YOK", None), (None, None), ("", None), ("1500000", None), ("abc", None),
    (True, None), (False, None), (-1, None), (-1_500_000, None),
    (1_500_000.5, None), (float("nan"), None), (float("inf"), None), (10**15, None),
    ([], None), ({}, None),
])
def test_xp_ayristirma_gecerli_ve_gecersiz_degerler(ham, beklenen):
    u = a.uyeler_ayristir(_govde([ham]), 1)
    assert u.uyeler[0].xp == beklenen


def test_xp_ayristirma_diger_uyeleri_etkilemez_ve_sira_korunur():
    u = a.uyeler_ayristir(_govde([3_000_000, "YOK", 1_000, "x", 1_500_000]), 5)
    assert [m.xp for m in u.uyeler] == [3_000_000, None, 1_000, None, 1_500_000]
    assert u.tamam is True, "XP eksikliği üye listesini bozuk saymaz (yalnız oyuncu o an 'bilinmiyor')"


def test_ornek_yanit_xp_degerlerini_okur():
    u = a.uyeler_ayristir(FX["party_members_response"], 2)
    assert [m.xp for m in u.uyeler] == [2_214_953, 1_765_874]


# ============================================================ B) çekirdek yardımcıları
def _src(uyeler, toplam=None, pid="1244"):
    """uyeler: [(id, xp)]; oyuncular API'de tanımlı (Ankara/TR)."""
    src = SahteKaynak()
    for i, _ in uyeler:
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    src.parti(pid, "TEKNO", [(str(i), f"U{i}", xp) for i, xp in uyeler], toplam=toplam)
    return src


def _kur(src, g=G1, pid="1244"):
    return src


def _satir(conn, oid, g=G1):
    return conn.execute("SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)).fetchone()


def _tara(servis, src, g=G1):
    return run(servis.partiler_tara(g, src))


# ============================================================ C) sınır değerleri ve eksik/geçersiz XP
@pytest.mark.parametrize("xp,takipte_mi", [(1_499_999, False), (1_500_000, True), (1_500_001, True)])
def test_xp_siniri_1_499_999_1_500_000_1_500_001(conn, servis, xp, takipte_mi):
    src = _src([(1, xp)])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert (takipte(conn, G1) == {"1"}) is takipte_mi
    assert s.xp_uygun == int(takipte_mi) and s.xp_alti == int(not takipte_mi) and s.xp_bilinmeyen == 0
    assert bildirimler(conn) == []


def test_xp_eksik_veya_gecersiz_otomatik_eklenmez_bilinmiyor_sayilir(conn, servis):
    src = _src([(1, None), (2, 3_000_000), (3, None)])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == {"2"}
    assert (s.incelenen, s.xp_uygun, s.xp_alti, s.xp_bilinmeyen) == (3, 1, 0, 2)
    assert conn.execute("SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=?", (G1,)).fetchone()[0] == 3, \
        "üyelik listesi tüm incelenenleri tutar (XP süzgeci takibi süzer, üyeliği değil)"


def test_sirali_oldugu_varsayilmaz_dusuk_xp_arkasindaki_nitelikliler_de_eklenir(conn, servis):
    # sıra bozuk: düşük XP'ler başta, nitelikliler sonda
    src = _src([(1, 10), (2, 1_499_999), (3, 1_500_000), (4, 5), (5, 9_000_000)])
    run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == {"3", "5"}


def test_yalniz_nitelikli_yeni_uyelerin_ilk_verisi_cekilir(conn, servis):
    src = _src([(1, 2_000_000), (2, 100), (3, None), (4, 1_500_000)])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert {c[1] for c in src.cagrilar if c[0] == "oyuncu"} == {"1", "4"}
    assert (s.yeni_eklenen, s.ilk_veri_alinan, s.ilk_veri_bekleyen) == (2, 2, 0)


# ============================================================ D) 75 / 129 / 329 üyeli partiler
def _karma(n, nitelikli, bilinmeyen=0, bas=1):
    """n üye: ilk `nitelikli` tanesi >= eşik, `bilinmeyen` tanesi XP'siz, kalanı eşik altı; karışık sırada."""
    xs = []
    for k in range(n):
        if k < nitelikli:
            xs.append(ESIK + 1000 + k)
        elif k < nitelikli + bilinmeyen:
            xs.append(None)
        else:
            xs.append(ESIK - 1 - k)
    ters = list(reversed(xs))  # sırayı bozar: XP sıralamasına güvenilmediğini kanıtlar
    return [(bas + i, xp) for i, xp in enumerate(ters)]


@pytest.mark.parametrize("n,toplam,nitelikli,bilinmeyen", [(75, None, 40, 5), (100, None, 63, 0), (100, 129, 63, 2), (100, 329, 63, 0)])
def test_75_100_129_329_uyeli_partilerde_yalniz_nitelikliler_takibe_alinir(conn, servis, n, toplam, nitelikli, bilinmeyen):
    uyeler = _karma(n, nitelikli, bilinmeyen)
    src = _src(uyeler, toplam=toplam)
    s = run(servis.parti_ekle(G1, "1244", src))
    beklenen = {str(i) for i, xp in uyeler if xp is not None and xp >= ESIK}
    assert takipte(conn, G1) == beklenen and len(beklenen) == nitelikli
    assert (s.incelenen, s.toplam_uye, s.xp_uygun, s.xp_bilinmeyen) == (n, toplam or n, nitelikli, bilinmeyen)
    assert s.xp_alti == n - nitelikli - bilinmeyen
    assert bildirimler(conn) == []
    assert sum(1 for c in src.cagrilar if c[0] == "parti") == 1, "ek sayfa isteği yok"


# ============================================================ E) XP değişimi: kademeli çıkarma, geçici yanıt koruması
def _kur_tek(conn, servis, xp=2_000_000):
    src = _src([(1, xp)])
    run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == {"1"}
    return src


def _xp_ayarla(src, xp, oid="1"):
    uy = [(u.oyuncu_id, u.ad, xp if u.oyuncu_id == oid else u.xp) for u in src.partiler["1244"].uyeler]
    src.parti("1244", "TEKNO", uy, toplam=src.partiler["1244"].toplam_uye)


def test_tek_gecici_dusuk_xp_yaniti_oyuncuyu_silmez_sayac_sifirlanir(conn, servis):
    src = _kur_tek(conn, servis)
    _xp_ayarla(src, 10)
    _tara(servis, src)
    assert "1" in takipte(conn, G1) and _satir(conn, "1")["xp_alti_sayac"] == 1
    _xp_ayarla(src, 2_000_000)  # normale döndü
    _tara(servis, src)
    assert _satir(conn, "1")["xp_alti_sayac"] == 0
    for _ in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA - 1):  # yeniden düşük ama yine de ardışık eşiğin altında
        _xp_ayarla(src, 10)
        _tara(servis, src)
    assert "1" in takipte(conn, G1)
    assert bildirimler(conn) == [], "XP düşüşü hiçbir bildirim üretmez"


def test_ardisik_n_tarama_esigin_altinda_ise_otomatik_oyuncu_takipten_cikar(conn, servis):
    src = _kur_tek(conn, servis)
    _xp_ayarla(src, 1_499_999)
    for i in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA):
        assert "1" in takipte(conn, G1), f"{i}. taramada henüz silinmemeli"
        _tara(servis, src)
    assert "1" not in takipte(conn, G1)
    assert bildirimler(conn) == [], "sahte ayrılma bildirimi yok"
    assert conn.execute("SELECT COUNT(*) FROM engeller WHERE group_id=?", (G1,)).fetchone()[0] == 0, "engel konmaz"
    # sonradan eşiğe ulaşırsa yeniden (sessizce) takibe alınır
    _xp_ayarla(src, ESIK)
    _tara(servis, src)
    assert "1" in takipte(conn, G1) and bildirimler(conn) == []


def test_eksik_xp_yaniti_sayaci_ne_arttirir_ne_sifirlar(conn, servis):
    src = _kur_tek(conn, servis)
    _xp_ayarla(src, 5)
    _tara(servis, src)
    _xp_ayarla(src, None)
    for _ in range(10):
        _tara(servis, src)
    assert "1" in takipte(conn, G1) and _satir(conn, "1")["xp_alti_sayac"] == 1


def test_sonradan_esige_ulasan_oyuncu_otomatik_takibe_alinir_ilk_gozlem_sessiz(conn, servis):
    src = _src([(1, 2_000_000), (2, 1_000_000)])
    src.oyuncu("2", "U2", None, None)  # bölge/vatandaşlık henüz bilinmiyor
    run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == {"1"}
    _xp_ayarla(src, 1_500_000, oid="2")
    src.oyuncu("2", "U2", "Ankara", "TR")
    _tara(servis, src)
    assert "2" in takipte(conn, G1) and _satir(conn, "2")["baseline_tamam"] == 0
    assert bildirimler(conn) == [], "katılım/XP bildirimi yok"
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn) == [] and _satir(conn, "2")["bolge"] == "Ankara", "ilk bölge/vatandaşlık sessiz"
    src.oyuncu("2", "U2", "Izmir", "TR")  # sonraki GERÇEK değişiklik bildirilir
    run(servis.oyuncular_tara(G1, src))
    v = bildirimler(conn, "oyuncu_degisti")
    assert len(v) == 1 and v[0]["veri"]["degisimler"] == [{"alan": "bolge", "eski": "Ankara", "yeni": "Izmir"}]


# ============================================================ F) korumalar
def test_elle_eklenen_oyuncu_xp_siniri_altinda_bile_korunur(conn, servis):
    src = _src([(1, 10)])
    run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == set()
    run(servis.oyuncu_ekle(G1, "1", src))  # elle
    for _ in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA + 3):
        _tara(servis, src)
    assert takipte(conn, G1) == {"1"}


def test_otomatik_oyuncu_sonradan_elle_eklenirse_artik_korunur(conn, servis):
    src = _kur_tek(conn, servis)
    run(servis.oyuncu_ekle(G1, "1", src))
    _xp_ayarla(src, 5)
    for _ in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA + 2):
        _tara(servis, src)
    assert "1" in takipte(conn, G1)


def test_baska_takipli_partide_nitelikli_olan_oyuncu_korunur(conn, servis):
    src = _src([(1, 2_000_000)])
    src.parti("77", "DIGER", [("1", "U1", 2_500_000)])
    run(servis.parti_ekle(G1, "1244", src)); run(servis.parti_ekle(G1, "77", src))
    _xp_ayarla(src, 5)  # 1244'te düşük görünüyor ama 77'de nitelikli
    for _ in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA + 2):
        _tara(servis, src)
    assert "1" in takipte(conn, G1)


def test_baska_partide_de_dusukse_korunmaz(conn, servis):
    src = _src([(1, 2_000_000)])
    src.parti("77", "DIGER", [("1", "U1", 2_500_000)])
    run(servis.parti_ekle(G1, "1244", src)); run(servis.parti_ekle(G1, "77", src))
    _xp_ayarla(src, 5)
    src.parti("77", "DIGER", [("1", "U1", 5)])
    for _ in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA + 2):
        _tara(servis, src)
    assert "1" not in takipte(conn, G1)


def test_xp_filtresinden_onceki_mevcut_otomatik_kayitlar_korunur_nitelikli_gorunce_devreye_girer(conn, servis):
    """Mevcut canlı kayıtlar: bu sürümden önce otomatik eklenmiş (xp_onayli=0) oyuncular XP düşük görünse bile
    SİLİNMEZ; yalnız bir kez nitelikli görüldükten sonra XP kuralına tabi olur."""
    src = _src([(1, 10)])
    run(servis.oyuncu_ekle(G1, "1", src))
    conn.execute("UPDATE takip_oyuncular SET elle_eklendi=0, xp_onayli=0, xp_alti_sayac=0")  # eski otomatik kayıt
    run(servis.parti_ekle(G1, "1244", src))  # parti şimdi eklendi; 1 listede ve XP düşük
    for _ in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA + 3):
        _tara(servis, src)
    assert "1" in takipte(conn, G1), "mevcut kayıt korunur"
    _xp_ayarla(src, 3_000_000)
    _tara(servis, src)
    assert _satir(conn, "1")["xp_onayli"] == 1
    _xp_ayarla(src, 10)
    for _ in range(config.PARTI_XP_ALTI_ARDISIK_TARAMA):
        _tara(servis, src)
    assert "1" not in takipte(conn, G1)


def test_gruplar_bagimsiz_xp_sayaci_baska_grubu_etkilemez(conn, servis):
    src = _src([(1, 2_000_000)])
    run(servis.parti_ekle(G1, "1244", src)); run(servis.parti_ekle(G2, "1244", src))
    _xp_ayarla(src, 5)
    _tara(servis, src, G1)
    assert _satir(conn, "1", G1)["xp_alti_sayac"] == 1 and _satir(conn, "1", G2)["xp_alti_sayac"] == 0


# ============================================================ G) üyelik bildirimleri: sahte ayrılma/katılma yok
def test_ilk_100e_giris_cikis_ve_xp_gecisleri_sinirli_partide_bildirim_uretmez_ve_kimse_silinmez(conn, servis):
    ilk = [(i, 2_000_000 + i) for i in range(1, 101)]
    src = _src(ilk, toplam=329)
    run(servis.parti_ekle(G1, "1244", src))
    yeni = [(i, 2_000_000 + i) for i in range(3, 103)] + []  # 1,2 listeden çıktı; 101,102 girdi
    src2 = _src(yeni, toplam=329)
    src.partiler["1244"] = src2.partiler["1244"]
    src.oyuncular.update(src2.oyuncular)
    _tara(servis, src)
    assert bildirimler(conn, "parti_uyelik") == []
    assert {"1", "2"} <= takipte(conn, G1), "ilk 100'den çıkmak takipten çıkarmaz (XP kanıtı yok)"
    assert {"101", "102"} <= takipte(conn, G1), "ilk 100'e giren nitelikli sessizce takibe alınır"
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn) == []


def test_tam_listede_dusuk_xp_katilimci_bildirilmez_nitelikli_katilimci_ve_takipli_ayrilan_bildirilir(conn, servis):
    src = _src([(1, 2_000_000), (2, 2_000_000), (3, 100)])
    run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == {"1", "2"}
    for i in (10, 11):
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    src.parti("1244", "TEKNO", [("1", "U1", 2_000_000), ("3", "U3", 100), ("10", "U10", 50), ("11", "U11", 9_000_000)])
    _tara(servis, src)  # 2 ayrıldı (takipli), 10 katıldı (düşük XP), 11 katıldı (nitelikli)
    v = bildirimler(conn, "parti_uyelik")
    assert len(v) == 1
    assert [x["oyuncu_id"] for x in v[0]["veri"]["katilanlar"]] == ["11"]
    assert [x["oyuncu_id"] for x in v[0]["veri"]["ayrilanlar"]] == ["2"]
    assert takipte(conn, G1) == {"1", "2", "11"}, "ayrılan takipte kalır (spec); düşük XP'li katılımcı eklenmez"


def test_yalniz_dusuk_xp_uyeler_degisirse_hic_bildirim_olmaz(conn, servis):
    src = _src([(1, 2_000_000), (3, 100), (4, 200)])
    run(servis.parti_ekle(G1, "1244", src))
    src.oyuncu("9", "U9", "A", "TR")
    src.parti("1244", "TEKNO", [("1", "U1", 2_000_000), ("9", "U9", 10)])  # 3,4 ayrıldı (takipsiz), 9 katıldı (düşük)
    _tara(servis, src)
    assert bildirimler(conn) == []


# ============================================================ H) başlangıç özeti (Telegram)
@pytest.fixture
def ui(conn, src, saat):
    return Arayuz(conn, src, KURUCU, saat=saat)


def test_ozet_ornek_senaryo_gercek_sayilar_hesaplanir(conn, ui, src, monkeypatch):
    monkeypatch.setattr(config, "PARTI_EKLE_ILK_VERI_UST", 40)
    uyeler = _karma(100, 63)
    for i, _ in uyeler:
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    src.parti("1319", "Örnek Parti", [(str(i), f"U{i}", xp) for i, xp in uyeler], toplam=329)
    y = run(ui.komut(grup_komut("partiekle", "1319")))
    m = y.metin
    for satir in ("✅ <b>Parti takibe eklendi</b>", "Toplam üye: 329", "API'den incelenen: 100",
                  "1.500.000+ XP ile takibe alınan: 63", "XP sınırı altında: 37",
                  "İlk bilgisi alınan: 40", "İlk taramayı bekleyen: 23"):
        assert satir in m, satir
    assert "Örnek Parti" in m
    assert "229 üye" in m and "kontrol edilmedi" in m, "ilk 100 dışının kontrol edilmediği açıkça belirtilir"
    assert "bilgisi eksik" not in m
    assert m.count("Parti takibe eklendi") == 1
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0, "oyuncular için ayrı mesaj yok"


def test_ozet_bilinmeyen_xp_engelli_ve_75_uyeli_parti(conn, ui, src, servis):
    uyeler = _karma(75, 30, bilinmeyen=4)
    for i, _ in uyeler:
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    src.parti("1244", "P", [(str(i), f"U{i}", xp) for i, xp in uyeler])
    m = run(ui.komut(grup_komut("partiekle", "1244"))).metin
    assert "Toplam üye: 75" in m and "API'den incelenen: 75" in m
    assert "1.500.000+ XP ile takibe alınan: 30" in m and "XP sınırı altında: 41" in m
    assert "XP bilgisi eksik/geçersiz (takibe alınmadı): 4" in m
    assert "kontrol edilmedi" not in m, "75 üyeli partide 'ilk 100 dışı' notu çıkmaz"


# ============================================================ I) şema v6: mevcut kayıtlar korunur
@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="DROP COLUMN için SQLite >= 3.35")
def test_v5_veritabani_v6ya_gecerken_kayitlar_korunur(tmp_path):
    yol = tmp_path / "canli.db"
    saat = SahteSaat()
    c = db.baglan(yol)
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G1)
    src = _src([(1, 2_000_000), (2, 5)])
    run(TakipServisi(c, saat).parti_ekle(G1, "1244", src))
    run(TakipServisi(c, saat).oyuncu_ekle(G1, "2", src))
    c.execute("ALTER TABLE parti_uyeleri DROP COLUMN xp")
    c.execute("ALTER TABLE takip_oyuncular DROP COLUMN xp_alti_sayac")
    c.execute("ALTER TABLE takip_oyuncular DROP COLUMN xp_onayli")
    c.execute("PRAGMA user_version=5")
    c.close()
    c = db.baglan(yol)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SEMA_SURUMU == 6
    assert takipte(c, G1) == {"1", "2"}
    r = c.execute("SELECT * FROM takip_oyuncular WHERE oyuncu_id='1'").fetchone()
    assert (r["xp_alti_sayac"], r["xp_onayli"]) == (0, 0), "mevcut kayıtlar korunur; XP kuralına henüz tabi değil"
    assert c.execute("SELECT COUNT(*) FROM parti_uyeleri WHERE xp IS NULL").fetchone()[0] == 2
    c.close()
    c = db.baglan(yol)  # idempotent
    assert takipte(c, G1) == {"1", "2"}
    c.close()


def test_eski_kod_ek_sutunlari_bilmeden_oyuncu_ve_uye_ekleyebilir(conn):
    conn.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, eklendi) VALUES(?,?,?)", (G1, "9", "t"))
    conn.execute("INSERT INTO parti_uyeleri(group_id, parti_id, oyuncu_id, ad) VALUES(?,?,?,?)", (G1, "p", "9", "x"))
    r = _satir(conn, "9")
    assert (r["xp_alti_sayac"], r["xp_onayli"]) == (0, 0)
    assert conn.execute("SELECT xp FROM parti_uyeleri").fetchone()[0] is None
