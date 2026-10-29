"""İlk gözlem sessizliği (alan bazında), parti eklerken ilk veri, tek başlangıç özeti, yeniden başlatma ve
çok gruplu bağımsızlık. MOCK (sahte kaynak, SQLite); canlı API doğrulaması DEĞİLDİR."""
import asyncio

import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte
from tg_fakes import grup_komut

from dipbot import config, db, yonetim
from dipbot.kaynak import IstekSiniri, SiteErisilemez, TokenGecersiz
from dipbot.takip import TakipServisi
from dipbot.tg.arayuz import Arayuz
from fakes import SahteKaynak, SahteSaat


def _degisimler(conn, grup=G1):
    """Takip bildirimleri artık log kanalına gider; hangi takip listesine ait olduğu grup_id ile ayrılır."""
    return [d for b in bildirimler(conn, "oyuncu_degisti") if b["grup_id"] == grup for d in b["veri"]["degisimler"]]


def _satir(conn, grup, oid):
    return conn.execute("SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (grup, oid)).fetchone()


# ----------------------------------------------------------------- madde 5: ilk gerçek bilgi bildirim değildir
def test_manuel_ekleme_bolge_bilinmiyor_sonra_ankara_sessiz_sonra_istanbul_bildirir(conn, servis, src):
    src.oyuncu("5", "Ali", None, "TR")
    run(servis.oyuncu_ekle(G1, "5", src))
    assert _satir(conn, G1, "5")["bolge"] is None
    src.oyuncu("5", "Ali", "Ankara", "TR")
    run(servis.oyuncular_tara(G1, src))  # ilk GERÇEK bölge bilgisi
    assert bildirimler(conn, "oyuncu_degisti") == [] and _satir(conn, G1, "5")["bolge"] == "Ankara"
    src.oyuncu("5", "Ali", "İstanbul", "TR")
    run(servis.oyuncular_tara(G1, src))  # gerçek değişiklik
    assert _degisimler(conn) == [{"alan": "bolge", "eski": "Ankara", "yeni": "İstanbul"}]


def test_bolge_ve_vatandaslik_alanlari_ayri_ayri_yonetilir(conn, servis, src):
    src.oyuncu("5", "Ali", "Ankara", None)  # bölge biliniyor, vatandaşlık bilinmiyor
    run(servis.oyuncu_ekle(G1, "5", src))
    src.oyuncu("5", "Ali", "İstanbul", "TR")  # bölge GERÇEK değişti; vatandaşlık İLK kez geldi
    run(servis.oyuncular_tara(G1, src))
    assert _degisimler(conn) == [{"alan": "bolge", "eski": "Ankara", "yeni": "İstanbul"}], \
        "vatandaşlığın ilk bilgisi bildirime karışmaz"
    assert _satir(conn, G1, "5")["vatandaslik_ulkesi"] == "TR"
    src.oyuncu("5", "Ali", "İstanbul", "DE")  # artık vatandaşlık değişimi gerçek
    run(servis.oyuncular_tara(G1, src))
    assert _degisimler(conn)[-1] == {"alan": "vatandaslik_ulkesi", "eski": "TR", "yeni": "DE"}
    assert len(_degisimler(conn)) == 2


@pytest.mark.parametrize("bos", [None, "", "   "])
def test_gecici_bos_deger_bildirim_uretmez_eski_deger_korunur(conn, servis, src, bos):
    src.oyuncu("5", "Ali", "Ankara", "TR")
    run(servis.oyuncu_ekle(G1, "5", src))
    src.oyuncu("5", "Ali", bos, bos)  # API geçici olarak boş döndü
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn, "oyuncu_degisti") == []
    r = _satir(conn, G1, "5")
    assert (r["bolge"], r["vatandaslik_ulkesi"]) == ("Ankara", "TR")
    src.oyuncu("5", "Ali", "Ankara", "TR")  # geri geldi: değişiklik yok
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn, "oyuncu_degisti") == []
    src.oyuncu("5", "Ali", "İzmir", "TR")
    run(servis.oyuncular_tara(G1, src))
    assert len(_degisimler(conn)) == 1


