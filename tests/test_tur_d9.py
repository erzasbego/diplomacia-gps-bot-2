"""Tur F / D9: supergroup göçünde veri KURUCU ONAYLI güvenli taşınır. MOCK; canlı doğrulama DEĞİLDİR."""
import pytest
from conftest import bildirimler, run
from tg_fakes import (
    G1, G2, G3, KURUCU, YETKILI, YABANCI, dugme, gercek_grup_komut, grup_dugme, grup_komut, komut, onay_kimligi,
)

from dipbot import config, outbox, yonetim
from dipbot.kaynak import SavasOzet
from dipbot.takip import TakipServisi
from dipbot.tg.arayuz import Arayuz

YENI = -1009001
YENI2 = -1009002
ESKI2 = -1007777

# group_id / grup_id içeren her tablo burada ya "taşınır" ya da gerekçeli istisnadır.
TABLOLAR = [
    "takip_oyuncular", "takip_partiler", "parti_uyeleri", "engeller", "savaslar",
    "bulunamadi_sayaclari", "grup_ayarlari", "tarama_isleri", "onayli_gruplar", "outbox",
    "yetkisiz_denemeler", "tg_islemler", "grup_gocleri", "sistem_durumu", "yetkili_kullanicilar",
    "bildirim_sinirlari", "savas_detay_onbellek",
]


@pytest.fixture
def ui(conn, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return Arayuz(conn, src, KURUCU, saat=saat)


def _tohum(conn, saat, g):
    """Grup g için HER grup-bağlı tabloya veri yazar."""
    from dipbot.zaman import iso
    t = iso(saat())
    conn.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, baseline_tamam, eklendi, uid) VALUES(?,?,?,?,?,?)",
                 (g, "11", "A", 1, t, "u-11"))
    conn.execute("INSERT INTO takip_oyuncular(group_id, oyuncu_id, ad, baseline_tamam, eklendi) VALUES(?,?,?,?,?)",
                 (g, "12", "B", 0, t))
    conn.execute("INSERT INTO takip_partiler(group_id, parti_id, ad, baseline_tamam, eklendi) VALUES(?,?,?,?,?)",
                 (g, "P1", "Parti", 1, t))
    conn.execute("INSERT INTO parti_uyeleri(group_id, parti_id, oyuncu_id, ad) VALUES(?,?,?,?)", (g, "P1", "11", "A"))
    conn.execute("INSERT INTO parti_uyeleri(group_id, parti_id, oyuncu_id, ad) VALUES(?,?,?,?)", (g, "P1", "12", "B"))
    conn.execute("INSERT INTO engeller(group_id, oyuncu_id, neden, olusturuldu) VALUES(?,?,?,?)", (g, "99", "x", t))
    conn.execute("INSERT INTO savaslar(group_id, savas_id, ad, ilk_gorulme) VALUES(?,?,?,?)", (g, "w1", "S1", t))
    conn.execute("INSERT INTO savaslar(group_id, savas_id, ad, ilk_gorulme) VALUES(?,?,?,?)", (g, "w2", "S2", t))
    conn.execute("INSERT INTO bulunamadi_sayaclari(group_id, tur, kimlik, sayac) VALUES(?,?,?,?)", (g, "oyuncu", "11", 2))
    conn.execute("INSERT OR IGNORE INTO tarama_isleri(group_id, is_tipi, son_basari) VALUES(?,?,?)", (g, "oyuncular", t))
    conn.execute("INSERT OR IGNORE INTO grup_ayarlari(group_id) VALUES(?)", (g,))
    conn.execute("UPDATE grup_ayarlari SET tarama_araligi_dk=10, savas_baseline_tamam=1, ardisik_hata=0 WHERE group_id=?", (g,))
    conn.execute("INSERT INTO sistem_durumu(anahtar, deger) VALUES(?, '3')", (f"savas_taninmayan:{g}",))
    outbox.kuyruga_ekle(conn, hedef_sohbet=g, grup_id=g, tur="yeni_savas", idem_anahtari=f"s{g}",
                        veri={"savas_id": "w1"}, simdi=saat())


def _anlik(conn):
    """Tüm tabloların içerik özeti (değişmezlik testleri için)."""
    out = {}
    for (ad,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        out[ad] = sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {ad}"))
    return out


def _say(conn, g):
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE group_id=?", (g,)).fetchone()[0]
        for t in ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "engeller", "savaslar",
                  "bulunamadi_sayaclari", "tarama_isleri")
    }


def _hazir(ui, conn, saat, eski=G1, yeni=YENI, onayla=True):
    _tohum(conn, saat, eski)
    ui.grup_goc(eski, yeni)
    if onayla:
        yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, yeni)


