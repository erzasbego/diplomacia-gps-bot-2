"""Büyük partilerde (API ilk 100 üyeyi döndürür) sınırlı takip: adaptör, çekirdek, bildirimler, göç (şema v5).
MOCK (sahte kaynak/httpx.MockTransport); gerçek Diplomacia davranışı DOĞRULANMADI."""
import copy
import json
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte
from tg_fakes import grup_dugme, grup_komut, onay_kimligi

from dipbot import config, db, yonetim
from dipbot.diplomacia import ayristir as a
from dipbot.kaynak import EksikVeri, PartiAnlik, PartiUyesi
from dipbot.takip import TakipServisi
from dipbot.tg.arayuz import Arayuz
from fakes import SahteKaynak, SahteSaat

FX = json.loads((Path(__file__).parent / "fixtures" / "diplomacia_ornek.json").read_text(encoding="utf-8"))


# ============================================================ yardımcılar
def _uuid(n):
    return str(uuid.UUID(int=10_000 + n))


def _govde(n, xp=lambda i: 1_000_000 - i * 10, ekstra=None, bas=1):
    """N üyeli /members gövdesi (XP azalan)."""
    taban = copy.deepcopy(FX["party_members_response"]["members"][0])
    uyeler = []
    for i in range(bas, bas + n):
        m = copy.deepcopy(taban)
        m.update(player_id=_uuid(i), player_number=i, username=f"U{i}", display_name=f"[P] U{i}")
        if xp is None:
            m.pop("xp", None)
        else:
            m["xp"] = xp(i)
        uyeler.append(m)
    g = {"members": uyeler, "role_limits": {}, "loyal": {}}
    g.update(ekstra or {})
    return g


def _kaynakta(n, **kw):
    """Sahte kaynakta 1244 numaralı partiyi n üyeyle kurar; tüm oyuncular tanımlı."""
    src = kw.pop("src", None) or SahteKaynak()
    toplam = kw.pop("toplam", None)
    bas = kw.pop("bas", 1)
    for i in range(bas, bas + n):
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    src.parti("1244", "TEKNO", [(str(i), f"U{i}") for i in range(bas, bas + n)], toplam=toplam)
    return src


def _parti_satiri(conn, g=G1, pid="1244"):
    return conn.execute("SELECT * FROM takip_partiler WHERE group_id=? AND parti_id=?", (g, pid)).fetchone()


