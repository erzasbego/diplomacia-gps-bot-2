"""Telegram arayüz mantığı (Arayuz): yetki matrisi, butonlar, onay akışları, ekranlar.

MOCK testler: sahte kaynak, bellek içi SQLite, ağ yok. Canlı Telegram doğrulaması DEĞİLDİR.
"""
import pytest

from conftest import bildirimler, run, takipte
from tg_fakes import (
    G1, G2, G3, KURUCU, YABANCI, YETKILI,
    dugme, grup_dugme, grup_komut, komut, onay_kimligi, veriler,
)

from dipbot import config, yonetim
from dipbot.kaynak import (
    DiplomaciaSource, SavasDetay, SavasOzet, SavasTaraf, SiteErisilemez,
)
from dipbot.tg import arayuz as ar
from dipbot.tg.arayuz import Arayuz, KOMUTLAR, kimlik_coz
from dipbot.tg.kaynak_yok import KaynakYapilandirilmadi, YapilandirilmamisKaynak
from dipbot.tg.modeller import CALLBACK_AZAMI_BAYT, Dugme, Olay


@pytest.fixture
def ui(conn, src, saat):
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    return Arayuz(conn, src, KURUCU, saat=saat)


def _her_dugme_kisa(yanit):
    for v in veriler(yanit):
        assert len(v.encode("utf-8")) <= CALLBACK_AZAMI_BAYT, v


# ---------------------------------------------------------------- yetki matrisi
KISILER = {"kurucu": KURUCU, "yetkili": YETKILI, "yetkisiz": YABANCI}
SOHBETLER = ["ozel", "onayli_grup", "onaysiz_grup"]
# (kişi, sohbet) -> işlem yapabilir mi?
BEKLENEN = {
    ("kurucu", "ozel"): True, ("yetkili", "ozel"): False, ("yetkisiz", "ozel"): False,
    ("kurucu", "onayli_grup"): True, ("yetkili", "onayli_grup"): True, ("yetkisiz", "onayli_grup"): False,
    ("kurucu", "onaysiz_grup"): False, ("yetkili", "onaysiz_grup"): False, ("yetkisiz", "onaysiz_grup"): False,
}


def _sohbet_args(kisi_id, sohbet):
    if sohbet == "ozel":
        return dict(sohbet=kisi_id, tur="private", kul=kisi_id)
    return dict(sohbet=G1 if sohbet == "onayli_grup" else G3, tur="supergroup", kul=kisi_id)


@pytest.mark.parametrize("sohbet", SOHBETLER)
@pytest.mark.parametrize("kisi", list(KISILER))
def test_yetki_matrisi_komut(ui, conn, kisi, sohbet):
    y = run(ui.komut(komut("yardim", **_sohbet_args(KISILER[kisi], sohbet))))
    izinli = BEKLENEN[(kisi, sohbet)]
    if izinli:
        assert y is not None and "Commands" in y.metin
    else:
        assert y is None or "Commands" not in y.metin
        n = conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE user_id=?", (KISILER[kisi],)).fetchone()[0]
        assert n == 1, "yetkisiz deneme kaydedilmeli"


@pytest.mark.parametrize("sohbet", SOHBETLER)
@pytest.mark.parametrize("kisi", list(KISILER))
def test_yetki_matrisi_dugme(ui, conn, kisi, sohbet):
    y = run(ui.buton(dugme("m", **_sohbet_args(KISILER[kisi], sohbet))))
    izinli = BEKLENEN[(kisi, sohbet)]
    if izinli:
        assert y.metin and y.duzenle and not y.sessiz
    else:
        # reddedilen düğme menüyü AÇMAZ; açılır uyarı döner (answerCallbackQuery için)
        assert y.sessiz and y.uyari and y.uyari_goster and not y.metin


def test_yetkisiz_ozel_sohbet_yanit_ve_kurucuya_bildirim(ui, conn):
    y = run(ui.komut(komut("oyuncular", kul=YABANCI)))
    assert y.metin == ar.KAPALI_OZEL
    b = bildirimler(conn, "yetkisiz_deneme", hedef=KURUCU)
    assert len(b) == 1 and b[0]["veri"]["kullanici_id"] == YABANCI and b[0]["veri"]["islem"] == "/oyuncular"
    # tekrar deneme kurucuyu mesaj yağmuruna tutmaz (çekirdek hız sınırı)
    run(ui.komut(komut("oyuncular", kul=YABANCI)))
    run(ui.komut(komut("durum", kul=YABANCI)))
    assert len(bildirimler(conn, "yetkisiz_deneme")) == 1
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 3


def test_onayli_grupta_yetkisiz_kullanici_reddedilir_ve_kayit(ui, conn):
    y = run(ui.komut(grup_komut("oyuncular", kul=YABANCI)))
    assert y.metin == ar.YETKISIZ
    r = bildirimler(conn, "yetkisiz_deneme")[0]["veri"]
    assert r["grup_id"] == G1 and r["kullanici_id"] == YABANCI


def test_onaysiz_grup_sessiz_ama_kayitli(ui, conn):
    assert run(ui.komut(grup_komut("oyuncular", grup=G3))) is None
    assert bildirimler(conn, "yetkisiz_deneme")[0]["veri"]["grup_id"] == G3


