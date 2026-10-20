"""Kural 6-10 + çatışma/yeniden onay (MOCK kaynakla; canlı doğrulama DEĞİLDİR)."""
from conftest import G1, G2, bildirimler, run, takipte

from dipbot.kaynak import EksikVeri, SiteErisilemez


def _kur(src, uyeler=("a", "b", "c")):
    for u in uyeler:
        src.oyuncu(u, u.upper())
    src.parti("5830", "TEKNO", [(u, u.upper()) for u in uyeler])


def test_k6_ilk_parti_kaydi_giris_bildirimi_uretmez(conn, servis, src):
    _kur(src)
    s = run(servis.parti_ekle(G1, "5830", src))
    assert (s.durum, s.uye_sayisi, s.yeni_eklenen, s.zaten_takipte) == ("eklendi", 3, 3, 0)
    assert takipte(conn, G1) == {"a", "b", "c"}
    assert bildirimler(conn) == []
    r = run(servis.partiler_tara(G1, src))
    assert r.ok and bildirimler(conn) == []
    # tekrar ekleme engellenir
    assert run(servis.parti_ekle(G1, "5830", src)).durum == "zaten_takipte"


def test_k7_yeni_uye_otomatik_takibe_alinir(conn, servis, src):
    _kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    src.oyuncu("d", "D")
    src.parti("5830", "TEKNO", [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])
    run(servis.partiler_tara(G1, src))
    b = bildirimler(conn, "parti_uyelik")
    assert len(b) == 1 and [x["oyuncu_id"] for x in b[0]["veri"]["katilanlar"]] == ["d"]
    assert "d" in takipte(conn, G1)
    # d'nin ilk gözlemi bildirimsiz baseline olur
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn, "oyuncu_degisti") == []
    # tekrar taramada tekrar bildirim yok
    run(servis.partiler_tara(G1, src))
    assert len(bildirimler(conn, "parti_uyelik")) == 1


def test_parti_ad_degisikligi_bildirilir(conn, servis, src):
    _kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    src.parti("5830", "YENI AD", [(u, u.upper()) for u in "abc"])
    run(servis.partiler_tara(G1, src))
    b = bildirimler(conn, "parti_ad_degisti")
    assert len(b) == 1 and (b[0]["veri"]["eski_ad"], b[0]["veri"]["yeni_ad"]) == ("TEKNO", "YENI AD")


def test_k8_ayrilan_oyuncu_takipte_kalir(conn, servis, src):
    _kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    src.parti("5830", "TEKNO", [("a", "A"), ("b", "B")])
    run(servis.partiler_tara(G1, src))
    b = bildirimler(conn, "parti_uyelik")
    assert [x["oyuncu_id"] for x in b[0]["veri"]["ayrilanlar"]] == ["c"]
    assert "c" in takipte(conn, G1)


def test_eksik_veya_sayfalamasi_bitmemis_liste_cikis_sayilmaz(conn, servis, src):
    _kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    src.parti("5830", "TEKNO", [("a", "A")], tamam=False)  # sayfalama bitmedi
    r = run(servis.partiler_tara(G1, src))
    assert not r.ok
    src.parti("5830", "TEKNO", [], tamam=True)  # boş liste şüpheli
    assert not run(servis.partiler_tara(G1, src)).ok
    assert bildirimler(conn) == []
    n = conn.execute("SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=?", (G1,)).fetchone()[0]
    assert n == 3 and takipte(conn, G1) == {"a", "b", "c"}
    # tamamlanmamış liste ile parti EKLENEMEZ
    src.parti("77", "Y", [("x", "X")], tamam=False)
    try:
        run(servis.parti_ekle(G1, "77", src))
        assert False
    except EksikVeri:
        pass


def test_k9_parti_kaldirilinca_yalniz_anki_uyeler_cikar(conn, servis, src):
    _kur(src)
    src.oyuncu("x", "X")
    run(servis.oyuncu_ekle(G1, "x", src))  # partiyle ilgisiz, elle
    run(servis.oyuncu_ekle(G1, "a", src))  # elle de eklenmiş
    run(servis.parti_ekle(G1, "5830", src))
    # A, B, C -> C ayrılır, D katılır
    src.oyuncu("d", "D")
    src.parti("5830", "TEKNO", [("a", "A"), ("b", "B"), ("d", "D")])
    run(servis.partiler_tara(G1, src))
    o = run(servis.parti_kaldirma_hazirla(G1, "5830", src))
    assert o.basarili and set(o.etkilenen) == {"a", "b", "d"}
    s = run(servis.parti_kaldirma_onayla(G1, "5830", src, o.ozet_anahtari))
    assert s.durum == "silindi" and set(s.silinen) == {"a", "b", "d"}
    assert takipte(conn, G1) == {"c", "x"}  # eski üye C ve ilgisiz X kalır
    assert conn.execute("SELECT COUNT(*) FROM takip_partiler").fetchone()[0] == 0


