"""Kural 1-5 ve 20 (MOCK kaynakla; canlı doğrulama DEĞİLDİR)."""
from conftest import G1, G2, bildirimler, run, takipte

from dipbot.kaynak import EksikVeri, OyuncuAnlik, SiteErisilemez


def _ekle(servis, src, grup=G1, oid="72400", ad="Ali", bolge="Varna", ulke="Macaristan"):
    src.oyuncu(oid, ad, bolge, ulke)
    return run(servis.oyuncu_ekle(grup, oid, src))


def test_k1_ilk_ekleme_degisiklik_bildirimi_uretmez(conn, servis, src):
    s = _ekle(servis, src)
    assert s.durum == "eklendi" and s.ad == "Ali" and s.bolge == "Varna"
    assert bildirimler(conn) == []
    r = run(servis.oyuncular_tara(G1, src))  # değişiklik yokken de bildirim yok
    assert r.ok and bildirimler(conn) == []


def test_k2_isim_degisikligi_ayni_kimlik_uzerinden(conn, servis, src):
    _ekle(servis, src)
    src.oyuncu("72400", "Veli", "Varna", "Macaristan")
    run(servis.oyuncular_tara(G1, src))
    b = bildirimler(conn, "oyuncu_degisti")
    assert len(b) == 1
    assert b[0]["veri"]["oyuncu_id"] == "72400"
    assert b[0]["veri"]["ad"] == "Veli"
    assert b[0]["veri"]["degisimler"] == [{"alan": "ad", "eski": "Ali", "yeni": "Veli"}]
    assert conn.execute("SELECT ad FROM takip_oyuncular WHERE oyuncu_id='72400'").fetchone()[0] == "Veli"


def test_k3_bolge_ve_vatandaslik_bagimsiz(conn, servis, src):
    _ekle(servis, src)
    src.oyuncu("72400", "Ali", "Istanbul", "Macaristan")  # yalnız bölge
    run(servis.oyuncular_tara(G1, src))
    src.oyuncu("72400", "Ali", "Istanbul", "Bosna")  # yalnız vatandaşlık
    run(servis.oyuncular_tara(G1, src))
    b = bildirimler(conn, "oyuncu_degisti")
    assert [[d["alan"] for d in x["veri"]["degisimler"]] for x in b] == [["bolge"], ["vatandaslik_ulkesi"]]
    # Aynı taramada ikisi birden: TEK birleşik bildirim
    src.oyuncu("72400", "Ali", "Varna", "Macaristan")
    run(servis.oyuncular_tara(G1, src))
    son = bildirimler(conn, "oyuncu_degisti")[-1]
    assert sorted(d["alan"] for d in son["veri"]["degisimler"]) == ["bolge", "vatandaslik_ulkesi"]
    assert len(bildirimler(conn, "oyuncu_degisti")) == 3


def test_k4_ayni_degisiklik_tekrar_bildirilmez(conn, servis, src):
    _ekle(servis, src)
    src.oyuncu("72400", "Ali", "Istanbul", "Macaristan")
    for _ in range(4):
        run(servis.oyuncular_tara(G1, src))
    assert len(bildirimler(conn, "oyuncu_degisti")) == 1


def test_k5_eksik_cevap_onceki_veriyi_bozmaz(conn, servis, src):
    _ekle(servis, src)
    for hata in (EksikVeri("eksik"), SiteErisilemez("503")):
        src.hatalar[("oyuncu", "72400")] = hata
        r = run(servis.oyuncular_tara(G1, src))
        assert not r.ok  # başarısız tarama
    src.hatalar.clear()
    satir = conn.execute("SELECT ad, bolge, vatandaslik_ulkesi FROM takip_oyuncular").fetchone()
    assert tuple(satir) == ("Ali", "Varna", "Macaristan")
    assert bildirimler(conn) == []
    # Yanıt geldi ama alanlar boş (None): eski değer korunur, değişiklik sayılmaz
    src.oyuncular["72400"] = OyuncuAnlik("72400", "Ali", None, None)
    r = run(servis.oyuncular_tara(G1, src))
    assert r.ok
    satir = conn.execute("SELECT ad, bolge, vatandaslik_ulkesi FROM takip_oyuncular").fetchone()
    assert tuple(satir) == ("Ali", "Varna", "Macaristan")
    assert bildirimler(conn) == []
    # Eksik ilk ekleme yanıtı kaydedilmez
    src.oyuncular["9"] = OyuncuAnlik("9", None, "X", "Y")
    try:
        run(servis.oyuncu_ekle(G1, "9", src))
        assert False, "EksikVeri beklenirdi"
    except EksikVeri:
        pass
    assert "9" not in takipte(conn, G1)


def test_k20_ayni_oyuncu_farkli_yollarla_mukerrer_olmaz(conn, servis, src):
    _ekle(servis, src, oid="a", ad="A")
    # tekrar elle ekleme
    assert run(servis.oyuncu_ekle(G1, "a", src)).durum == "zaten_takipte"
    # parti yoluyla ekleme: "a" zaten takipte sayılır
    src.oyuncu("b", "B")
    src.parti("P", "Parti", [("a", "A"), ("b", "B")])
    s = run(servis.parti_ekle(G1, "P", src))
    assert (s.yeni_eklenen, s.zaten_takipte) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM takip_oyuncular WHERE oyuncu_id='a'").fetchone()[0] == 1
    # tek değişiklik -> tek bildirim
    src.oyuncu("a", "A", "Istanbul", "Ulke1")
    run(servis.partiler_tara(G1, src))
    run(servis.oyuncular_tara(G1, src))
    assert len(bildirimler(conn, "oyuncu_degisti")) == 1
    # sil: oyuncu_sil sonrası tekrar ekleme yeniden mümkün
    assert servis.oyuncu_sil(G1, "a") is True
    assert servis.oyuncu_sil(G1, "a") is False