def test_yetkisi_alinan_kullanicinin_eski_butonu_calismaz(ui, conn, saat):
    eski_dugme = "ol:0"
    assert run(ui.buton(grup_dugme(eski_dugme, kul=YETKILI))).metin
    yonetim.yetki_al(conn, saat, KURUCU, KURUCU, YETKILI)
    y = run(ui.buton(grup_dugme(eski_dugme, kul=YETKILI)))
    assert y.sessiz and y.uyari_goster and not y.metin
    # bekleyen bir onay düğmesi de çalışmaz
    # (yetki alınmadan önce üretilmiş onay)


def test_yetkisi_alinan_kullanicinin_bekleyen_onayi_gecersiz(ui, conn, saat, src):
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("oyuncu_ekle", "72400", kul=YETKILI)))
    y = run(ui.komut(grup_komut("oyuncu_sil", "72400", kul=YETKILI)))
    kid = onay_kimligi(y)
    yonetim.yetki_al(conn, saat, KURUCU, KURUCU, YETKILI)
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))
    assert r.sessiz and r.uyari_goster
    assert "72400" in takipte(conn, G1), "silme gerçekleşmemeli"


def test_baska_sohbete_ait_islem_kimligi_reddedilir(ui, conn, saat, src):
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("oyuncu_ekle", "72400")))
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, 66)
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "72400"))))
    # Aynı kimlik G2 sohbetinden (yetkili kurucu tarafından bile) kullanılamaz
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", grup=G2)))
    assert r.sessiz and "another chat/group" in r.uyari
    assert "72400" in takipte(conn, G1)
    assert any(x["veri"]["islem"] == "buton:baska_sohbet_islemi" for x in bildirimler(conn, "yetkisiz_deneme"))
    # İşlem hâlâ kendi sohbetinde geçerli
    ok = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert "removed from tracking" in ok.metin and "72400" not in takipte(conn, G1)


def test_baska_gruba_ait_hedefli_islem_reddedilir(ui, conn):
    # sohbet G2'de başlatılmış ama hedef grubu G1 olan sahte/bozuk işlem
    kid = ui.islemler.olustur("oyuncu_sil", G2, KURUCU, G1, {"id": "1"})
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", grup=G2)))
    assert r.sessiz and "another group" in r.uyari


def test_baska_gruptaki_oyuncu_gorunmez_ve_silinemez(ui, conn, src):
    src.oyuncu("1", "Gizli")
    run(ui.komut(grup_komut("oyuncu_ekle", "1", grup=G2)))
    # G1 bağlamında G2'nin oyuncusu yok
    assert run(ui.buton(grup_dugme("od:1"))).uyari_goster
    y = run(ui.komut(grup_komut("oyuncu_sil", "1")))
    assert "not tracked in this group" in y.metin and not y.klavye
    assert "1" in takipte(conn, G2)


def test_onay_baska_kullanici_tarafindan_verilemez(ui, conn, src):
    src.oyuncu("5", "Bir")
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "5"))))
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))
    assert r.sessiz and "Only the user who started the action" in r.uyari
    assert "5" in takipte(conn, G1)


def test_onay_suresi_dolar_ve_tek_kullanimlik(ui, conn, src, saat):
    src.oyuncu("5", "Bir")
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "5"))))
    ok = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert "removed from tracking" in ok.metin
    tekrar = run(ui.buton(grup_dugme(f"c:e:{kid}")))  # çift tıklama
    assert tekrar.metin == ar.ONAY_BITTI and tekrar.uyari
    # süre dolumu
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    kid2 = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "5"))))
    saat.ilerle(11 * 60)
    eski = run(ui.buton(grup_dugme(f"c:e:{kid2}")))
    assert eski.metin == ar.ONAY_BITTI
    assert "5" in takipte(conn, G1)


def test_bilinmeyen_eski_dugme_anlasilir_yanit(ui):
    y = run(ui.buton(grup_dugme("zzz:1")))
    assert y.uyari == ar.ESKI_DUGME and y.sessiz
    y = run(ui.buton(grup_dugme("c:e:yokboyle")))
    assert y.metin == ar.ONAY_BITTI


def test_vazgec_islemi_iptal_eder(ui, conn, src):
    src.oyuncu("5", "Bir")
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "5"))))
    y = run(ui.buton(grup_dugme(f"c:h:{kid}")))
    assert "cancelled" in y.metin and "5" in takipte(conn, G1)
    assert ui.islemler.getir(kid) is None


# ---------------------------------------------------------------- HTML kaçış / callback boyutu
EVIL = '<b>Ali</b> & "Veli" <script>x</script>'


def test_html_kacis_liste_ve_detay(ui, conn, src):
    src.oyuncu("9", EVIL, bolge="<i>Varna</i>", ulke="A&B")
    run(ui.komut(grup_komut("oyuncu_ekle", "9")))
    liste = run(ui.komut(grup_komut("oyuncular")))
    detay = run(ui.buton(grup_dugme("od:9")))
    for y in (liste, detay):
        assert "<script>" not in y.metin and "<b>Ali</b>" not in y.metin
        assert "&lt;script&gt;" in y.metin and "&amp;" in y.metin
    assert "&lt;i&gt;Varna&lt;/i&gt;" in detay.metin
    assert 'href="https://diplomacia.com.tr/player/9"' in detay.metin