def _tasi_komutu(ui, eski=G1, yeni=YENI, **kw):
    return run(ui.komut(komut("grup_tasi", f"{eski} {yeni}", **kw)))


def _tasi(ui, eski=G1, yeni=YENI):
    y = _tasi_komutu(ui, eski, yeni)
    return run(ui.buton(dugme(f"c:e:{onay_kimligi(y)}")))


# ---------------------------------------------------------------- G1
def test_goc_olayi_yalniz_kayit_uretir_yetki_vermez(ui, conn, saat):
    _tohum(conn, saat, G1)
    onceki = _say(conn, G1)
    ui.grup_goc(G1, YENI)
    ui.grup_goc(G1, YENI)  # tekrar: tek kayıt
    r = conn.execute("SELECT eski_id, yeni_id, durum FROM grup_gocleri").fetchall()
    assert [tuple(x) for x in r] == [(G1, YENI, "bekliyor")]
    d = yonetim.durum_yukle(conn, KURUCU)
    assert YENI not in d.onayli_gruplar and G1 not in d.onayli_gruplar
    assert _say(conn, G1) == onceki, "eski veri silinmez"
    # BİLİNÇLİ DEĞİŞİKLİK: yeni kimlik grup olarak artık yok sayılır (gruplar komut işlemez: yanıt da kayıt da yok);
    # kurucu özel sohbette onaysız YENI'yi seçse de seçim kabul edilmez (liste YENI'ye geçmez).
    assert run(ui.komut(gercek_grup_komut("oyuncular", grup=YENI))) is None
    assert run(ui.komut(gercek_grup_komut("oyuncular", grup=YENI, kul=YETKILI))) is None
    run(ui.komut(grup_komut("oyuncular", grup=YENI)))
    assert ui._secili.get(KURUCU) != YENI
    assert not conn.execute("SELECT 1 FROM yetkisiz_denemeler").fetchall()


# ---------------------------------------------------------------- G2/G4
def test_kurucu_ozelde_tasir_tum_tablolar(ui, conn, saat):
    _hazir(ui, conn, saat)
    onceki = _say(conn, G1)
    ayar_once = conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()
    bek = outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="ek",
                              veri={}, simdi=saat())
    assert bek
    ui.islemler.olustur("oyuncu_sil", G1, YETKILI, G1, {"id": "11"})
    y = _tasi(ui)
    assert "Players: 2" in y.metin and "parties: 1" in y.metin, y.metin
    assert _say(conn, YENI) == onceki
    assert all(v == 0 for v in _say(conn, G1).values())
    assert conn.execute("SELECT COUNT(*) FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()[0] == 0
    a = conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (YENI,)).fetchone()
    assert a["tarama_araligi_dk"] == 10 and a["savas_baseline_tamam"] == 1
    assert a["sonraki_tarama"] == ayar_once["sonraki_tarama"]
    assert conn.execute("SELECT sayac FROM bulunamadi_sayaclari WHERE group_id=?", (YENI,)).fetchone()[0] == 2
    # baseline bayrakları aynen korunur (ilk gözlem sayılmaz)
    b = dict(conn.execute("SELECT oyuncu_id, baseline_tamam FROM takip_oyuncular WHERE group_id=?", (YENI,)).fetchall())
    assert b == {"11": 1, "12": 0}
    # grup-bazlı durum anahtarı taşındı
    assert conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar=?", (f"savas_taninmayan:{YENI}",)).fetchone()[0] == "3"
    assert conn.execute("SELECT COUNT(*) FROM sistem_durumu WHERE anahtar=?", (f"savas_taninmayan:{G1}",)).fetchone()[0] == 0
    # eski id'deki bekleyen outbox + tg_islemler iptal
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", (G1,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tg_islemler WHERE grup_id=?", (G1,)).fetchone()[0] == 0
    assert conn.execute("SELECT durum FROM grup_gocleri").fetchone()[0] == "tamamlandi"
    # yeni grup hâlâ onaylı, eski onaysız
    d = yonetim.durum_yukle(conn, KURUCU)
    assert YENI in d.onayli_gruplar and G1 not in d.onayli_gruplar