def test_parti_uzerinden_alinan_oyuncu_ilk_gozlemde_sessiz_sonra_gercek_degisiklik_bildirir(conn, servis, src):
    for i in ("1", "2"):
        src.oyuncu(i, "A" + i, None, None)  # parti eklenirken bölge/vatandaşlık henüz yok
    src.parti("1244", "P", [("1", "A1"), ("2", "A2")])
    run(servis.parti_ekle(G1, "1244", src))
    src.oyuncu("1", "A1", "Ankara", "TR"); src.oyuncu("2", "A2", "Bursa", None)
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn) == [] or all(b["tur"] != "oyuncu_degisti" for b in bildirimler(conn))
    src.oyuncu("1", "A1", "İstanbul", "TR"); src.oyuncu("2", "A2", "Bursa", "FR")
    run(servis.oyuncular_tara(G1, src))
    assert sorted((d["alan"], d["eski"], d["yeni"]) for d in _degisimler(conn)) == [
        ("bolge", "Ankara", "İstanbul")]  # A2'nin vatandaşlığı İLK bilgi: sessiz
    assert _satir(conn, G1, "2")["vatandaslik_ulkesi"] == "FR"


def test_mevcut_kayitli_oyuncunun_gercek_degisikligi_susturulmaz(conn, servis, src):
    src.oyuncu("5", "Ali", "Ankara", "TR")
    run(servis.oyuncu_ekle(G1, "5", src))
    src.oyuncu("5", "Ali", "İstanbul", "TR")
    run(servis.oyuncular_tara(G1, src))
    assert len(bildirimler(conn, "oyuncu_degisti")) == 1


def test_baslangic_bilgisi_yeniden_baslatmada_kaybolmaz(tmp_path):
    yol = tmp_path / "t.db"
    saat = SahteSaat()
    c = db.baglan(yol)
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G1)
    src = SahteKaynak()
    src.oyuncu("5", "Ali", None, "TR")
    s1 = TakipServisi(c, saat)
    run(s1.oyuncu_ekle(G1, "5", src))
    src.oyuncu("5", "Ali", "Ankara", "TR")
    run(s1.oyuncular_tara(G1, src))  # ilk gerçek bölge: sessiz, kalıcı
    c.close()
    c = db.baglan(yol)  # süreç yeniden başladı
    s2 = TakipServisi(c, saat)
    run(s2.oyuncular_tara(G1, src))  # aynı bilgi: sessiz
    assert bildirimler(c, "oyuncu_degisti") == []
    src.oyuncu("5", "Ali", "İstanbul", "TR")
    run(s2.oyuncular_tara(G1, src))
    assert [d["yeni"] for d in _degisimler(c)] == ["İstanbul"]
    c.close()


def test_birden_fazla_grupta_ayni_oyuncu_gruplar_bagimsiz(conn, servis, src):
    src.oyuncu("5", "Ali", "Ankara", "TR")
    run(servis.oyuncu_ekle(G1, "5", src))  # G1: baseline Ankara
    src.oyuncu("5", "Ali", None, None)
    run(servis.oyuncu_ekle(G2, "5", src))  # G2 sonradan ekledi, bölgeyi bilmiyor
    src.oyuncu("5", "Ali", "İstanbul", "TR")
    run(servis.oyuncular_tara(G1, src)); run(servis.oyuncular_tara(G2, src))
    b = bildirimler(conn, "oyuncu_degisti")
    assert [x["grup_id"] for x in b] == [G1], "G1'in gerçek değişikliği bildirilir"
    assert {x["hedef_sohbet"] for x in b} == {config.LOG_KANAL_ID}
    assert [x for x in b if x["grup_id"] == G2] == [], "G2 için bu İLK gerçek bilgi: sessiz"
    assert _satir(conn, G2, "5")["bolge"] == "İstanbul"


# ----------------------------------------------------------------- madde 4: parti eklerken ilk oyuncu verileri
def test_parti_eklerken_uye_bolge_vatandaslik_ve_gozlem_zamani_kaydedilir(conn, servis, src):
    for i, (b, v) in {"1": ("Ankara", "TR"), "2": ("Bursa", None), "3": (None, "DE")}.items():
        src.oyuncu(i, "A" + i, b, v)
    src.parti("1244", "P", [(i, "A" + i) for i in "123"])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert (s.yeni_eklenen, s.ilk_veri_alinan, s.ilk_veri_bekleyen) == (3, 3, 0)
    assert (s.bolge_bilinen, s.vatandaslik_bilinen) == (2, 2)
    r = _satir(conn, G1, "1")
    assert (r["bolge"], r["vatandaslik_ulkesi"], r["baseline_tamam"]) == ("Ankara", "TR", 1)
    assert r["son_gozlem"] is not None, "gözlem zamanı kaydedilir"
    assert _satir(conn, G1, "2")["vatandaslik_ulkesi"] is None
    assert bildirimler(conn) == []  # hiçbir bildirim yok