def test_callback_data_64_bayt_sinirina_uyar(ui, conn, src):
    uzun = "9" * 12  # Tur A: kanonik en uzun numara (12 hane); "od:"+12 = 15 bayt, düğme sığar
    for i in range(1, 25):
        src.oyuncu(str(1000 + i), f"Oyuncu {i}")
        run(ui.komut(grup_komut("oyuncu_ekle", str(1000 + i))))
    src.oyuncu(uzun, "Uzun")
    run(ui.komut(grup_komut("oyuncu_ekle", uzun)))
    ekranlar = [
        run(ui.komut(grup_komut("start"))),
        run(ui.komut(grup_komut("oyuncular"))),
        run(ui.buton(grup_dugme("ol:2"))),
        run(ui.buton(grup_dugme("yo"))),
        run(ui.buton(grup_dugme("ta"))),
        run(ui.buton(grup_dugme("du"))),
        run(ui.komut(komut("start"))),
        run(ui.buton(dugme("yo"))),
        run(ui.buton(dugme("gl"))),
        run(ui.buton(dugme("tk"))),
    ]
    for y in ekranlar:
        _her_dugme_kisa(y)
    # 25 oyuncu -> 4 sayfa; uzun kimlikli oyuncunun kendi düğmesi atlanmış olmalı
    sayfa3 = run(ui.buton(grup_dugme("ol:3")))
    assert all(len(v.encode()) <= 64 for v in veriler(sayfa3))
    # onay düğmeleri sabit kısa anahtar taşır
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", uzun))))
    assert len(f"c:e:{kid}") <= 12


def test_dugme_64_bayt_uzerini_reddeder():
    with pytest.raises(ValueError):
        Dugme("x", "a" * 65)
    Dugme("x", "ç" * 32)  # 64 bayt (Türkçe karakter 2 bayt)
    with pytest.raises(ValueError):
        Dugme("x", "ç" * 33)


def test_komut_adlari_telegram_kuralina_uyar():
    import re
    assert len(KOMUTLAR) <= 100
    for ad, aciklama in KOMUTLAR.items():
        assert re.fullmatch(r"[a-z0-9_]{1,32}", ad), ad
        assert 1 <= len(aciklama) <= 256
    istenen = {
        "addplayer", "players", "removeplayer", "addparty", "parties", "removeparty", "wars",
        "scan", "status", "grant", "revoke", "approve_group", "remove_group", "help", "start",
        "move_group",  # D9: moving data in the owner's private chat
    }
    assert istenen == set(KOMUTLAR)
    # The menu is English-only: legacy alt-name Turkish commands and the short Turkish names are NOT listed ...
    assert not {"oyuncu_ekle", "oyuncu_sil", "parti_ekle", "parti_sil"} & set(KOMUTLAR)
    assert not set(ar.TR_KOMUTLAR) & set(KOMUTLAR)
    # ... but keep working as aliases (same handler); details: tests/test_kisa_komutlar.py.
    assert set(ar.EN_KOMUTLAR) == set(KOMUTLAR)
    assert set(ar.ESKI_KOMUTLAR) == {"oyuncu_ekle", "oyuncu_sil", "parti_ekle", "parti_sil"}
    for ad in (*ar.ESKI_KOMUTLAR, *ar.TR_KOMUTLAR, *KOMUTLAR):
        assert ar.komut_ic_adi(ad) is not None, ad


# ---------------------------------------------------------------- komutlar
def test_kimlik_coz():
    assert kimlik_coz("https://diplomacia.com.tr/player/72400", "player") == "72400"
    assert kimlik_coz("diplomacia.com.tr/player/72400/", "player") == "72400"
    assert kimlik_coz("72400", "player") == "72400"
    assert kimlik_coz("https://diplomacia.com.tr/party/5830", "party") == "5830"
    assert kimlik_coz("https://diplomacia.com.tr/party/5830", "player") is None  # tür karışmasın
    assert kimlik_coz("https://evil.example/player/1", "player") is None
    assert kimlik_coz("1 2", "player") is None and kimlik_coz("", "player") is None
    assert kimlik_coz("a<b>", "player") is None


def test_oyuncu_ekle_ozet_ve_tekrar(ui, conn, src):
    src.oyuncu("72400", "Ali", bolge="Varna", ulke="Ulke1")
    y = run(ui.komut(grup_komut("oyuncu_ekle", "https://diplomacia.com.tr/player/72400")))
    assert "Player added to tracking" in y.metin and "Varna" in y.metin and "Ulke1" in y.metin
    assert "not a change notification" in y.metin
    assert not bildirimler(conn, "oyuncu_degisti")
    assert "already tracked" in run(ui.komut(grup_komut("oyuncu_ekle", "72400"))).metin
    assert takipte(conn, G1) == {"72400"} and takipte(conn, G2) == set()
    bozuk = run(ui.komut(grup_komut("oyuncu_ekle", "bozuk link!"))).metin
    assert "Usage" in bozuk and "/addplayer" in bozuk  # the hint shows the English command name
    # the English menu name is the same handler
    assert "already tracked" in run(ui.komut(grup_komut("addplayer", "72400"))).metin


