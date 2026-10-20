"""Tur F1: /grup_kaldir yalnız kurucu (F1-1). MOCK; canlı doğrulama DEĞİLDİR."""
import pytest
from conftest import bildirimler, run, takipte
from tg_fakes import (
    G1, G2, KURUCU, YABANCI, YETKILI,
    dugme, grup_dugme, grup_komut, komut, onay_kimligi, veriler,
)

from dipbot import yonetim
from dipbot.tg import arayuz as ar
from dipbot.tg.arayuz import Arayuz


@pytest.fixture
def ui(conn, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return Arayuz(conn, src, KURUCU, saat=saat)


def _onayli(conn):
    return yonetim.durum_yukle(conn, KURUCU).onayli_gruplar


def _yetkisiz_kayit(conn, kul):
    return conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE user_id=?", (kul,)).fetchone()[0]


# ------------------------------------------------------------ F1-1
def test_f1_yetkili_komutla_grup_kaldiramaz(ui, conn):
    y = run(ui.komut(grup_komut("grup_kaldir", str(G2), kul=YETKILI)))
    assert y is not None and not veriler(y), "onay düğmesi üretilmemeli"
    assert y.metin == ar.YALNIZ_KURUCU
    assert G2 in _onayli(conn)
    assert _yetkisiz_kayit(conn, YETKILI) == 1
    assert len(bildirimler(conn, "yetkisiz_deneme", hedef=KURUCU)) == 1


def test_f1_yetkili_dugmeyle_grup_kaldiramaz(ui, conn):
    y = run(ui.buton(grup_dugme(f"gk:{G2}", kul=YETKILI)))
    assert y.uyari == ar.YALNIZ_KURUCU and y.uyari_goster
    assert G2 in _onayli(conn)
    assert _yetkisiz_kayit(conn, YETKILI) == 1


def test_f1_yetkili_onay_islemi_elle_uretilse_de_servis_reddeder(ui, conn):
    kid = ui.islemler.olustur("grup_kaldir", G1, YETKILI, None, {"grup": G2})
    y = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))
    assert G2 in _onayli(conn)
    assert "kaldırıldı" not in (y.metin or "")


def test_f1_kurucu_komutla_ozelde_ve_grupta_kaldirir(ui, conn):
    kid = onay_kimligi(run(ui.komut(komut("grup_kaldir", str(G2)))))
    assert "kaldırıldı" in run(ui.buton(dugme(f"c:e:{kid}"))).metin
    assert G2 not in _onayli(conn)
    kid = onay_kimligi(run(ui.komut(grup_komut("grup_kaldir", str(G1)))))
    assert "kaldırıldı" in run(ui.buton(grup_dugme(f"c:e:{kid}"))).metin
    assert G1 not in _onayli(conn)


def test_f1_kurucu_dugmeyle_ozelde_kaldirir(ui, conn):
    kid = onay_kimligi(run(ui.buton(dugme(f"gk:{G2}"))))
    assert "kaldırıldı" in run(ui.buton(dugme(f"c:e:{kid}"))).metin
    assert G2 not in _onayli(conn)


def test_f1_servis_aktorsuz_veya_kurucu_olmayan_aktorle_reddedilir(conn, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    for aktor in (None, YETKILI, YABANCI):
        with pytest.raises(yonetim.YetkiHatasi):
            yonetim.grup_kaldir(conn, saat, KURUCU, aktor, G1)
    assert G1 in _onayli(conn)
    assert yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1) == 0
    assert G1 not in _onayli(conn)


def test_f1_sistem_kaynakli_dusurme_aktorsuz_calisir(conn, saat):
    yonetim.grup_kaldir_sistem(conn, saat, KURUCU, G1, neden="goc")
    assert G1 not in _onayli(conn)


def test_f1_goc_otomatik_dusurme_calismaya_devam_eder(ui, conn):
    assert ui.grup_goc(G1, -1009999) is True
    assert G1 not in _onayli(conn)