def _uye_say(conn, g=G1, pid="1244"):
    return conn.execute("SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (g, pid)).fetchone()[0]


# ============================================================ A) adaptör: sınırlı liste ayrıştırma
@pytest.mark.parametrize("toplam", [329, 129, 101])
def test_100_uye_toplam_100den_fazla_gecerli_sinirli_liste(toplam):
    u = a.uyeler_ayristir(_govde(100), toplam)
    assert u.sinirli is True and u.tamam is False and u.neden == ""
    assert len(u.uyeler) == 100 and u.xp_sirali is True
    p = a.parti_birlestir(a.PartiBilgi("1244", _uuid(0), 1244, "T", toplam), u)
    assert p.sinirli and p.toplam_uye == toplam and not p.uyeler_tamam and p.xp_sirali is True


@pytest.mark.parametrize("n", [75, 100])
def test_75_ve_100_uyeli_parti_tam_liste(n):
    u = a.uyeler_ayristir(_govde(n), n)
    assert u.tamam is True and u.sinirli is False and len(u.uyeler) == n


@pytest.mark.parametrize("n,toplam,neden", [
    (99, 329, "uyuşmuyor"), (50, 329, "uyuşmuyor"), (0, 329, "uyuşmuyor"), (101, 329, "uyuşmuyor"),
    (100, 99, "uyuşmuyor"), (75, 100, "uyuşmuyor"),
])
def test_gercekten_eksik_veya_tutarsiz_liste_reddedilir(n, toplam, neden):
    u = a.uyeler_ayristir(_govde(n), toplam)
    assert u.tamam is False and u.sinirli is False and neden in u.neden


def test_mukerrer_uyeli_100_liste_sinirli_sayilmaz():
    g = _govde(100)
    g["members"][50]["player_number"] = g["members"][49]["player_number"]
    u = a.uyeler_ayristir(g, 329)
    assert u.sinirli is False and u.tamam is False and "mükerrer" in u.neden
    g = _govde(100)
    g["members"][60]["player_id"] = g["members"][5]["player_id"]
    assert a.uyeler_ayristir(g, 329).sinirli is False


@pytest.mark.parametrize("anahtar", ["next", "page", "total", "has_more", "cursor", "limit"])
def test_sayfalama_anahtari_varsa_sinirli_sayilmaz(anahtar):
    u = a.uyeler_ayristir(_govde(100, ekstra={anahtar: 2}), 329)
    assert u.sinirli is False and u.tamam is False and "sayfalama" in u.neden


def test_bozuk_uye_kaydi_yine_eksikveri():
    g = _govde(100)
    del g["members"][10]["username"]
    with pytest.raises(EksikVeri):
        a.uyeler_ayristir(g, 329)


def test_api_100den_fazla_tam_liste_dondururse_ilk_100_alinir():
    u = a.uyeler_ayristir(_govde(120), 120)
    assert u.sinirli is True and len(u.uyeler) == 100
    assert [x.oyuncu_id for x in u.uyeler] == [str(i) for i in range(1, 101)], "İLK 100 (sıra korunur)"


def test_xp_sirasi_dogrulama_varsayim_yapmaz():
    assert a.uyeler_ayristir(_govde(100, xp=lambda i: i), 329).xp_sirali is False  # artan: XP-azalan DEĞİL
    assert a.uyeler_ayristir(_govde(100, xp=None), 329).xp_sirali is None  # xp alanı yok: bilinmiyor
    g = _govde(100); g["members"][3]["xp"] = "x"
    assert a.uyeler_ayristir(g, 329).xp_sirali is None
    # sıralama doğrulanamasa da liste "API'nin döndürdüğü ilk 100" olarak kabul edilir
    assert a.uyeler_ayristir(_govde(100, xp=lambda i: i), 329).sinirli is True
    assert a.uyeler_ayristir(_govde(100, xp=lambda i: 5), 329).xp_sirali is True  # eşit XP azalmayan sıradır


def _istemci(tmp_path, n_uye, member_count):
    from dipbot.diplomacia.istemci import IstemciKaynagi, OrtamKimlik
    from dipbot.tg.token import TokenDeposu
    depo = TokenDeposu(tmp_path / "t")
    depo.yaz("aaa.bbb.ccc")  # uydurma
    parti = copy.deepcopy(FX["party_response"])
    parti["party"].update(member_count=member_count, party_number=1244)
    istekler = []

    def isle(req: httpx.Request):
        istekler.append(req)
        if req.url.path == "/api/parties/1244":
            return httpx.Response(200, json=parti)
        if req.url.path == "/api/parties/1244/members":
            return httpx.Response(200, json=_govde(n_uye))
        return httpx.Response(404)

    k = IstemciKaynagi(OrtamKimlik(depo, {}), transport=httpx.MockTransport(isle))
    return k, istekler


@pytest.mark.parametrize("toplam", [329, 129])
def test_istemci_ek_sayfa_istegi_atmaz_ve_sinirli_liste_doner(tmp_path, toplam):
    k, istekler = _istemci(tmp_path, 100, toplam)
    p = run(k.fetch_party("1244"))
    assert p.sinirli and len(p.uyeler) == 100 and p.toplam_uye == toplam
    assert [r.url.path for r in istekler] == ["/api/parties/1244", "/api/parties/1244/members"]
    assert all(not r.url.query for r in istekler), "sayfa/offset parametresi gönderilmez"


# ============================================================ B) çekirdek: parti ekleme
@pytest.mark.parametrize("n,toplam,beklenen_sinirli", [(75, None, False), (100, None, False), (100, 129, True), (100, 329, True)])
def test_parti_ekleme_75_100_129_329(conn, servis, src, n, toplam, beklenen_sinirli):
    _kaynakta(n, src=src, toplam=toplam)
    s = run(servis.parti_ekle(G1, "1244", src))
    assert s.durum == "eklendi" and s.yeni_eklenen == n and len(takipte(conn, G1)) == n
    assert s.sinirli is beklenen_sinirli and s.toplam_uye == (toplam or n)
    assert _uye_say(conn) == n
    r = _parti_satiri(conn)
    assert bool(r["sinirli"]) is beklenen_sinirli and r["toplam_uye"] == (toplam or n)
    assert bildirimler(conn) == [], "ilk eklemede hiçbir bildirim yok"
    # ilk veri sınırlı eşzamanlılıkla alındı; ek sayfa/üye isteği yok
    assert sum(1 for c in src.cagrilar if c[0] == "parti") == 1
    assert sum(1 for c in src.cagrilar if c[0] == "oyuncu") <= 100


def test_329_uyeli_parti_ilk_100_alinir_diger_229_cekilmez(conn, servis, src):
    _kaynakta(100, src=src, toplam=329)
    for i in range(101, 330):
        src.oyuncu(str(i), f"U{i}", "Izmir", "TR")  # API'de var ama listede yok
    run(servis.parti_ekle(G1, "1244", src))
    assert takipte(conn, G1) == {str(i) for i in range(1, 101)}
    assert {c[1] for c in src.cagrilar if c[0] == "oyuncu"} <= {str(i) for i in range(1, 101)}


def test_eksik_veya_bozuk_yanit_parti_eklemeyi_reddeder_hicbir_sey_yazilmaz(conn, servis, src):
    # 99 üye ama toplam 329: sınırlı liste DEĞİL (eksik) -> reddedilir
    src.partiler["1244"] = PartiAnlik("1244", "T", tuple(PartiUyesi(str(i), f"U{i}") for i in range(1, 100)), False,
                                       sinirli=False, toplam_uye=329)
    with pytest.raises(EksikVeri):
        run(servis.parti_ekle(G1, "1244", src))
    assert _parti_satiri(conn) is None and takipte(conn, G1) == set()
    # çekirdek de savunmalı: sinirli=True ama toplam bilgisi tutarsız
    src.partiler["1244"] = PartiAnlik("1244", "T", tuple(PartiUyesi(str(i), f"U{i}") for i in range(1, 101)), False,
                                       sinirli=True, toplam_uye=100)
    with pytest.raises(EksikVeri):
        run(servis.parti_ekle(G1, "1244", src))
    # yinelenen kimlik
    uy = [PartiUyesi(str(i), f"U{i}") for i in range(1, 100)] + [PartiUyesi("5", "dup")]
    src.partiler["1244"] = PartiAnlik("1244", "T", tuple(uy), False, sinirli=True, toplam_uye=329)
    with pytest.raises(EksikVeri):
        run(servis.parti_ekle(G1, "1244", src))
    assert _parti_satiri(conn) is None


# ============================================================ C) taramada: sıralama değişimi != üyelik değişimi
def _kur329(conn, servis, src):
    _kaynakta(100, src=src, toplam=329)
    run(servis.parti_ekle(G1, "1244", src))


def _sirala(src, uyeler_ids, toplam=329):
    for i in uyeler_ids:
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR") if str(i) not in src.oyuncular else None
    src.parti("1244", "TEKNO", [(str(i), f"U{i}") for i in uyeler_ids], toplam=toplam)


def test_ilk_100den_cikan_ayrildi_bildirimi_uretmez_yeni_giren_sessiz_takibe_alinir(conn, servis, src):
    _kur329(conn, servis, src)
    _sirala(src, list(range(3, 103)))  # U1,U2 düştü; U101,U102 ilk 100'e girdi (XP sırası değişti)
    r = run(servis.partiler_tara(G1, src))
    assert r.ok and r.degisiklik == 0
    assert bildirimler(conn, "parti_uyelik") == [], "'ayrıldı'/'katıldı' bildirimi YOK"
    assert "101" in takipte(conn, G1) and "102" in takipte(conn, G1), "yeni girenler takibe alındı"
    assert _satir(conn, "101")["baseline_tamam"] == 0
    run(servis.oyuncular_tara(G1, src))
    assert bildirimler(conn) == [], "başlangıç verisi sessizce kaydedildi"
    assert _satir(conn, "101")["bolge"] == "Ankara" and _satir(conn, "101")["baseline_tamam"] == 1
    # listede düşenler (otomatik eklenmişlerdi) takipten çıkar; set ~100 kalır
    assert "1" not in takipte(conn, G1) and "2" not in takipte(conn, G1)
    assert len(takipte(conn, G1)) == 100 and _uye_say(conn) == 100


def _satir(conn, oid, g=G1):
    return conn.execute("SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (g, oid)).fetchone()


def test_xp_siralamasi_tamamen_degisse_de_gercek_uyelik_degisikligi_gosterilmez(conn, servis, src):
    _kur329(conn, servis, src)
    ters = list(range(100, 0, -1))  # sıra tersine döndü, küme aynı
    _sirala(src, ters)
    run(servis.partiler_tara(G1, src))
    assert bildirimler(conn) == [] and len(takipte(conn, G1)) == 100


def test_manuel_eklenen_oyuncu_ilk_100den_cikinca_korunur(conn, servis, src):
    _kur329(conn, servis, src)
    # U1 daha önce otomatik takipteydi; kullanıcı onu elle de ekledi (zaten_takipte -> manuel niyet kaydı)
    assert run(servis.oyuncu_ekle(G1, "1", src)).durum == "zaten_takipte"
    assert _satir(conn, "1")["elle_eklendi"] == 1
    # U500 listede hiç yoktu: tamamen manuel eklenir
    src.oyuncu("500", "U500", "Bursa", "TR")
    run(servis.oyuncu_ekle(G1, "500", src))
    _sirala(src, list(range(3, 103)))  # U1,U2 düştü
    run(servis.partiler_tara(G1, src))
    assert "1" in takipte(conn, G1) and "500" in takipte(conn, G1), "manuel eklenenler korunur"
    assert "2" not in takipte(conn, G1), "yalnız otomatik eklenen düşer"
    assert bildirimler(conn) == []


def test_ilk_100_disindaki_uyelerin_ayrilma_katilma_bildirimi_uretilmez(conn, servis, src):
    _kur329(conn, servis, src)
    # toplam üye sayısı 329 -> 330 (biri katıldı) ama ilk 100 aynı: bildirim yok
    src.parti("1244", "TEKNO", [(str(i), f"U{i}") for i in range(1, 101)], toplam=330)
    run(servis.partiler_tara(G1, src))
    src.parti("1244", "TEKNO", [(str(i), f"U{i}") for i in range(1, 101)], toplam=300)
    run(servis.partiler_tara(G1, src))
    assert bildirimler(conn) == []
    assert _parti_satiri(conn)["toplam_uye"] == 300


def test_engelli_oyuncu_ilk_100e_girince_geri_eklenmez(conn, servis, src):
    _kur329(conn, servis, src)
    servis.oyuncu_sil(G1, "100")  # elle silindi -> engel
    assert "100" not in takipte(conn, G1)
    _sirala(src, list(range(1, 100)) + [101])  # 100 düştü
    run(servis.partiler_tara(G1, src))
    _sirala(src, list(range(1, 101)))  # 100 yeniden ilk 100'e girdi (doğrulanmamış katılım)
    run(servis.partiler_tara(G1, src))
    assert "100" not in takipte(conn, G1), "sıralama dönüşü engeli kaldırmaz/sessiz yeniden ekleme yok"
    assert bildirimler(conn) == []


def test_ani_toplu_degisimde_yalniz_otomatik_ve_listede_olmayanlar_cikar_manuel_korunur(conn, servis, src):
    _kur329(conn, servis, src)
    src.oyuncu("50", "U50", "Ankara", "TR")
    run(servis.oyuncu_ekle(G1, "50", src))  # otomatik kaydı manuel niyete çevirir
    _sirala(src, list(range(201, 301)))  # 100 üyenin tamamı değişti
    run(servis.partiler_tara(G1, src))
    beklenen = {str(i) for i in range(201, 301)} | {"50"}
    assert takipte(conn, G1) == beklenen and bildirimler(conn, "parti_uyelik") == []
    _sirala(src, list(range(1, 101)))  # eski liste geri döndü
    run(servis.partiler_tara(G1, src))
    assert takipte(conn, G1) == {str(i) for i in range(1, 101)} and bildirimler(conn) == []


def test_ayni_oyuncu_baska_takipli_partinin_listesindeyse_cikarilmaz(conn, servis, src):
    _kur329(conn, servis, src)
    src.parti("77", "KUCUK", [("1", "U1"), ("900", "U900")])
    src.oyuncu("900", "U900", "X", "TR")
    run(servis.parti_ekle(G1, "77", src))
    _sirala(src, list(range(3, 103)))  # U1 büyük partinin ilk 100'ünden düştü ama KUCUK partide
    run(servis.partiler_tara(G1, src))
    assert "1" in takipte(conn, G1) and "2" not in takipte(conn, G1)


# ============================================================ D) gerçek üyelik değişikliği (≤100 üyeli parti) hâlâ bildirilir
def test_75_uyeli_partide_gercek_katilim_ve_ayrilma_bildirilir(conn, servis, src):
    _kaynakta(75, src=src)
    run(servis.parti_ekle(G1, "1244", src))
    src.oyuncu("900", "U900", "Ankara", "TR")
    src.parti("1244", "TEKNO", [(str(i), f"U{i}") for i in range(2, 76)] + [("900", "U900")])  # U1 ayrıldı, U900 katıldı
    r = run(servis.partiler_tara(G1, src))
    assert r.degisiklik == 1
    v = bildirimler(conn, "parti_uyelik")[0]["veri"]
    assert [x["oyuncu_id"] for x in v["katilanlar"]] == ["900"] and [x["oyuncu_id"] for x in v["ayrilanlar"]] == ["1"]
    assert "900" in takipte(conn, G1) and "1" in takipte(conn, G1), "ayrılan takipte kalır (spec)"


def test_gecis_sinirli_dan_tam_listeye_ayrilanlar_dogrulanmis_katilanlar_sessiz(conn, servis, src):
    _kaynakta(100, src=src, toplam=129)
    run(servis.parti_ekle(G1, "1244", src))
    # parti küçüldü: artık 95 üye ve TAM liste (U96..U100 listede yok, U120 yeni görünüyor)
    src.oyuncu("120", "U120", "Ankara", "TR")
    src.parti("1244", "TEKNO", [(str(i), f"U{i}") for i in range(1, 96)] + [("120", "U120")])
    run(servis.partiler_tara(G1, src))
    v = bildirimler(conn, "parti_uyelik")
    assert len(v) == 1
    assert sorted(x["oyuncu_id"] for x in v[0]["veri"]["ayrilanlar"]) == ["100", "96", "97", "98", "99"]
    assert v[0]["veri"]["katilanlar"] == [], "önceki liste kısmiydi: U120'nin yeni katılım olduğu doğrulanamaz"
    assert "120" in takipte(conn, G1)
    assert not _parti_satiri(conn)["sinirli"]


def test_gecis_tamdan_sinirliya_bildirim_yok(conn, servis, src):
    _kaynakta(95, src=src)
    run(servis.parti_ekle(G1, "1244", src))
    _kaynakta(100, src=src, toplam=140)  # parti büyüdü: artık yalnız ilk 100
    run(servis.partiler_tara(G1, src))
    assert bildirimler(conn) == [] and bool(_parti_satiri(conn)["sinirli"])


# ============================================================ E) başlangıç özeti ve ekranlar
@pytest.fixture
def ui(conn, src, saat):
    return Arayuz(conn, src, KURUCU, saat=saat)


def test_baslangic_ozeti_329_uyeli_parti(conn, ui, src):
    _kaynakta(100, src=src, toplam=329)
    y = run(ui.komut(grup_komut("partiekle", "1244")))
    m = y.metin
    assert m.count("Parti takibe eklendi") == 1 and "TEKNO" in m
    assert "Toplam üye: 329" in m and "Takip edilen oyuncu: 100" in m
    assert "ilk 100" in m.lower() and "XP" in m
    assert "İlk veri alınan üye: 100" in m
    assert "katıl" not in m.lower().replace("katılım bildirimi", "") or "bildirim gönderilmez" in m
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_baslangic_ozeti_bekleyen_sayisi_ve_75_uyeli_parti(conn, ui, src, monkeypatch):
    monkeypatch.setattr(config, "PARTI_EKLE_ILK_VERI_UST", 30)
    _kaynakta(75, src=src)
    m = run(ui.komut(grup_komut("partiekle", "1244"))).metin
    assert "Toplam üye: 75" in m and "Takip edilen oyuncu: 75" in m
    assert "İlk veri alınan üye: 30" in m and "ilk taramada tamamlanacak: 45" in m
    assert "ilk 100" not in m.lower(), "75 üyeli partide sınır notu çıkmaz"


@pytest.mark.parametrize("xp,parca", [(None, "XP sıralaması doğrulanamadı"), (False, "azalan sırada DEĞİL"),
                                       (True, "azalan sırada doğrulandı")])
def test_ozet_xp_sirasi_durumunu_dogru_belirtir_varsayim_yapmaz(conn, ui, src, xp, parca):
    _kaynakta(100, src=src, toplam=329)
    src.partiler["1244"] = PartiAnlik("1244", "TEKNO", src.partiler["1244"].uyeler, False,
                                       sinirli=True, toplam_uye=329, xp_sirali=xp)
    m = run(ui.komut(grup_komut("partiekle", "1244"))).metin
    assert parca in m and "API'nin döndürdüğü ilk 100" in m


def test_parti_listesi_ve_detayinda_sinirli_gosterim(conn, ui, src):
    _kaynakta(100, src=src, toplam=329)
    run(ui.komut(grup_komut("partiekle", "1244")))
    assert "100/329 üye" in run(ui.komut(grup_komut("partiler"))).metin
    d = run(ui.buton(grup_dugme("pd:1244"))).metin
    assert "Toplam üye: 329" in d and "ilk 100" in d.lower()


def test_sinirli_partiyi_kaldirma_onizlemesi_uyari_verir_yalniz_dogrulananlari_siler(conn, ui, servis, src):
    _kaynakta(100, src=src, toplam=329)
    run(ui.komut(grup_komut("partiekle", "1244")))
    src.oyuncu("500", "U500", "Bursa", "TR")
    run(servis.oyuncu_ekle(G1, "500", src))  # listede olmayan elle eklenmiş üye (belki partidedir, doğrulanamaz)
    y = run(ui.komut(grup_komut("partisil", "1244")))
    assert "329" in y.metin and "ilk 100" in y.metin.lower() and "çıkarılmaz" in y.metin
    run(ui.buton(grup_dugme(f"c:e:{onay_kimligi(y)}")))
    assert takipte(conn, G1) == {"500"}, "yalnız doğrulanan ilk 100 çıkar; doğrulanamayan manuel kayıt korunur"


def test_kaldirma_eksik_liste_ise_hala_silinmez(conn, ui, servis, src):
    _kaynakta(100, src=src, toplam=329)
    run(ui.komut(grup_komut("partiekle", "1244")))
    src.partiler["1244"] = PartiAnlik("1244", "T", tuple(PartiUyesi(str(i)) for i in range(1, 51)), False,
                                       sinirli=False, toplam_uye=329)
    y = run(ui.komut(grup_komut("partisil", "1244")))
    assert "kaldırılamadı" in y.metin and len(takipte(conn, G1)) == 100


# ============================================================ F) şema v5: mevcut kayıtlar korunur
@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="DROP COLUMN için SQLite >= 3.35")
def test_v4_veritabani_v5e_gecerken_kayitlar_korunur(tmp_path):
    yol = tmp_path / "canli.db"
    saat = SahteSaat()
    c = db.baglan(yol)
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G1)
    src = _kaynakta(3)
    run(TakipServisi(c, saat).parti_ekle(G1, "1244", src))
    c.execute("ALTER TABLE takip_partiler DROP COLUMN sinirli")
    c.execute("ALTER TABLE takip_partiler DROP COLUMN toplam_uye")
    c.execute("ALTER TABLE takip_partiler DROP COLUMN xp_sirali")
    c.execute("PRAGMA user_version=4")
    c.close()
    c = db.baglan(yol)  # güncelleme sonrası ilk açılış
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SEMA_SURUMU >= 5
    r = _parti_satiri(c)
    assert r["ad"] == "TEKNO" and r["sinirli"] == 0 and r["toplam_uye"] is None and r["xp_sirali"] is None
    assert takipte(c, G1) == {"1", "2", "3"} and _uye_say(c) == 3
    c.close()
    c = db.baglan(yol)  # ikinci açılış idempotent
    assert _parti_satiri(c)["ad"] == "TEKNO"
    c.close()