def test_kaynak_yapilandirilmadi_acik_hata_sahte_basari_yok(conn, saat):
    ui2 = Arayuz(conn, YapilandirilmamisKaynak(), KURUCU, saat=saat)
    assert isinstance(YapilandirilmamisKaynak(), DiplomaciaSource)
    y = run(ui2.komut(grup_komut("oyuncu_ekle", "1")))
    assert "not configured" in y.metin and "No record was changed" in y.metin
    assert takipte(conn, G1) == set()
    y = run(ui2.komut(grup_komut("savaslar")))
    assert "not configured" in y.metin and "unreachable" in y.metin.lower()
    assert "Active wars" not in y.metin
    with pytest.raises(KaynakYapilandirilmadi):
        run(YapilandirilmamisKaynak().fetch_player("1"))


def test_durum_yapilandirilmadi_gosterir(conn, saat):
    ui2 = Arayuz(conn, YapilandirilmamisKaynak(), KURUCU, saat=saat)
    y = run(ui2.komut(grup_komut("durum")))
    assert "Diplomacia connection: not configured" in y.metin


def test_oyuncu_listesi_sayfalama(ui, conn, src):
    for i in range(1, 21):
        src.oyuncu(str(i), f"Oyuncu {i:02d}")
        run(ui.komut(grup_komut("oyuncu_ekle", str(i))))
    p0 = run(ui.komut(grup_komut("oyuncular")))
    assert "total 20" in p0.metin and p0.metin.count("player/") == 8
    assert "ol:1" in veriler(p0) and "ol:-1" not in veriler(p0)
    p2 = run(ui.buton(grup_dugme("ol:2")))
    assert p2.metin.count("player/") == 4 and "ol:1" in veriler(p2) and "ol:3" not in veriler(p2)
    # taşan sayfa numarası son sayfaya kıstırılır
    assert run(ui.buton(grup_dugme("ol:99"))).metin.count("player/") == 4


def test_ayni_isimli_oyuncular_kimlikle_ayirt_edilir(ui, src):
    src.oyuncu("1", "Aynı Ad")
    src.oyuncu("2", "Aynı Ad")
    for i in ("1", "2"):
        run(ui.komut(grup_komut("oyuncu_ekle", i)))
    m = run(ui.komut(grup_komut("oyuncular"))).metin
    assert "(#1)" in m and "(#2)" in m


def test_tarama_komutu(ui, conn):
    assert "Invalid interval" in run(ui.komut(grup_komut("tarama", "7"))).metin
    assert "Invalid interval" in run(ui.komut(grup_komut("scan", "abc"))).metin
    assert yonetim.tarama_araligi(conn, G1) == config.DEFAULT_SCAN_INTERVAL_MINUTES
    assert "15 minutes" in run(ui.komut(grup_komut("tarama", "15"))).metin
    assert yonetim.tarama_araligi(conn, G1) == 15 and yonetim.tarama_araligi(conn, G2) == 10
    ekran = run(ui.komut(grup_komut("tarama")))
    assert set(veriler(ekran)) >= {"tv:5", "tv:10", "tv:15", "tv:20"}
    d = run(ui.buton(grup_dugme("tv:20")))
    assert d.duzenle and yonetim.tarama_araligi(conn, G1) == 20
    bad = run(ui.buton(grup_dugme("tv:3")))
    assert bad.sessiz and yonetim.tarama_araligi(conn, G1) == 20


def test_ozel_sohbette_grup_secimi_gerekir(ui, conn, src):
    src.oyuncu("1", "A")
    y = run(ui.komut(komut("oyuncular")))  # iki onaylı grup, seçim yok
    assert "Select the group" in y.metin and f"gs:{G1}" in veriler(y)
    y = run(ui.buton(dugme(f"gs:{G1}")))
    assert f"Selected group: <code>{G1}</code>" in y.metin
    run(ui.komut(komut("oyuncu_ekle", "1")))
    assert takipte(conn, G1) == {"1"} and takipte(conn, G2) == set()
    # grup onayı kalkarsa seçim geçersiz olur
    yonetim.grup_kaldir(conn, ui.saat, KURUCU, KURUCU, G1)
    # seçili grup artık onaylı değil: eski seçim KULLANILMAZ (tek kalan onaylı grup G2 otomatik seçilir)
    assert ui._grup(komut("oyuncular")) == G2
    yonetim.grup_kaldir(conn, ui.saat, KURUCU, KURUCU, G2)
    assert "No approved groups yet" in run(ui.komut(komut("oyuncular"))).metin
    yonetim.grup_onayla(conn, ui.saat, KURUCU, KURUCU, G1)
    yonetim.grup_onayla(conn, ui.saat, KURUCU, KURUCU, G2)
    # onaysız gruba seçim yapılamaz
    r = run(ui.buton(dugme(f"gs:{G3}")))
    assert r.sessiz and r.uyari_goster