# ---------------------------------------------------------------- yetki
def test_yetkili_ozelde_veya_grupta_tasiyamaz_kurucu_grupta_da_tasiyamaz(ui, conn, saat):
    _hazir(ui, conn, saat)
    once = _anlik(conn)
    # yetkili kullanıcı: özel sohbet kurucuya özel
    y = run(ui.komut(komut("grup_tasi", f"{G1} {YENI}", kul=YETKILI)))
    assert y is not None and not any(v.startswith("c:e:") for v in (d.veri or "" for s in (y.klavye or []) for d in s))
    # yetkili kullanıcı onaylı yeni grubun içinden
    kayit_once = conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0]
    # BİLİNÇLİ DEĞİŞİKLİK: grup içinden gelen komut (yetkili / kurucu / yabancı) artık YANIT bile üretmez (None)
    # ve yetkisiz-deneme kaydı düşmez; eskiden yetkili/kurucu için 'onay düğmesiz' bir yanıt dönerdi.
    assert run(ui.komut(grup_komut("grup_tasi", f"{G1} {YENI}", grup=YENI, kul=YETKILI))) is None
    assert run(ui.komut(gercek_grup_komut("grup_tasi", f"{G1} {YENI}", grup=YENI, kul=KURUCU))) is None
    assert run(ui.komut(grup_komut("grup_tasi", f"{G1} {YENI}", grup=G2, kul=YABANCI))) is None
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == kayit_once
    sonra = _anlik(conn)
    # yalnız yetkisiz deneme/bildirim kayıtları artabilir; takip verisi ve göç kaydı aynı
    for t in ("takip_oyuncular", "takip_partiler", "parti_uyeleri", "engeller", "savaslar", "grup_gocleri", "grup_ayarlari"):
        assert sonra[t] == once[t], t
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE user_id=?", (YETKILI,)).fetchone()[0] >= 1


def test_onay_islemi_grup_icinden_veya_baskasi_tarafindan_uygulanmaz(ui, conn, saat):
    _hazir(ui, conn, saat)
    once = _anlik(conn)
    # elle üretilmiş işlem: yetkili kullanıcı adına
    kid = ui.islemler.olustur("grup_tasi", YENI, YETKILI, None, {"eski": G1, "yeni": YENI})
    run(ui.buton(grup_dugme(f"c:e:{kid}", grup=YENI, kul=YETKILI)))
    # kurucu adına ama grup içinde
    kid = ui.islemler.olustur("grup_tasi", YENI, KURUCU, None, {"eski": G1, "yeni": YENI})
    run(ui.buton(grup_dugme(f"c:e:{kid}", grup=YENI, kul=KURUCU)))
    sonra = _anlik(conn)
    for t in ("takip_oyuncular", "parti_uyeleri", "grup_gocleri"):
        assert sonra[t] == once[t], t


def test_servis_aktor_kurucu_degilse_reddeder(ui, conn, saat):
    _hazir(ui, conn, saat)
    with pytest.raises(yonetim.YetkiHatasi):
        yonetim.grup_tasi(conn, saat, KURUCU, YETKILI, G1, YENI)
    with pytest.raises(yonetim.YetkiHatasi):
        yonetim.grup_tasi(conn, saat, KURUCU, None, G1, YENI)
    assert _say(conn, G1)["takip_oyuncular"] == 2


def test_kayitsiz_cift_reddedilir(ui, conn, saat):
    _hazir(ui, conn, saat)
    once = _anlik(conn)
    y = _tasi_komutu(ui, G1, YENI2)
    assert not any(v.startswith("c:e:") for v in (d.veri or "" for s in (y.klavye or []) for d in s))
    with pytest.raises(yonetim.TasimaHatasi):
        yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, YENI2)
    assert _anlik(conn)["takip_oyuncular"] == once["takip_oyuncular"]


@pytest.mark.parametrize("arg", [f"{G1} {G1}", f"5 {YENI}", f"{G1} 5", f"{G1}", "", "a b"])
def test_gecersiz_kimlik_reddedilir(ui, conn, saat, arg):
    _hazir(ui, conn, saat)
    once = _anlik(conn)
    y = run(ui.komut(komut("grup_tasi", arg)))
    assert y is not None and not any(v.startswith("c:e:") for v in (d.veri or "" for s in (y.klavye or []) for d in s))
    assert _anlik(conn) == once


def _reddedilir(ui, conn):
    once = _anlik(conn)
    y = _tasi_komutu(ui)
    kid = None
    for s in (y.klavye or []):
        for d in s:
            if (d.veri or "").startswith("c:e:"):
                kid = d.veri.split(":")[2]
    if kid is not None:  # onay öncesi doğrulama kaçırdıysa onayda da reddedilmeli
        run(ui.buton(dugme(f"c:e:{kid}")))
    with pytest.raises(yonetim.TasimaHatasi):
        yonetim.grup_tasi(conn, ui.saat, KURUCU, KURUCU, G1, YENI)
    sonra = _anlik(conn)
    for t in once:
        if t in ("yetkisiz_denemeler", "tg_islemler"):
            continue
        assert sonra[t] == once[t], t