def test_k10_guncel_uyeler_dogrulanamazsa_silme_yapilmaz(conn, servis, src):
    _kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    for durum in ("hata", "eksik_sayfa", "bos"):
        if durum == "hata":
            src.hatalar[("parti", "5830")] = SiteErisilemez("503")
        else:
            src.hatalar.clear()
            src.parti("5830", "TEKNO", [] if durum == "bos" else [("a", "A")], tamam=(durum == "bos"))
        o = run(servis.parti_kaldirma_hazirla(G1, "5830", src))
        assert not o.basarili and o.neden
        s = run(servis.parti_kaldirma_onayla(G1, "5830", src, "herhangi"))
        assert s.durum == "dogrulanamadi" and s.neden
        assert takipte(conn, G1) == {"a", "b", "c"}
        assert conn.execute("SELECT COUNT(*) FROM takip_partiler").fetchone()[0] == 1
    # başka gruptaki parti kimliğiyle işlem yapılamaz
    s = run(servis.parti_kaldirma_onayla(G2, "5830", src, "x"))
    assert s.durum == "bulunamadi"


def test_onay_aninda_uyelik_degismisse_tekrar_onay_istenir(conn, servis, src):
    _kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    o1 = run(servis.parti_kaldirma_hazirla(G1, "5830", src))
    src.oyuncu("d", "D")
    src.parti("5830", "TEKNO", [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])  # onaydan önce değişti
    s = run(servis.parti_kaldirma_onayla(G1, "5830", src, o1.ozet_anahtari))
    assert s.durum == "tekrar_onay" and "d" not in s.onizleme.etkilenen  # d henüz takipte değil
    assert s.onizleme.uye_sayisi == 4
    assert takipte(conn, G1) == {"a", "b", "c"}  # hiçbir şey silinmedi
    s2 = run(servis.parti_kaldirma_onayla(G1, "5830", src, s.onizleme.ozet_anahtari))
    assert s2.durum == "silindi" and set(s2.silinen) == {"a", "b", "c"}


def test_baska_partideki_cikarilmis_oyuncu_sessizce_yeniden_eklenmez(conn, servis, src):
    for u in "abc":
        src.oyuncu(u, u.upper())
    src.parti("P1", "Bir", [("a", "A"), ("b", "B")])
    src.parti("P2", "Iki", [("b", "B"), ("c", "C")])
    run(servis.parti_ekle(G1, "P1", src))
    run(servis.parti_ekle(G1, "P2", src))
    o = run(servis.parti_kaldirma_hazirla(G1, "P1", src))
    run(servis.parti_kaldirma_onayla(G1, "P1", src, o.ozet_anahtari))
    assert takipte(conn, G1) == {"c"}
    assert conn.execute("SELECT oyuncu_id FROM engeller").fetchall()[0][0] == "b"
    for _ in range(3):  # rutin taramalar b'yi sessizce geri getirmez
        run(servis.partiler_tara(G1, src))
    assert takipte(conn, G1) == {"c"} and bildirimler(conn) == []
    # Kullanıcı elle yeniden eklerse engel kalkar
    run(servis.oyuncu_ekle(G1, "b", src))
    assert "b" in takipte(conn, G1) and conn.execute("SELECT COUNT(*) FROM engeller").fetchone()[0] == 0


def test_engelli_oyuncu_partiye_gercekten_yeniden_katilirsa_engel_kalkar(conn, servis, src):
    for u in "abc":
        src.oyuncu(u, u.upper())
    src.parti("P1", "Bir", [("a", "A"), ("b", "B")])
    src.parti("P2", "Iki", [("b", "B"), ("c", "C")])
    run(servis.parti_ekle(G1, "P1", src))
    run(servis.parti_ekle(G1, "P2", src))
    o = run(servis.parti_kaldirma_hazirla(G1, "P1", src))
    run(servis.parti_kaldirma_onayla(G1, "P1", src, o.ozet_anahtari))
    src.parti("P2", "Iki", [("c", "C")])  # b ayrılır
    run(servis.partiler_tara(G1, src))
    assert "b" not in takipte(conn, G1)
    src.parti("P2", "Iki", [("b", "B"), ("c", "C")])  # b gerçekten yeniden katılır
    run(servis.partiler_tara(G1, src))
    assert "b" in takipte(conn, G1)
    assert conn.execute("SELECT COUNT(*) FROM engeller").fetchone()[0] == 0


def test_elle_silinen_parti_uyesi_sessizce_geri_gelmez(conn, servis, src):
    _kur(src)
    run(servis.parti_ekle(G1, "5830", src))
    assert servis.oyuncu_sil(G1, "b")
    run(servis.partiler_tara(G1, src))
    assert "b" not in takipte(conn, G1)