def test_yetki_komutlari(ui, conn):
    assert "usage" in run(ui.komut(grup_komut("yetki_ver", "-5"))).metin.lower()
    assert "/grant" in run(ui.komut(grup_komut("grant", "-5"))).metin  # English menu name = same handler
    assert "Access granted" in run(ui.komut(grup_komut("yetki_ver", "300"))).metin
    assert "already authorized" in run(ui.komut(grup_komut("grant", "300"))).metin
    assert 300 in yonetim.durum_yukle(conn, KURUCU).yetkililer
    y = run(ui.komut(grup_komut("yetki_al", "300")))
    kid = onay_kimligi(y)
    assert 300 in yonetim.durum_yukle(conn, KURUCU).yetkililer, "onaydan önce kaldırılmamalı"
    assert "Access revoked" in run(ui.buton(grup_dugme(f"c:e:{kid}"))).metin
    assert 300 not in yonetim.durum_yukle(conn, KURUCU).yetkililer
    # kurucunun yetkisi alınamaz
    assert "cannot be revoked" in run(ui.komut(grup_komut("yetki_al", str(KURUCU), kul=YETKILI))).metin
    # listede olmayan
    assert "not in the authorized list" in run(ui.komut(grup_komut("yetki_al", "9999"))).metin


def test_grup_onayla_ve_kaldir(ui, conn):
    assert "Usage" in run(ui.komut(komut("grup_onayla", "123"))).metin  # pozitif kimlik grup değildir
    assert "Group approved" in run(ui.komut(komut("approve_group", str(G3)))).metin
    assert G3 in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    assert "already approved" in run(ui.komut(komut("grup_onayla", str(G3)))).metin
    kid = onay_kimligi(run(ui.komut(komut("remove_group", str(G3)))))
    assert G3 in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    assert "approval removed" in run(ui.buton(dugme(f"c:e:{kid}"))).metin
    assert G3 not in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    # kaldırılan grup artık işlem yapamaz
    assert run(ui.komut(grup_komut("yardim", grup=G3))) is None


def test_yonetim_ekrani_grup_listesi_yalniz_ozelde(ui):
    ozel = run(ui.buton(dugme("yo")))
    assert f"gk:{G1}" in veriler(ozel) and "tk" in veriler(ozel)
    grup = run(ui.buton(grup_dugme("yo")))
    assert not any(v.startswith("gk:") for v in veriler(grup)) and "tk" not in veriler(grup)
    assert f"yk:{YETKILI}" in veriler(grup)
    # grup bağlamında gk düğmesi elle üretilse de reddedilir
    assert run(ui.buton(grup_dugme(f"gk:{G2}"))).uyari == ar.ESKI_DUGME


# ---------------------------------------------------------------- parti kaldırma (çekirdek onay akışı)
def _parti_kur(src, uyeler):
    for oid, ad in uyeler:
        src.oyuncu(oid, ad)
    src.parti("5830", "Parti X", uyeler)


def test_parti_kaldirma_onay_ve_tekrar_onay(ui, conn, src):
    _parti_kur(src, [("1", "A"), ("2", "B")])
    run(ui.komut(grup_komut("parti_ekle", "https://diplomacia.com.tr/party/5830")))
    assert takipte(conn, G1) == {"1", "2"}
    y = run(ui.komut(grup_komut("parti_sil", "5830")))
    assert "<b>2</b>" in y.metin and "Current member count: 2" in y.metin
    assert takipte(conn, G1) == {"1", "2"}, "önizleme hiçbir şey silmemeli"
    kid = onay_kimligi(y)
    # onay anında üyelik değişir
    _parti_kur(src, [("1", "A"), ("2", "B"), ("3", "C")])
    y2 = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert "confirm the updated summary again" in y2.metin and "Current member count: 3" in y2.metin
    assert takipte(conn, G1) == {"1", "2"}, "özet değişmişse silinmemeli"
    kid2 = onay_kimligi(y2)
    assert kid2 != kid
    y3 = run(ui.buton(grup_dugme(f"c:e:{kid2}")))
    assert "Party tracking removed" in y3.metin and takipte(conn, G1) == set()


def test_parti_kaldirma_dogrulanamazsa_silmez(ui, conn, src):
    _parti_kur(src, [("1", "A")])
    run(ui.komut(grup_komut("parti_ekle", "5830")))
    src.hatalar[("parti", "5830")] = SiteErisilemez("x")
    y = run(ui.komut(grup_komut("parti_sil", "5830")))
    assert "Could not remove party" in y.metin and "could not be verified" in y.metin
    assert not any(v.startswith("c:e:") for v in veriler(y)), "onay düğmesi sunulmamalı"
    assert takipte(conn, G1) == {"1"}


def test_parti_sil_butonu_baska_grubun_partisini_gormez(ui, conn, src):
    _parti_kur(src, [("1", "A")])
    run(ui.komut(grup_komut("parti_ekle", "5830", grup=G2)))
    y = run(ui.buton(grup_dugme("ps:5830")))  # G1 bağlamı
    assert "Could not remove party" in y.metin and "not tracked in this group" in y.metin
    assert takipte(conn, G2) == {"1"}


def test_parti_listesi_ve_detay(ui, src):
    _parti_kur(src, [("1", "A"), ("2", "B")])
    run(ui.komut(grup_komut("parti_ekle", "5830")))
    m = run(ui.komut(grup_komut("partiler"))).metin
    assert "2 members" in m and 'party/5830' in m
    d = run(ui.buton(grup_dugme("pd:5830")))
    assert "ps:5830" in veriler(d) and "Known member count: 2" in d.metin