def test_f1_onay_islemi_sohbet_ve_kullaniciya_bagli(ui, conn):
    kid = onay_kimligi(run(ui.komut(komut("grup_kaldir", str(G2)))))
    # başka sohbetten (kurucu aynı kullanıcı ama grup sohbeti) ve başka kullanıcıdan kabul edilmez
    y = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert y.uyari and G2 in _onayli(conn)
    y = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))
    assert y.uyari and G2 in _onayli(conn)
    assert "kaldırıldı" in run(ui.buton(dugme(f"c:e:{kid}"))).metin


# ------------------------------------------------------------ F1-2 denetim bildirimi
import asyncio  # noqa: E402

from conftest import KURUCU as _K  # noqa: E402,F401
from dipbot import config, outbox  # noqa: E402
from dipbot.tg import bicim  # noqa: E402
from fakes import SahteGonderici  # noqa: E402


def _denetim(conn):
    return bildirimler(conn, "denetim")


@pytest.fixture
def yconn(conn, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return conn


def test_f2_yetkili_yetki_ver_kurucuya_denetim_bildirimi(yconn, saat):
    assert yonetim.yetki_ver(yconn, saat, KURUCU, YETKILI, 300) is True
    b = _denetim(yconn)
    assert len(b) == 1
    assert b[0]["hedef_sohbet"] == KURUCU and b[0]["grup_id"] is None
    v = b[0]["veri"]
    assert set(v) == {"eylem", "aktor_id", "hedef_id", "zaman"}
    assert v["eylem"] == "yetki_ver" and v["aktor_id"] == YETKILI and v["hedef_id"] == 300
    metin = bicim.bildirim_metni("denetim", v)
    assert str(YETKILI) in metin and "300" in metin and "Europe" not in metin
    assert "token" not in metin.lower().replace("token'", "")


def test_f2_degisiklik_olmayan_islem_bildirim_uretmez(yconn, saat):
    yonetim.yetki_ver(yconn, saat, KURUCU, YETKILI, 300)
    saat.ilerle(config.AUDIT_DEDUP_WINDOW_SECONDS + 1)
    assert yonetim.yetki_ver(yconn, saat, KURUCU, YETKILI, 300) is False  # zaten yetkili
    assert len(_denetim(yconn)) == 1


def test_f2_ayni_eylem_pencerede_tek_bildirim_pencere_sonrasi_yeniden(yconn, saat):
    """BİLİNÇLİ DEĞİŞİKLİK (QA ORTA-3): tekrar engeli yalnız AYNI eylemin arka arkaya tekrarını bastırır;
    karşıt eylem (ver<->al) görülünce karşıtın engeli kalkar ki kurucunun gördüğü SON bildirim gerçek durumu
    yansıtsın. Sel koruması saatlik üst sınır + özet ile sürer (test_f2_sel_tek_ozet_ve_ust_sinir)."""
    ayni = dict(eylem="yetki_ver", aktor_id=YETKILI, hedef_id=300)
    assert yonetim.denetim_bildir(yconn, saat, KURUCU, **ayni) is True
    assert yonetim.denetim_bildir(yconn, saat, KURUCU, **ayni) is False  # pencerede tekrar yok
    saat.ilerle(config.AUDIT_DEDUP_WINDOW_SECONDS + 1)
    assert yonetim.denetim_bildir(yconn, saat, KURUCU, **ayni) is True  # pencere sonrası yeniden
    assert [b["veri"]["eylem"] for b in _denetim(yconn)] == ["yetki_ver", "yetki_ver"]


def test_f2_kurucunun_kendi_eylemi_bildirim_uretmez_bayrakla_acilir(yconn, saat, monkeypatch):
    assert config.AUDIT_NOTIFY_OWNER_ACTIONS is False
    yonetim.yetki_ver(yconn, saat, KURUCU, KURUCU, 300)
    yonetim.grup_onayla(yconn, saat, KURUCU, KURUCU, -5000)
    assert _denetim(yconn) == []
    monkeypatch.setattr(config, "AUDIT_NOTIFY_OWNER_ACTIONS", True)
    yonetim.yetki_ver(yconn, saat, KURUCU, KURUCU, 301)
    assert len(_denetim(yconn)) == 1


def test_f2_tum_eylem_turleri_bildirilir(yconn, saat):
    yonetim.grup_onayla(yconn, saat, KURUCU, YETKILI, -5000)
    yonetim.yetki_ver(yconn, saat, KURUCU, YETKILI, 300)
    yonetim.yetki_al(yconn, saat, KURUCU, YETKILI, 300)
    yonetim.grup_kaldir_sistem(yconn, saat, KURUCU, -5000, neden="grup_goc")
    b = _denetim(yconn)
    assert [(x["veri"]["eylem"], x["veri"]["aktor_id"], x["veri"]["hedef_id"]) for x in b] == [
        ("grup_onayla", YETKILI, -5000), ("yetki_ver", YETKILI, 300), ("yetki_al", YETKILI, 300),
        ("grup_goc_dusur", None, -5000),
    ]
    for x in b:
        bicim.bildirim_metni("denetim", x["veri"])  # biçim hatasız


def test_f2_grup_kaldir_servis_degisiklik_yoksa_bildirim_yok(yconn, saat):
    yonetim.grup_kaldir(yconn, saat, KURUCU, KURUCU, -7777)  # onaylı değildi
    assert _denetim(yconn) == []


def test_f2_sel_tek_ozet_ve_ust_sinir(yconn, saat):
    n = config.AUDIT_NOTIFY_MAX_PER_HOUR
    for i in range(n + 4):
        yonetim.yetki_ver(yconn, saat, KURUCU, YETKILI, 400 + i)
    assert len(_denetim(yconn)) == n
    oz = bildirimler(yconn, "denetim_ozet")
    assert len(oz) == 1 and oz[0]["veri"]["sayi"] == 4 and oz[0]["hedef_sohbet"] == KURUCU
    assert "4" in bicim.bildirim_metni("denetim_ozet", oz[0]["veri"])


def test_f2_basarisiz_gonderim_yeniden_denenir(yconn, saat):
    yonetim.yetki_ver(yconn, saat, KURUCU, YETKILI, 300)
    g = SahteGonderici(hata_sayisi=1)
    o = asyncio.run(outbox.bekleyenleri_gonder(yconn, g, saat))
    assert o.yeniden_denenecek == 1 and g.gonderilen == []
    saat.ilerle(config.OUTBOX_BACKOFF_BASE_SECONDS + 1)
    o = asyncio.run(outbox.bekleyenleri_gonder(yconn, g, saat))
    assert o.gonderilen == 1 and g.gonderilen[0][0] == KURUCU and g.gonderilen[0][1] == "denetim"


def test_f2_kuyruga_ekleme_hatasi_asil_islemi_geri_almaz(yconn, saat, monkeypatch):
    def patla(*a, **k):
        raise RuntimeError("outbox bozuk")
    monkeypatch.setattr(outbox, "kuyruga_ekle", patla)
    assert yonetim.yetki_ver(yconn, saat, KURUCU, YETKILI, 300) is True
    assert 300 in yonetim.durum_yukle(yconn, KURUCU).yetkililer


def test_f2_bildirim_yalniz_kurucu_ozel_sohbetine_gider(ui, conn, saat):
    run(ui.komut(grup_komut("yetki_ver", "300", kul=YETKILI)))
    run(ui.komut(grup_komut("grup_onayla", "-4444", kul=YETKILI)))
    ui.grup_goc(G2, -100999)
    b = _denetim(conn)
    assert {x["veri"]["eylem"] for x in b} == {"yetki_ver", "grup_onayla", "grup_goc_dusur"}
    for x in conn.execute("SELECT hedef_sohbet, grup_id FROM outbox WHERE tur IN ('denetim','denetim_ozet')"):
        assert x["hedef_sohbet"] == KURUCU and x["grup_id"] is None
    # grup onayı kalkınca grup satırları iptal olur ama kurucu denetim satırları bekler
    assert all(x["durum"] == "bekliyor" for x in b)
    g = SahteGonderici()
    asyncio.run(outbox.bekleyenleri_gonder(conn, g, saat))
    assert {s for s, t, _ in g.gonderilen if t == "denetim"} == {KURUCU}


def test_f2_bicim_html_kacisli():
    m = bicim.bildirim_metni("denetim", {"eylem": "<b>x</b>", "aktor_id": "<i>", "hedef_id": "&", "zaman": "bozuk"})
    assert "<b>x</b>" not in m and "<i>" not in m


# ------------------------------------------------------------ F1-3 (E13 kalanı)
from dipbot.kaynak import EksikVeri, OyuncuAnlik  # noqa: E402


def _mukerrer_kur(conn, servis, src):
    src.oyuncular["a"] = OyuncuAnlik("a", "A", "Varna", "U1", uid="UUID-A")
    src.parti("P", "PARTI", [("a", "A")])
    run(servis.parti_ekle(G1, "P", src))
    src.oyuncular["a2"] = OyuncuAnlik("a2", "A", "Varna", "U1", uid="UUID-A")
    src.parti("P", "PARTI", [("a", "A"), ("a2", "A")])
    run(servis.partiler_tara(G1, src))
    run(servis.oyuncular_tara(G1, src))
    assert takipte(conn, G1) == {"a"}


def _engeller(conn):
    return conn.execute("SELECT oyuncu_id FROM engeller WHERE group_id=?", (G1,)).fetchall()


def test_f3a_karsidaki_kayit_silinince_mukerrer_engeli_kalkar(conn, servis, src):
    _mukerrer_kur(conn, servis, src)
    assert [r[0] for r in _engeller(conn)] == ["a2"]
    assert servis.oyuncu_sil(G1, "a") is True
    assert [r[0] for r in _engeller(conn)] == ["a"]  # "a" partide: kendi elle_silindi engeli; a2 engeli KALKTI
    # parti listesi hâlâ a2'yi içerir: engel kalktığı için artık takibe alınır
    run(servis.partiler_tara(G1, src))
    assert "a2" in takipte(conn, G1)


def test_f3a_ilgisiz_kayit_silinince_engel_korunur(conn, servis, src):
    _mukerrer_kur(conn, servis, src)
    src.oyuncu("z", "Z")
    run(servis.oyuncu_ekle(G1, "z", src))
    servis.oyuncu_sil(G1, "z")
    assert [r[0] for r in _engeller(conn)] == ["a2"]


def test_f3b_mukerrer_numara_uyelik_bildiriminde_listelenmez(conn, servis, src):
    _mukerrer_kur(conn, servis, src)
    aktif = [b for b in bildirimler(conn, "parti_uyelik") if b["durum"] != "iptal"]
    for b in aktif:
        assert "a2" not in [k["oyuncu_id"] for k in b["veri"]["katilanlar"]]
    assert aktif == [], "içeriği yalnız mükerrer numaradan ibaret bildirim iptal edilmeli"


def test_f3b_gercek_katilan_bildirimde_kalir_mukerrer_ayiklanir(conn, servis, src):
    src.oyuncular["a"] = OyuncuAnlik("a", "A", "Varna", "U1", uid="UUID-A")
    src.parti("P", "PARTI", [("a", "A")])
    run(servis.parti_ekle(G1, "P", src))
    src.oyuncular["a2"] = OyuncuAnlik("a2", "A", "Varna", "U1", uid="UUID-A")
    src.oyuncular["c"] = OyuncuAnlik("c", "C", "Varna", "U2", uid="UUID-C")
    src.parti("P", "PARTI", [("a", "A"), ("a2", "A"), ("c", "C")])
    run(servis.partiler_tara(G1, src))
    run(servis.oyuncular_tara(G1, src))
    aktif = [b for b in bildirimler(conn, "parti_uyelik") if b["durum"] != "iptal"]
    assert len(aktif) == 1
    assert [k["oyuncu_id"] for k in aktif[0]["veri"]["katilanlar"]] == ["c"]


def test_f3c_parti_ekle_tekrarli_uye_komutu_dusurmez_veri_yazilmaz(conn, servis, src):
    for u in "ab":
        src.oyuncu(u, u.upper())
    src.parti("P", "PARTI", [("a", "A"), ("b", "B"), ("a", "A")])
    try:
        s = run(servis.parti_ekle(G1, "P", src))
    except EksikVeri:
        s = None  # temiz, tipli hata: arayüz mesajlar; çökme/rollback artığı yok
    assert s is None or s.durum == "eklendi"
    if s is None:
        assert conn.execute("SELECT COUNT(*) FROM takip_partiler").fetchone()[0] == 0
        assert takipte(conn, G1) == set()
    else:
        assert takipte(conn, G1) == {"a", "b"}