# ============================================================ G) kalıcılık, göç ve eski kod uyumu
def test_sinirli_durum_yeniden_baslatmada_korunur_ve_tarama_sessiz_devam_eder(tmp_path):
    yol = tmp_path / "t.db"
    saat = SahteSaat()
    c = db.baglan(yol)
    yonetim.grup_onayla(c, saat, KURUCU, KURUCU, G1)
    src = _kaynakta(100, toplam=329)
    run(TakipServisi(c, saat).parti_ekle(G1, "1244", src))
    c.close()
    c = db.baglan(yol)  # yeniden başlatma
    r = _parti_satiri(c)
    assert (r["sinirli"], r["toplam_uye"]) == (1, 329) and _uye_say(c) == 100
    run(TakipServisi(c, saat).partiler_tara(G1, src))
    assert bildirimler(c) == []
    c.close()


def test_grup_tasi_sinirli_parti_alanlarini_tasir(conn, servis, src, saat):
    from dipbot.tg.arayuz import Arayuz as _A
    _kaynakta(100, src=src, toplam=329)
    run(servis.parti_ekle(G1, "1244", src))
    yeni = -1009001
    _A(conn, src, KURUCU, saat=saat).grup_goc(G1, yeni)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, yeni)
    yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, yeni)
    r = _parti_satiri(conn, yeni)
    assert (r["sinirli"], r["toplam_uye"]) == (1, 329) and _uye_say(conn, yeni) == 100


def test_eski_kod_ek_sutunlari_bilmese_de_parti_ekleyebilir_geri_donus_uyumu(conn):
    """Geri dönüş güvenliği: v4 kodu `takip_partiler`e yeni sütunları vermeden INSERT eder; varsayılanlar yeter."""
    conn.execute(
        "INSERT INTO takip_partiler(group_id, parti_id, ad, baseline_tamam, son_dogrulama, eklendi)"
        " VALUES(?,?,?,1,'t','t')", (G1, "9", "ESKI"),
    )
    r = _parti_satiri(conn, pid="9")
    assert (r["sinirli"], r["toplam_uye"], r["xp_sirali"]) == (0, None, None)