# ---------------------------------------------------------------- savaşlar
def test_savas_listesi_sayfali_ve_guncellik(ui, src):
    src.savaslar = [SavasOzet(f"s{i}", f"Savaş {i}") for i in range(1, 21)]
    y = run(ui.komut(grup_komut("savaslar")))
    assert "total 20" in y.metin and "Data as of: 19.10.2026 15:00" in y.metin  # 12:00 UTC = 15:00 İstanbul
    # BİLİNÇLİ DEĞİŞİKLİK (sade liste): numarasız savaşta kimlik eki yok; satır sayısı sayfa boyutu kadar
    assert y.metin.count(". ⚔️ Savaş ") == 8 and "sl:1" in veriler(y)
    son = run(ui.buton(grup_dugme("sl:2")))
    assert son.metin.count(". ⚔️ Savaş ") == 4
    assert "sd:s17" in veriler(son)


def test_savas_listesi_alinamazsa_sifir_gostermez(ui, conn, src):
    src.savaslar = [SavasOzet("s1", "Bir")]
    run(ui.komut(grup_komut("savaslar")))
    src.hatalar[("savaslar", None)] = SiteErisilemez("x")
    y = run(ui.komut(grup_komut("savaslar")))
    assert "Current data could not be fetched" in y.metin and "the data was unreachable" in y.metin
    assert "Active wars" not in y.metin and "total 0" not in y.metin
    assert "Last successful war scan: none" in y.metin


def test_savas_detay_alanlar_ve_bilinmeyen_sifir_degildir(ui, src):
    src.savas_detay["s1"] = SavasDetay(
        "s1", "Büyük <Savaş>", "Aktif",
        (SavasTaraf("A Tarafı", 1234567), SavasTaraf("B Tarafı", None), SavasTaraf("C", 0)),
    )
    y = run(ui.buton(grup_dugme("sd:s1")))
    m = y.metin
    assert "Büyük &lt;Savaş&gt;" in m and "Status: ❔ Aktif" in m  # ham (tanınmayan) durum metni + ❔
    assert "A Tarafı — Power: 1,234,567" in m
    assert "B Tarafı — Power: no data" in m, "bilinmeyen güç sıfır gibi gösterilmemeli"
    assert "C — Power: 0" in m, "gerçek sıfır gösterilebilir"
    assert 'href="https://diplomacia.com.tr/wars"' in m
    assert "Data as of: 19.10.2026 15:00" in m
    assert "sd:s1" in veriler(y)  # yenile


def test_savas_detay_erisilemezse_tarihli_son_veri(ui, src, saat):
    src.savas_detay["s1"] = SavasDetay("s1", "Savaş", "Aktif", (SavasTaraf("A", 10),))
    run(ui.buton(grup_dugme("sd:s1")))  # 15:00'de önbelleğe alınır
    saat.ilerle(3600)
    src.hatalar[("savas", "s1")] = SiteErisilemez("x")
    y = run(ui.buton(grup_dugme("sd:s1")))
    assert "Current data could not be fetched" in y.metin and "is from 19.10.2026 15:00" in y.metin
    assert "Power: 10" in y.metin
    # hiç veri yoksa
    src.hatalar[("savas", "s2")] = SiteErisilemez("x")
    y2 = run(ui.buton(grup_dugme("sd:s2")))
    assert "Data unreachable" in y2.metin and "Power" not in y2.metin


# ---------------------------------------------------------------- durum
def test_durum_ekrani_alanlari_ve_sir_yok(ui, conn, src, saat):
    from dipbot.zaman import iso
    src.oyuncu("1", "A")
    run(ui.komut(grup_komut("oyuncu_ekle", "1")))
    y = run(ui.komut(grup_komut("durum"))).metin
    for parca in ("Bot Status", f"Group: <code>{G1}</code>", "Tracked Players: 1", "Tracked Parties: 0",
                  "Scan Interval: 10 minutes", "Last Successful Scan", "• Players: none yet",
                  "• Parties: none yet", "• Wars: none yet",
                  "Next Scheduled Scan: waiting for the first scan", "Scan in Progress: No",
                  "Pending Notifications: 0"):
        assert parca in y, parca
    # The fixed "connection configured / no scan yet" boilerplate and the pagination note were removed:
    # the connection line only appears when there is a real problem.
    assert "Diplomacia connection" not in y and "unpaginated" not in y
    for tip in ("oyuncular", "partiler", "savaslar"):
        conn.execute("INSERT INTO tarama_isleri(group_id, is_tipi, son_deneme, son_basari) VALUES(?,?,?,?)",
                     (G1, tip, iso(saat()), iso(saat())))
    conn.execute("UPDATE grup_ayarlari SET sonraki_tarama=? WHERE group_id=?", ("2026-10-19T12:10:00.000000Z", G1))
    y = run(ui.buton(grup_dugme("du"))).metin
    assert "• Players: 19.10.2026 15:00" in y and "Next Scheduled Scan: 19.10.2026 15:10" in y
    assert "• Parties: 19.10.2026 15:00" in y and "• Wars: 19.10.2026 15:00" in y
    assert "Diplomacia connection" not in y, "no problem -> no connection line"
    assert "token" not in y.lower()