def test_yeni_grup_onaysiz_reddedilir(ui, conn, saat):
    _hazir(ui, conn, saat, onayla=False)
    _reddedilir(ui, conn)


def test_eski_grup_hala_onayliysa_reddedilir(ui, conn, saat):
    _hazir(ui, conn, saat)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, G1)
    _reddedilir(ui, conn)


@pytest.mark.parametrize("tablo", ["takip_oyuncular", "takip_partiler", "parti_uyeleri", "engeller",
                                   "bulunamadi_sayaclari"])
def test_yeni_grupta_veri_varsa_reddedilir(ui, conn, saat, tablo):
    _hazir(ui, conn, saat)
    t = "2026-10-19T12:00:00+00:00"
    ek = {
        "takip_oyuncular": ("INSERT INTO takip_oyuncular(group_id, oyuncu_id, eklendi) VALUES(?, 'z', ?)", t),
        "takip_partiler": ("INSERT INTO takip_partiler(group_id, parti_id, eklendi) VALUES(?, 'z', ?)", t),
        "parti_uyeleri": ("INSERT INTO parti_uyeleri(group_id, parti_id, oyuncu_id) VALUES(?, 'z', ?)", "z"),
        "engeller": ("INSERT INTO engeller(group_id, oyuncu_id, neden, olusturuldu) VALUES(?, 'z', 'x', ?)", t),
        "savaslar": ("INSERT INTO savaslar(group_id, savas_id, ilk_gorulme) VALUES(?, 'z', ?)", t),
        "bulunamadi_sayaclari": ("INSERT INTO bulunamadi_sayaclari(group_id, tur, kimlik) VALUES(?, 'oyuncu', ?)", "z"),
    }[tablo]
    conn.execute(ek[0], (YENI, ek[1]))
    _reddedilir(ui, conn)


def test_yeni_grupta_savas_taramasi_baslamissa_tasima_yine_calisir(ui, conn, saat):
    """BİLİNÇLİ DEĞİŞİKLİK (QA-A YÜKSEK-1): yeni grup onaylanınca zamanlayıcı savaş taramasını kendiliğinden
    başlatır; bu önceden taşımayı kalıcı engelliyordu. Savaş kayıtları artık birleştirilir
    (ayrıntı: tests/test_qa_bulgulari.py)."""
    _hazir(ui, conn, saat)
    conn.execute("UPDATE grup_ayarlari SET savas_baseline_tamam=1 WHERE group_id=?", (YENI,))
    conn.execute("INSERT INTO savaslar(group_id, savas_id, ilk_gorulme) VALUES(?, 'z', 't')", (YENI,))
    sayilar = yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, YENI)
    assert sayilar["takip_oyuncular"] == 2


def test_eski_grupta_tasinacak_veri_yoksa_reddedilir(ui, conn, saat):
    ui.grup_goc(G1, YENI)  # G1'de takip verisi yok
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, YENI)
    yonetim.grup_kaldir(conn, saat, KURUCU, KURUCU, G1)
    with pytest.raises(yonetim.TasimaHatasi):
        yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, YENI)


# ---------------------------------------------------------------- atomiklik
def test_transaction_ortasinda_hata_tam_geri_alinir(ui, conn, saat, monkeypatch):
    _hazir(ui, conn, saat)
    once = _anlik(conn)

    def patla(*a, **k):
        raise RuntimeError("enjekte hata")

    monkeypatch.setattr(outbox, "grup_bekleyenlerini_iptal_et", patla)
    with pytest.raises(RuntimeError):
        yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, YENI)
    assert not conn.in_transaction
    assert _anlik(conn) == once
    assert conn.execute("SELECT durum FROM grup_gocleri").fetchone()[0] == "bekliyor"
    # UI yolu da çökmeden "değişmedi" söyler
    monkeypatch.setattr(outbox, "grup_bekleyenlerini_iptal_et", patla)
    y = _tasi(ui)
    assert y is not None and "rolled back" in y.metin and "nothing was moved" in y.metin
    assert _anlik(conn)["takip_oyuncular"] == once["takip_oyuncular"]