def test_parti_eklerken_uye_istekleri_sinirli_eszamanli(conn, servis, src, monkeypatch):
    monkeypatch.setattr(config, "PARTI_EKLE_ESZAMANLI", 3)
    for i in range(1, 21):
        src.oyuncu(str(i), f"A{i}", "Ankara", "TR")
    src.parti("1244", "P", [(str(i), f"A{i}") for i in range(1, 21)])
    src.gecikme = 0.01
    src.azami = 0
    run(servis.parti_ekle(G1, "1244", src))
    assert 1 < src.azami <= 3, f"eşzamanlılık sınırı aşıldı: {src.azami}"


def test_buyuk_partide_ust_sinir_kalan_ilk_taramaya_birakilir(conn, servis, src, monkeypatch):
    monkeypatch.setattr(config, "PARTI_EKLE_ILK_VERI_UST", 10)
    for i in range(1, 101):
        src.oyuncu(str(i), f"A{i}", "Ankara", "TR")
    src.parti("1244", "P", [(str(i), f"A{i}") for i in range(1, 101)])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert (s.yeni_eklenen, s.ilk_veri_alinan, s.ilk_veri_bekleyen) == (100, 10, 90)
    assert sum(1 for c in src.cagrilar if c[0] == "oyuncu") == 10, "komutta üst sınırdan fazla istek atılmaz"
    assert conn.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=? AND baseline_tamam=0", (G1,)).fetchone()[0] == 90
    assert bildirimler(conn) == []
    # ilk taramada kalan 90'ın ayrıntısı bildirimsiz tamamlanır
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn) == [] and conn.execute(
        "SELECT COUNT(*) FROM takip_oyuncular WHERE group_id=? AND bolge IS NULL", (G1,)).fetchone()[0] == 0


def test_uye_istekleri_hata_verirse_parti_yine_eklenir_eksik_sonra_tamamlanir(conn, servis, src):
    for i in "123":
        src.oyuncu(i, "A" + i, "Ankara", "TR")
    src.hatalar[("oyuncu", "2")] = SiteErisilemez("x")
    src.parti("1244", "P", [(i, "A" + i) for i in "123"])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert s.durum == "eklendi" and (s.ilk_veri_alinan, s.ilk_veri_bekleyen) == (2, 1)
    assert takipte(conn, G1) == {"1", "2", "3"} and _satir(conn, G1, "2")["baseline_tamam"] == 0
    src.hatalar.clear()
    run(servis.oyuncular_tara(G1, src))
    assert _satir(conn, G1, "2")["bolge"] == "Ankara" and bildirimler(conn) == []


@pytest.mark.parametrize("hata", [IstekSiniri("429", 30), TokenGecersiz("401")])
def test_istek_siniri_veya_token_hatasinda_kalan_istekler_atilmaz_komut_bloke_olmaz(conn, servis, src, hata, monkeypatch):
    monkeypatch.setattr(config, "PARTI_EKLE_ESZAMANLI", 1)
    for i in range(1, 31):
        src.oyuncu(str(i), f"A{i}", "Ankara", "TR")
    src.hatalar[("oyuncu", "3")] = hata
    src.parti("1244", "P", [(str(i), f"A{i}") for i in range(1, 31)])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert s.durum == "eklendi" and s.yeni_eklenen == 30
    assert sum(1 for c in src.cagrilar if c[0] == "oyuncu") <= 4, "hız sınırı/token hatasından sonra istek yağmuru yok"
    assert s.ilk_veri_bekleyen >= 26 and s.ilk_veri_notu


def test_yavas_kaynakta_sure_butcesi_komutu_bloke_etmez(conn, servis, src, monkeypatch):
    monkeypatch.setattr(config, "PARTI_EKLE_ILK_VERI_SURE_SN", 0.05)
    for i in range(1, 11):
        src.oyuncu(str(i), f"A{i}", "Ankara", "TR")
    src.kapilar[("oyuncu", "1")] = asyncio.Event()  # hiç açılmaz
    src.parti("1244", "P", [(str(i), f"A{i}") for i in range(1, 11)])
    s = run(servis.parti_ekle(G1, "1244", src))
    assert s.durum == "eklendi" and s.ilk_veri_bekleyen >= 1 and takipte(conn, G1) == {str(i) for i in range(1, 11)}


def test_ayni_uid_baska_numarayla_zaten_takipteyse_mukerrer_olusmaz(conn, servis, src):
    from dipbot.kaynak import OyuncuAnlik
    src.oyuncular["10"] = OyuncuAnlik("10", "Ali", "Ankara", "TR", "uuid-1")
    run(servis.oyuncu_ekle(G1, "10", src))
    src.oyuncular["11"] = OyuncuAnlik("11", "Ali", "Ankara", "TR", "uuid-1")  # aynı kişi, başka numara
    src.parti("1244", "P", [("11", "Ali")])
    run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == {"10"}