def test_durum_token_duraklatildi_ve_devam_eden(conn, src, saat):
    class Z:
        token_duraklatildi = True

        def calisiyor_mu(self, g):
            return True

    ui2 = Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=Z())
    y = run(ui2.komut(grup_komut("durum"))).metin
    assert "Scan in Progress: Yes" in y and "the site rejected the token (401)" in y
    assert "Diplomacia connection: the site rejected the token (401); scans are paused" in y
    assert "paused (token)" in y


def test_durum_ozel_sohbette_genel(ui):
    y = run(ui.komut(komut("durum"))).metin
    assert "general status" in y and "Approved groups: 2; authorized users: 1" in y


# ---------------------------------------------------------------- my_chat_member / migrate
def test_onaysiz_gruba_eklenme_tespit_ve_bildirim(ui, conn):
    s = ui.bot_uyelik_degisti(G3, "supergroup", "left", "member", YABANCI)
    assert s == "onaysiz_ekleme"
    b = bildirimler(conn, "yetkisiz_deneme", hedef=KURUCU)
    assert len(b) == 1
    assert b[0]["veri"] == {**b[0]["veri"], "grup_id": G3, "kullanici_id": YABANCI, "islem": "onaysiz_gruba_eklendi"}
    # aynı grup için tekrar bildirim yağmuru yok
    ui.bot_uyelik_degisti(G3, "supergroup", "kicked", "administrator", 88)
    assert len(bildirimler(conn, "yetkisiz_deneme")) == 1
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE islem='onaysiz_gruba_eklendi'").fetchone()[0] == 2


def test_my_chat_member_diger_durumlar(ui, conn):
    assert ui.bot_uyelik_degisti(G1, "supergroup", "left", "member", YABANCI) == "onayli_ekleme"
    assert ui.bot_uyelik_degisti(G3, "supergroup", "member", "left", YABANCI) == "diger"  # çıkarıldı
    assert ui.bot_uyelik_degisti(G3, "supergroup", "member", "administrator", YABANCI) == "diger"  # terfi
    assert ui.bot_uyelik_degisti(5, "private", "kicked", "member", 5) == "diger"
    assert ui.bot_uyelik_degisti(-77, "channel", "left", "administrator", 5) == "diger"
    assert not bildirimler(conn, "yetkisiz_deneme")