# ---------------------------------------------------------------- izolasyon / tekrar
def test_baska_gruplarin_verisi_ve_diger_cift_dokunulmaz(ui, conn, saat):
    _tohum(conn, saat, G2)
    _tohum(conn, saat, ESKI2)
    _hazir(ui, conn, saat)
    ui.grup_goc(ESKI2, YENI2)  # ikinci kayıtlı çift: onaylı değil ama veri var
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, YENI2)
    g2_once, e2_once = _say(conn, G2), _say(conn, ESKI2)
    ayar_g2 = tuple(conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (G2,)).fetchone())
    _tasi(ui)
    assert _say(conn, G2) == g2_once and _say(conn, ESKI2) == e2_once
    assert tuple(conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (G2,)).fetchone()) == ayar_g2
    assert _say(conn, YENI2)["takip_oyuncular"] == 0
    assert conn.execute("SELECT durum FROM grup_gocleri WHERE eski_id=?", (ESKI2,)).fetchone()[0] == "bekliyor"
    # çift karışmaz: ESKI2 verisi YENI'ye değil, onaylıysa yalnız kendi hedefine taşınabilir
    with pytest.raises(yonetim.TasimaHatasi):
        yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, ESKI2, YENI)
    yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, ESKI2, YENI2)
    assert _say(conn, YENI2)["takip_oyuncular"] == 2 and _say(conn, YENI)["takip_oyuncular"] == 2


def test_ikinci_tasima_reddedilir(ui, conn, saat):
    _hazir(ui, conn, saat)
    _tasi(ui)
    once = _anlik(conn)
    with pytest.raises(yonetim.TasimaHatasi):
        yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, G1, YENI)
    assert _anlik(conn) == once
    y = _tasi_komutu(ui)
    assert not any(v.startswith("c:e:") for v in (d.veri or "" for s in (y.klavye or []) for d in s))


# ---------------------------------------------------------------- baseline / bildirim seli
def test_tasinan_savas_baseline_ilk_taramada_bildirim_uretmez(ui, conn, saat, src):
    _hazir(ui, conn, saat)
    _tasi(ui)
    src.savaslar = [SavasOzet("w1", "S1", durum="active", war_number=1), SavasOzet("w2", "S2", durum="active", war_number=2)]
    sv = TakipServisi(conn, saat)
    run(sv.savaslari_tara(YENI, src))
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor' AND tur LIKE '%savas%'",
                        (YENI,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (YENI,)).fetchone()[0] == 2


# ---------------------------------------------------------------- denetim
def test_denetim_bildirimi_yalniz_kimlik_ve_bayrak(ui, conn, saat, monkeypatch):
    _hazir(ui, conn, saat)
    monkeypatch.setattr(config, "AUDIT_NOTIFY_OWNER_ACTIONS", False)
    n0 = len(bildirimler(conn, "denetim"))
    _tasi(ui)
    assert len(bildirimler(conn, "denetim")) == n0, "kurucunun kendi eylemi bildirilmez (bayrak kapalı)"

    # bayrak açıkken: ikinci taşıma
    _tohum(conn, saat, ESKI2)
    ui.grup_goc(ESKI2, YENI2)
    yonetim.grup_onayla(conn, saat, KURUCU, KURUCU, YENI2)
    monkeypatch.setattr(config, "AUDIT_NOTIFY_OWNER_ACTIONS", True)
    yonetim.grup_tasi(conn, saat, KURUCU, KURUCU, ESKI2, YENI2)
    b = [x for x in bildirimler(conn, "denetim", hedef=KURUCU) if x["veri"]["eylem"] == "grup_tasi"]
    assert len(b) == 1
    assert set(b[0]["veri"]) == {"eylem", "aktor_id", "hedef_id", "zaman"}
    assert b[0]["veri"]["aktor_id"] == KURUCU and b[0]["veri"]["hedef_id"] == YENI2
    assert b[0]["grup_id"] is None


# ---------------------------------------------------------------- şema taraması
def test_semadaki_tum_group_id_tablolari_tasima_tarafindan_ele_alinir(ui, conn):
    bulunan = set()
    for (ad,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        sut = {r[1] for r in conn.execute(f"PRAGMA table_info({ad})")}
        if sut & {"group_id", "grup_id"}:
            bulunan.add(ad)
    ele_alinan = set(yonetim.TASINAN_TABLOLAR) | set(yonetim.TASIMA_ISTISNALARI)
    assert bulunan <= ele_alinan, f"ele alınmamış grup-bağlı tablo: {bulunan - ele_alinan}"
    assert set(yonetim.TASINAN_TABLOLAR) <= bulunan
    # yalnız yeni oluşan göç tablosu id sütunları eski_id/yeni_id; sistem_durumu anahtarı ayrıca ele alınır
    assert "savas_taninmayan:" in yonetim.TASINAN_DURUM_ANAHTARLARI