# ----------------------------------------------------------------- madde 7: tek başlangıç özeti
@pytest.fixture
def ui(conn, src, saat):
    return Arayuz(conn, src, KURUCU, saat=saat)


def test_100_oyunculu_ilk_parti_eklemesi_tek_ozet_yuzlerce_bildirim_yok(conn, ui, src):
    for i in range(1, 101):
        src.oyuncu(str(i), f"A{i}", "Ankara" if i % 2 else None, "TR")
    src.parti("1244", "P", [(str(i), f"A{i}") for i in range(1, 101)])
    y = run(ui.komut(grup_komut("partiekle", "1244")))
    assert y.metin.count("Party added to tracking") == 1 and "Total members: 100" in y.metin
    assert "initial summary" in y.metin.lower() and "no notifications are sent" in y.metin
    assert "100" in y.metin and "region" in y.metin.lower()
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    # ilk tarama da (kalan/eksik bilgiler dahil) bildirim üretmez
    sv = TakipServisi(conn, ui.saat)
    run(sv.partiler_tara(G1, src)); run(sv.oyuncular_tara(G1, src))
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_baslangic_ozeti_sonraki_gercek_degisiklik_bildirimlerinden_ayri(conn, ui, src):
    for i in "12":
        src.oyuncu(i, "A" + i, "Ankara", "TR")
    src.parti("1244", "P", [("1", "A1"), ("2", "A2")])
    run(ui.komut(grup_komut("partiekle", "1244")))
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    sv = TakipServisi(conn, ui.saat)
    # gerçek değişiklikler: yeni üye katıldı + biri bölge değiştirdi
    src.oyuncu("3", "A3", "Izmir", "TR")
    src.parti("1244", "P", [("1", "A1"), ("2", "A2"), ("3", "A3")])
    src.oyuncu("1", "A1", "İstanbul", "TR")
    run(sv.partiler_tara(G1, src)); run(sv.oyuncular_tara(G1, src))
    turler = sorted(b["tur"] for b in bildirimler(conn))
    assert turler == ["oyuncu_degisti", "parti_uyelik"]
    uyelik = bildirimler(conn, "parti_uyelik")[0]["veri"]
    assert [k["oyuncu_id"] for k in uyelik["katilanlar"]] == ["3"] and uyelik["ayrilanlar"] == []


# ----------------------------------------------------------------- şema değişmedi (güvenli güncelleme garantisi)
def test_veritabani_semasi_parmak_izi_degisiklik_bilincli_olmali():
    """Şema parmak izi: şema BİLEREK değiştirilirse bu değer, migration ve deploy/GUNCELLEME-PLANI.md birlikte
    güncellenmeli. v5 (büyük parti ilk 100): takip_partiler'e kismi/toplam_uye/xp_sirali; v6 (XP süzgeci): parti_uyeleri.xp,
    takip_oyuncular.xp_alti_sayac/xp_onayli; v7 (çok hesaplı tarama): YENİ tablolar hesaplar + oyuncu_atamalari (mevcut tablolara dokunmaz) — hepsi EKLEME, mevcut kayıtlar korunur (tests/test_parti_ilk100.py,
    tests/test_xp_filtresi.py migration testleri)."""
    import hashlib
    assert db.SEMA_SURUMU == 7
    assert hashlib.sha256(db._sema().encode()).hexdigest() == "1bed69e3061f0c1996982de98b6dcdae48c8c1abe9b8c543bbdfe11b8d1a2f9e"


def test_mevcut_veri_yeni_surumle_acilinca_korunur(tmp_path):
    yol = tmp_path / "canli.db"
    saat = SahteSaat()
    c = db.baglan(yol)
    yonetim.yetki_ver(c, saat, KURUCU, KURUCU, 55)
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G1)
    src = SahteKaynak(); src.oyuncu("5", "Ali", "Ankara", "TR")
    run(TakipServisi(c, saat).oyuncu_ekle(G1, "5", src))
    c.close()
    c = db.baglan(yol)  # güncelleme sonrası ilk açılış (migration idempotent)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SEMA_SURUMU
    assert G1 in yonetim.durum_yukle(c, KURUCU).onayli_gruplar and 55 in yonetim.durum_yukle(c, KURUCU).yetkililer
    assert takipte(c, G1) == {"5"}
    c.close()