def test_grup_goc_kaydi_sessizce_yetkili_saymaz(ui, conn):
    yeni = -1009999
    # C3: göç öncesi eski grupta bekleyen bildirim var
    from dipbot import outbox
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="x1",
                        veri={"savas_id": "s"}, simdi=ui.saat())
    assert ui.grup_goc(G1, yeni) is True
    # eski grubun onayı düştü: tarama/gönderim durur, bekleyen iptal, veri silinmez
    assert G1 not in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", (G1,)).fetchone()[0] == 0
    assert conn.execute("SELECT durum FROM outbox WHERE idem_anahtari='x1'").fetchone()[0] == "iptal"
    assert run(ui.komut(grup_komut("oyuncular", grup=G1))) is None
    from dipbot.takip import TakipServisi
    from fakes import SahteKaynak
    sv = TakipServisi(conn, ui.saat)
    k = SahteKaynak()
    run(sv.savaslari_tara(G1, k))  # onay yok: baseline/kayıt yazılmaz
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 0
    r = conn.execute("SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()
    assert r is None or r[0] == 0
    assert ui.grup_goc(G1, yeni) is False  # aynı göç için ikinci bildirim yok
    b = bildirimler(conn, "grup_goc", hedef=KURUCU)
    assert len(b) == 1 and b[0]["veri"]["eski"] == G1 and b[0]["veri"]["yeni"] == yeni
    # yeni kimlik ONAYSIZ kalır: hiçbir komut çalışmaz
    d = yonetim.durum_yukle(conn, KURUCU)
    assert yeni not in d.onayli_gruplar
    assert run(ui.komut(grup_komut("oyuncular", grup=yeni))) is None
    # onaysız grubun göçü bildirim üretmez
    assert ui.grup_goc(G3, -10088) is False


# ---------------------------------------------------------------- token yenileme (arayüz akışı)
class SahteZamanlayici:
    token_duraklatildi = False

    def __init__(self):
        self.devam = 0

    def token_devam_ettir(self):
        self.devam += 1
        self.token_duraklatildi = False

    def calisiyor_mu(self, g):
        return False


class SahteDogrulayici:
    def __init__(self, durum="gecerli", hata=None):
        self.durum, self.hata, self.alinan = durum, hata, []

    async def dogrula(self, token):
        self.alinan.append(token)
        if self.hata:
            raise self.hata
        from dipbot.tg.token import DogrulamaSonucu
        return DogrulamaSonucu(self.durum, "sahte")


YENI_TOKEN = "eyJYENI01.TOKEN02.GIZLI03"


def _token_ui(conn, src, saat, tmp_path, dogrulayici=None):
    from dipbot.tg.token import TokenDeposu
    depo = TokenDeposu(tmp_path / "diplomacia.token")
    depo.yaz("eski-token-XYZ")
    z = SahteZamanlayici()
    z.token_duraklatildi = True
    kw = {} if dogrulayici is None else {"dogrulayici": dogrulayici}
    return Arayuz(conn, src, KURUCU, saat=saat, zamanlayici=z, token_deposu=depo, **kw), depo, z


def _token_mesaji(ui, metin=YENI_TOKEN, silinen=None):
    async def sil():
        if silinen is not None:
            silinen.append(1)
        return True
    return run(ui.metin(Olay(sohbet_id=KURUCU, sohbet_turu="private", kullanici_id=KURUCU, metin=metin, mesaj_id=7, sil=sil)))


def test_token_yenileme_dogrulayici_yoksa_reddedilir(conn, src, saat, tmp_path):
    ui, depo, z = _token_ui(conn, src, saat, tmp_path)  # varsayılan doğrulayıcı: yapılandırılmadı
    menu = run(ui.buton(dugme("tk")))
    assert "Token validator: not configured" in menu.metin and "eski-token" not in menu.metin
    run(ui.buton(dugme("tn")))
    silinen = []
    y = _token_mesaji(ui, silinen=silinen)
    assert silinen, "token mesajı silinmeye çalışılmalı"
    assert "not configured" in y.metin and "NOT CHANGED" in y.metin
    assert YENI_TOKEN not in y.metin
    assert depo.oku() == "eski-token-XYZ"
    assert z.devam == 0 and z.token_duraklatildi, "sahte başarı yok: tarama devam ettirilmemeli"


def test_token_yenileme_gecerli_dogrulayici_akisi(conn, src, saat, tmp_path):
    d = SahteDogrulayici("gecerli")
    ui, depo, z = _token_ui(conn, src, saat, tmp_path, d)
    run(ui.buton(dugme("tn")))
    y = _token_mesaji(ui)
    assert d.alinan == [YENI_TOKEN]
    assert "player request with the candidate token succeeded; saved" in y.metin and "resuming" in y.metin
    assert "has not been verified separately" in y.metin and "was verified" not in y.metin
    assert YENI_TOKEN not in y.metin
    assert depo.oku() == YENI_TOKEN and z.devam == 1
    # tek kullanımlık bekleme: sonraki düz mesaj token sayılmaz
    assert _token_mesaji(ui, "merhaba") is None
    # token veritabanına yazılmadı
    for (ad,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for satir in conn.execute(f"SELECT * FROM {ad}"):
            assert YENI_TOKEN not in str(tuple(satir))


def test_token_gecersizse_degismez(conn, src, saat, tmp_path):
    ui, depo, z = _token_ui(conn, src, saat, tmp_path, SahteDogrulayici("gecersiz"))
    run(ui.buton(dugme("tn")))
    y = _token_mesaji(ui)
    assert "rejected" in y.metin.lower() and "NOT CHANGED" in y.metin
    assert depo.oku() == "eski-token-XYZ" and z.devam == 0


def test_dogrulayici_hatasi_token_sizdirmaz(conn, src, saat, tmp_path):
    ui, depo, z = _token_ui(conn, src, saat, tmp_path, SahteDogrulayici(hata=RuntimeError("boom " + YENI_TOKEN)))
    run(ui.buton(dugme("tn")))
    y = _token_mesaji(ui)
    assert YENI_TOKEN not in y.metin and "RuntimeError" in y.metin and "NOT CHANGED" in y.metin
    assert depo.oku() == "eski-token-XYZ"


def test_token_menusu_yalniz_kurucu_ozel_sohbet(conn, src, saat, tmp_path):
    ui, depo, z = _token_ui(conn, src, saat, tmp_path, SahteDogrulayici())
    yonetim.yetki_ver(conn, saat, KURUCU, KURUCU, YETKILI)
    # kurucu bile grupta token menüsünü kullanamaz
    for veri in ("tk", "tn", "tc"):
        y = run(ui.buton(grup_dugme(veri)))
        assert y.sessiz and y.uyari_goster
    # yetkili kullanıcı özelde kullanamaz (zaten özel sohbet kapalı)
    y = run(ui.buton(dugme("tn", kul=YETKILI)))
    assert y.sessiz and y.uyari_goster
    assert ui._token_bekleyen == {}
    assert len(bildirimler(conn, "yetkisiz_deneme")) >= 1


def test_token_bekleme_suresi_dolar_ve_grup_mesaji_yoksayilir(conn, src, saat, tmp_path):
    d = SahteDogrulayici()
    ui, depo, z = _token_ui(conn, src, saat, tmp_path, d)
    run(ui.buton(dugme("tn")))
    saat.ilerle(6 * 60)
    y = _token_mesaji(ui)
    assert "has expired" in y.metin and d.alinan == [] and depo.oku() == "eski-token-XYZ"
    # grup sohbetindeki metin asla token olarak işlenmez
    run(ui.buton(dugme("tn")))
    g = run(ui.metin(Olay(sohbet_id=G1, sohbet_turu="supergroup", kullanici_id=KURUCU, metin=YENI_TOKEN)))
    assert g is None and d.alinan == []


def test_token_iptal(conn, src, saat, tmp_path):
    d = SahteDogrulayici()
    ui, depo, z = _token_ui(conn, src, saat, tmp_path, d)
    run(ui.buton(dugme("tn")))
    assert "cancelled" in run(ui.buton(dugme("tc"))).metin
    _token_mesaji(ui)  # bekleme yok: JWT biçimli mesaj işlenmez (E9: artık uyarı yanıtı dönebilir)
    assert d.alinan == [] and depo.oku() == "eski-token-XYZ"
