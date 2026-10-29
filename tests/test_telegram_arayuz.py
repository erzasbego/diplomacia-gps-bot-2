"""Telegram arayüz mantığı (Arayuz): yetki matrisi, butonlar, onay akışları, ekranlar.

MOCK testler: sahte kaynak, bellek içi SQLite, ağ yok. Canlı Telegram doğrulaması DEĞİLDİR.
"""
import pytest

from conftest import bildirimler, run, takipte
from tg_fakes import (
    G1, G2, G3, KURUCU, YABANCI, YETKILI,
    dugme, grup_dugme, grup_komut, gercek_grup_dugme, gercek_grup_komut, komut, onay_kimligi, veriler,
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
# BİLİNÇLİ TASARIM DEĞİŞİKLİĞİ: Telegram GRUPLARI artık HİÇBİR komut/düğme işlemez (kurucu, yetkili, yabancı; onaylı
# ya da onaysız grup fark etmez): yanıt yok, yetkisiz-deneme kaydı yok, kurucuya bildirim yok. Yönetim yalnız
# kurucunun ÖZEL sohbetinde yapılır. Eski "grupta yetkili kullanıcı işlem yapar / yabancı reddedilir + kayıt"
# matrisi bu yüzden yeniden yazıldı: özel sohbet matrisi AYNI, grup sütunları "sessizce yok sayılır".
KISILER = {"kurucu": KURUCU, "yetkili": YETKILI, "yetkisiz": YABANCI}
SOHBETLER = ["ozel", "onayli_grup", "onaysiz_grup"]
# (kişi, sohbet) -> işlem yapabilir mi? (gruplarda hiç kimse)
BEKLENEN = {
    ("kurucu", "ozel"): True, ("yetkili", "ozel"): False, ("yetkisiz", "ozel"): False,
    ("kurucu", "onayli_grup"): False, ("yetkili", "onayli_grup"): False, ("yetkisiz", "onayli_grup"): False,
    ("kurucu", "onaysiz_grup"): False, ("yetkili", "onaysiz_grup"): False, ("yetkisiz", "onaysiz_grup"): False,
}


def _sohbet_args(kisi_id, sohbet):
    if sohbet == "ozel":
        return dict(sohbet=kisi_id, tur="private", kul=kisi_id)
    return dict(sohbet=G1 if sohbet == "onayli_grup" else G3, tur="supergroup", kul=kisi_id)


def _hic_iz_yok(conn):
    """Grup olayları: denetim kaydı YOK, outbox (kurucu/kanal bildirimi) YOK."""
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("sohbet", SOHBETLER)
@pytest.mark.parametrize("kisi", list(KISILER))
def test_yetki_matrisi_komut(ui, conn, kisi, sohbet):
    y = run(ui.komut(komut("yardim", **_sohbet_args(KISILER[kisi], sohbet))))
    izinli = BEKLENEN[(kisi, sohbet)]
    if izinli:
        assert y is not None and "Commands" in y.metin
        _hic_iz_yok(conn)
    elif sohbet == "ozel":
        assert y is not None and y.metin == ar.KAPALI_OZEL and "Commands" not in y.metin
        n = conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE user_id=?", (KISILER[kisi],)).fetchone()[0]
        assert n == 1, "yetkisiz özel sohbet denemesi kaydedilmeli"
    else:
        assert y is None, "gruplar komutu sessizce yok sayar (kurucu dahil)"
        _hic_iz_yok(conn)


@pytest.mark.parametrize("sohbet", SOHBETLER)
@pytest.mark.parametrize("kisi", list(KISILER))
def test_yetki_matrisi_dugme(ui, conn, kisi, sohbet):
    y = run(ui.buton(dugme("m", **_sohbet_args(KISILER[kisi], sohbet))))
    izinli = BEKLENEN[(kisi, sohbet)]
    if izinli:
        assert y.metin and y.duzenle and not y.sessiz
        _hic_iz_yok(conn)
    elif sohbet == "ozel":
        # reddedilen düğme menüyü AÇMAZ; açılır uyarı döner (answerCallbackQuery için)
        assert y.sessiz and y.uyari and y.uyari_goster and not y.metin
        n = conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE user_id=?", (KISILER[kisi],)).fetchone()[0]
        assert n == 1
    else:
        # grup: tamamen sessiz (ne mesaj, ne açılır uyarı, ne düzenleme) ve iz yok
        assert y.sessiz and not y.metin and not y.uyari and not y.uyari_goster and not y.klavye and not y.duzenle
        _hic_iz_yok(conn)


GRUP_KOMUTLARI = [("yardim", ""), ("start", ""), ("oyuncular", ""), ("durum", ""), ("tarama", "15"),
                  ("oyuncuekle", "72400"), ("addplayer", "72400"), ("grant", "300"), ("approve_group", str(G3)),
                  ("remove_group", str(G1)), ("scan", "15"), ("move_group", f"{G1} {G2}")]
GRUP_DUGMELERI = ["m", "yo", "ol:0", "pl:0", "sl:0", "ta", "du", "gl", f"gs:{G1}", f"gs:{G3}", f"gk:{G1}", "tk", "tn",
                  "tv:20", "c:e:yokboyle", "zzz:1"]


def _durum_ozeti(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM takip_oyuncular").fetchone()[0],
        tuple(sorted(yonetim.durum_yukle(conn, KURUCU).onayli_gruplar)),
        tuple(sorted(yonetim.durum_yukle(conn, KURUCU).yetkililer)),
        yonetim.tarama_araligi(conn, G1),
    )


@pytest.mark.parametrize("grup", [G1, G2, G3], ids=["onayli_G1", "onayli_G2", "onaysiz_G3"])
@pytest.mark.parametrize("kisi", list(KISILER))
def test_gruplar_komut_ve_dugmeyi_sessizce_yoksayar(ui, conn, src, kisi, grup):
    """BİLİNÇLİ DEĞİŞİKLİK: grup sohbetleri (onaylı/onaysız; kurucu/yetkili/yabancı) için TÜM komutlar None, TÜM
    düğmeler sessiz Yanit döner; yetkisiz_deneme satırı, outbox satırı, kaynak çağrısı ve durum değişikliği YOK."""
    src.oyuncu("72400", "Ali")
    once = _durum_ozeti(conn)
    for ad, arg in GRUP_KOMUTLARI:
        assert run(ui.komut(gercek_grup_komut(ad, arg, grup=grup, kul=KISILER[kisi]))) is None, ad
    for veri in GRUP_DUGMELERI:
        y = run(ui.buton(gercek_grup_dugme(veri, grup=grup, kul=KISILER[kisi])))
        assert y.sessiz and not y.metin and not y.uyari and not y.uyari_goster and not y.klavye, veri
    _hic_iz_yok(conn)
    assert src.cagrilar == []
    assert _durum_ozeti(conn) == once
    assert ui._token_bekleyen == {} and ui.kapsam == config.MERKEZ_ID
    assert not hasattr(ui, "_secili") and not hasattr(ui, "_grup_secimi")  # grup/liste seçimi mimaride yok


def test_grup_olayi_komut_dugme_yardimcilari_yalniz_kurucuyu_ozele_cevirir(ui):
    """Yardımcı sözleşmesi: kurucu için özel sohbet+liste seçimi; başkaları için GERÇEK grup olayı."""
    assert grup_komut("yardim").sohbet_turu == "private" and grup_komut("yardim").liste == G1
    assert grup_dugme("m", grup=G2).liste == G2
    for kul in (YETKILI, YABANCI):
        assert grup_komut("yardim", kul=kul).sohbet_turu == "supergroup" and grup_dugme("m", kul=kul).grup_mu


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


def test_onayli_grupta_yetkisiz_kullanici_sessizce_yoksayilir_kayit_yok(ui, conn):
    """BİLİNÇLİ DEĞİŞİKLİK: eskiden onaylı grupta yetkisiz kullanıcı 'not authorized' yanıtı + denetim kaydı alırdı;
    artık gruplar sessizdir (yanıt yok, kayıt yok, kurucuya bildirim yok)."""
    assert run(ui.komut(grup_komut("oyuncular", kul=YABANCI))) is None
    y = run(ui.buton(grup_dugme("ol:0", kul=YABANCI)))
    assert y.sessiz and not y.metin and not y.uyari and not y.uyari_goster
    _hic_iz_yok(conn)


def test_onaysiz_grup_sessiz_ve_kayitsiz(ui, conn):
    """BİLİNÇLİ DEĞİŞİKLİK: onaysız grup da sessiz; eskiden kurucuya 'yetkisiz deneme' kaydı/bildirimi giderdi."""
    for kul in (KURUCU, YETKILI, YABANCI):
        assert run(ui.komut(gercek_grup_komut("oyuncular", grup=G3, kul=kul))) is None
        y = run(ui.buton(gercek_grup_dugme("ol:0", grup=G3, kul=kul)))
        assert y.sessiz and not y.metin and not y.uyari_goster
    _hic_iz_yok(conn)
    # kurucunun özel sohbetinde onaysız listeyi seçmek de işe yaramaz (seçim doğrulanır)
    r = run(ui.buton(grup_dugme(f"gs:{G3}")))
    assert r.sessiz and r.uyari_goster and not r.metin


def test_yetkisi_alinan_kullanicinin_eski_butonu_calismaz(ui, conn, saat):
    """Yetkili kullanıcı artık ne grupta ne özelde işlem yapabilir; yetkisi alınınca eski düğmesi de (aynı şekilde)
    çalışmaz. Grupta sessiz yok sayılır (önce/sonra AYNI), özelde kapalı-özel uyarısı + denetim kaydı."""
    eski_dugme = "ol:0"
    for _ in range(2):  # yetki alınmadan önce ve sonra
        y = run(ui.buton(grup_dugme(eski_dugme, kul=YETKILI)))
        assert y.sessiz and not y.metin and not y.uyari and not y.uyari_goster
        assert run(ui.komut(grup_komut("oyuncular", kul=YETKILI))) is None
        yonetim.yetki_al(conn, saat, KURUCU, KURUCU, YETKILI)
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0
    p = run(ui.buton(dugme(eski_dugme, kul=YETKILI)))
    assert p.sessiz and p.uyari_goster and not p.metin
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE user_id=?", (YETKILI,)).fetchone()[0] == 1


def test_yetkisi_alinan_kullanicinin_bekleyen_onayi_gecersiz(ui, conn, saat, src):
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("oyuncu_ekle", "72400")))
    # yetkili adına açılmış bekleyen bir silme onayı (eski tasarımdan kalma/sahte); yetkisi alınınca geçersizdir
    kid = ui.islemler.olustur("oyuncu_sil", G1, YETKILI, G1, {"id": "72400"})
    yonetim.yetki_al(conn, saat, KURUCU, KURUCU, YETKILI)
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))  # grup: sessiz
    assert r.sessiz and not r.metin and not r.uyari_goster
    r = run(ui.buton(dugme(f"c:e:{kid}", kul=YETKILI)))  # özel: kapalı
    assert r.sessiz and r.uyari_goster and not r.metin
    assert "72400" in takipte(conn, G1), "silme gerçekleşmemeli"


def test_baska_sohbete_ait_islem_kimligi_reddedilir(ui, conn, saat, src):
    src.oyuncu("72400", "Ali")
    run(ui.komut(grup_komut("oyuncu_ekle", "72400")))
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "72400"))))
    # Gerçek bir grup sohbetinden (kurucu bile) kullanılamaz: gruplar sessizce yok sayılır, işlem korunur
    r = run(ui.buton(gercek_grup_dugme(f"c:e:{kid}", grup=G2)))
    assert r.sessiz and not r.metin and not r.uyari_goster
    assert "72400" in takipte(conn, G1)
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler").fetchone()[0] == 0
    # başka (özel) sohbete ait işlem kimliği kurucunun özel sohbetinde de reddedilir + denetim kaydı
    yabanci_op = ui.islemler.olustur("oyuncu_sil", 4242, KURUCU, G1, {"id": "72400"})
    r = run(ui.buton(grup_dugme(f"c:e:{yabanci_op}")))
    assert r.sessiz and "another chat/group" in r.uyari
    assert "72400" in takipte(conn, G1)
    assert any(x["veri"]["islem"] == "buton:baska_sohbet_islemi" for x in bildirimler(conn, "yetkisiz_deneme"))
    # İşlem hâlâ kendi sohbetinde geçerli
    ok = run(ui.buton(grup_dugme(f"c:e:{kid}")))
    assert "removed from tracking" in ok.metin and "72400" not in takipte(conn, G1)


def test_baska_gruba_ait_hedefli_islem_reddedilir(ui, conn):
    # sohbet G2'de başlatılmış ama hedef grubu G1 olan sahte/bozuk işlem
    kid = ui.islemler.olustur("oyuncu_sil", G2, KURUCU, G1, {"id": "1"})
    # grup sohbetinden: sessizce yok sayılır (işlem bozulmaz)
    r = run(ui.buton(gercek_grup_dugme(f"c:e:{kid}", grup=G2)))
    assert r.sessiz and not r.metin and not r.uyari_goster
    assert ui.islemler.getir(kid) is not None
    # kurucunun özel sohbetinden: işlem başka sohbete ait
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", grup=G2)))
    assert r.sessiz and "another chat/group" in r.uyari
    assert ui.islemler.getir(kid) is not None


def test_baska_gruptaki_oyuncu_gorunmez_ve_silinemez(ui, conn, src):
    src.oyuncu("1", "Gizli")
    run(ui.komut(grup_komut("oyuncu_ekle", "1", grup=G2)))
    # G1 bağlamında G2'nin oyuncusu yok
    assert run(ui.buton(grup_dugme("od:1"))).uyari_goster
    y = run(ui.komut(grup_komut("oyuncu_sil", "1")))
    assert "This player is not tracked." in y.metin and not y.klavye
    assert "1" in takipte(conn, G2)


def test_onay_baska_kullanici_tarafindan_verilemez(ui, conn, src):
    src.oyuncu("5", "Bir")
    run(ui.komut(grup_komut("oyuncu_ekle", "5")))
    kid = onay_kimligi(run(ui.komut(grup_komut("oyuncu_sil", "5"))))
    # yetkili kullanıcı: grupta sessiz yok sayılır, özelde kapalı; işlem gerçekleşmez
    r = run(ui.buton(grup_dugme(f"c:e:{kid}", kul=YETKILI)))
    assert r.sessiz and not r.metin and not r.uyari_goster
    r = run(ui.buton(dugme(f"c:e:{kid}", kul=YETKILI)))
    assert r.sessiz and r.uyari_goster and not r.metin
    assert "5" in takipte(conn, G1)
    # kurucu kendi sohbetinde ama başka kullanıcı adına açılmış işlemi onaylayamaz
    baskasi = ui.islemler.olustur("oyuncu_sil", KURUCU, YETKILI, G1, {"id": "5"})
    r = run(ui.buton(grup_dugme(f"c:e:{baskasi}")))
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
        "scan", "status", "help", "start",
        "channel", "lists",  # notification-channel re-verification and central tracking-list overview (owner only)
    }
    assert len(KOMUTLAR) == 13
    assert istenen == set(KOMUTLAR)
    # DELIBERATE CHANGE (single central area): the group-administration commands are no longer offered at all
    assert not {"grant", "revoke", "approve_group", "remove_group", "move_group"} & set(KOMUTLAR)
    assert not {"grant", "revoke", "approve_group", "remove_group", "move_group"} & set(ar.EN_KOMUTLAR)
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


def test_ozel_sohbette_grup_secimi_yok_tek_merkezi_alan(ui, conn, src):
    """DELIBERATE CHANGE: there is no group/list selection any more; the owner's private chat always works on the
    single central area (config.MERKEZ_ID). Legacy 'gs:' buttons are dead and never create a selection."""
    src.oyuncu("1", "A")
    y = run(ui.komut(komut("oyuncular")))  # no selection prompt: straight to the (empty) central list
    assert "Select the tracking list" not in y.metin and "No players are tracked yet." in y.metin
    assert not any(v.startswith("gs:") for v in veriler(y))
    for g in (G1, G3):
        r = run(ui.buton(dugme(f"gs:{g}")))
        assert r.sessiz and r.uyari_goster and r.uyari == ar.ESKI_DUGME and not r.metin
    assert ui.kapsam == config.MERKEZ_ID
    run(ui.komut(komut("oyuncu_ekle", "1")))
    assert takipte(conn, config.MERKEZ_ID) == {"1"} and takipte(conn, G1) == set() and takipte(conn, G2) == set()
    assert ui._grup(komut("oyuncular")) == config.MERKEZ_ID
    # approving / removing old groups at the service layer does not change the working area
    yonetim.grup_kaldir(conn, ui.saat, KURUCU, KURUCU, G1)
    assert ui._grup(komut("oyuncular")) == config.MERKEZ_ID
    assert "1" in run(ui.komut(komut("oyuncular"))).metin


def test_yetki_komutlari(ui, conn):
    """DELIBERATE CHANGE: /grant and /revoke (and their Turkish aliases) were removed from Telegram; they are not
    dispatched at all (None), neither in the owner's private chat nor in groups, and change no authorization data.
    The service layer (yonetim.yetki_ver / yetki_al) is still tested directly."""
    assert "grant" not in KOMUTLAR and "revoke" not in KOMUTLAR
    for ad in ("yetki_ver", "grant", "yetki_al", "revoke"):
        assert ar.komut_ic_adi(ad) is None, ad
        assert run(ui.komut(grup_komut(ad, "300"))) is None, ad
        assert run(ui.komut(komut(ad, "300"))) is None, ad
        assert run(ui.komut(komut(ad, "301", kul=YETKILI))) is None, ad
        assert run(ui.komut(gercek_grup_komut(ad, "300"))) is None, ad
    assert 300 not in yonetim.durum_yukle(conn, KURUCU).yetkililer
    # service layer still works
    assert yonetim.yetki_ver(conn, ui.saat, KURUCU, KURUCU, 300) is True
    assert 300 in yonetim.durum_yukle(conn, KURUCU).yetkililer
    # ...but granting never changes runtime authorization: 300 is still not allowed in the private chat
    assert run(ui.komut(komut("yardim", kul=300))).metin == ar.KAPALI_OZEL


def test_grup_onayla_ve_kaldir(ui, conn):
    """DELIBERATE CHANGE: /approve_group and /remove_group (and the 'gk:' / 'yk:' buttons) were removed; they are not
    dispatched and approve/remove nothing. The service layer (yonetim.grup_onayla / grup_kaldir) is checked directly."""
    assert "approve_group" not in KOMUTLAR and "remove_group" not in KOMUTLAR
    for ad, arg in (("grup_onayla", str(G3)), ("approve_group", str(G3)), ("grup_kaldir", str(G1)),
                    ("remove_group", str(G1)), ("grup_tasi", f"{G1} {G2}"), ("move_group", f"{G1} {G2}")):
        assert ar.komut_ic_adi(ad) is None, ad
        assert run(ui.komut(komut(ad, arg))) is None, ad
        assert run(ui.komut(gercek_grup_komut(ad, arg, grup=G3))) is None, ad
    for veri in (f"gk:{G1}", f"yk:{YETKILI}", f"gs:{G1}", "gl"):
        y = run(ui.buton(dugme(veri)))
        assert y.sessiz and y.uyari_goster and y.uyari == ar.ESKI_DUGME and not y.metin and not y.klavye, veri
    d = yonetim.durum_yukle(conn, KURUCU)
    assert G3 not in d.onayli_gruplar and {G1, G2} <= d.onayli_gruplar
    # service layer: unchanged archive/library behaviour
    assert yonetim.grup_onayla(conn, ui.saat, KURUCU, KURUCU, G3) is True
    assert G3 in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    assert yonetim.grup_kaldir(conn, ui.saat, KURUCU, KURUCU, G3) == 0
    assert G3 not in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    # a group chat stays silent regardless
    assert run(ui.komut(gercek_grup_komut("yardim", grup=G3))) is None


def test_yonetim_ekrani_grup_listesi_yalniz_ozelde(ui):
    """DELIBERATE CHANGE: the administration screen has only an info text, 'Renew Token' and 'Main menu' (no
    authorized-user / approved-group / revoke / remove-approval buttons); it still opens only in the owner's private chat."""
    ozel = run(ui.buton(dugme("yo")))
    assert "Administration" in ozel.metin and veriler(ozel) == ["tk", "m"]
    assert not any(v.startswith(("gk:", "yk:", "gs:")) for v in veriler(ozel))
    assert "Approved groups" not in ozel.metin and "Authorized" not in ozel.metin
    # in a group the administration screen never opens (even for the owner); a hand-made 'gk' is silently ignored
    grup = run(ui.buton(gercek_grup_dugme("yo")))
    assert grup.sessiz and not grup.metin and not grup.klavye and not veriler(grup)
    g = run(ui.buton(gercek_grup_dugme(f"gk:{G2}")))
    assert g.sessiz and not g.metin and not g.uyari and not g.uyari_goster
    assert G2 in yonetim.durum_yukle(ui.conn, KURUCU).onayli_gruplar
    # in the owner's private chat the removed 'gk:' button is a dead button and removes nothing
    k = run(ui.buton(dugme(f"gk:{G2}")))
    assert k.sessiz and k.uyari == ar.ESKI_DUGME and G2 in yonetim.durum_yukle(ui.conn, KURUCU).onayli_gruplar


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
    for parca in ("Bot Status", "Tracked Players: 1", "Tracked Parties: 0",
                  "Scan Interval: 10 minutes", "Last Successful Scan", "• Players: none yet",
                  "• Parties: none yet", "• Wars: none yet",
                  "Next Scheduled Scan: waiting for the first scan", "Scan in Progress: No",
                  "Pending Notifications: 0"):
        assert parca in y, parca
    # The fixed "connection configured / no scan yet" boilerplate and the pagination note were removed:
    # the connection line only appears when there is a real problem.
    assert "Diplomacia connection" not in y and "unpaginated" not in y
    # DELIBERATE CHANGE: no group line / approved-groups / tracking-list-status lines in /status
    assert "Group:" not in y and "Approved groups" not in y and "Tracking list status" not in y
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
    # DELIBERATE CHANGE: no 'general status' / approved-groups / authorized-users lines any more (single central area)
    assert "Bot Status" in y and "Tracked Players: 0" in y and "Scan Interval: 10 minutes" in y
    assert "general status" not in y and "Approved groups" not in y and "authorized users" not in y
    assert "Group:" not in y and "Tracking list status" not in y


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
    # DELIBERATE CHANGE: there is no 'onayli_ekleme' any more; every group/supergroup addition (even to a once-approved
    # group id) is 'onaysiz_ekleme' and is recorded for the owner.
    assert ui.bot_uyelik_degisti(G1, "supergroup", "left", "member", YABANCI) == "onaysiz_ekleme"
    assert len(bildirimler(conn, "yetkisiz_deneme")) == 1
    assert conn.execute("SELECT COUNT(*) FROM yetkisiz_denemeler WHERE group_id=? AND islem='onaysiz_gruba_eklendi'",
                        (G1,)).fetchone()[0] == 1
    assert ui.bot_uyelik_degisti(G3, "supergroup", "member", "left", YABANCI) == "diger"  # çıkarıldı
    assert ui.bot_uyelik_degisti(G3, "supergroup", "member", "administrator", YABANCI) == "diger"  # terfi
    assert ui.bot_uyelik_degisti(5, "private", "kicked", "member", 5) == "diger"
    assert ui.bot_uyelik_degisti(-77, "channel", "left", "administrator", 5) == "diger"
    assert len(bildirimler(conn, "yetkisiz_deneme")) == 1  # only the G1 addition above; the rest add no notification


def test_grup_goc_kaydi_sessizce_yetkili_saymaz(ui, conn):
    """DELIBERATE CHANGE: Arayuz.grup_goc (migrate handling) was removed; the UI no longer reacts to group migration.
    The service functions (goc_kaydet / grup_kaldir_sistem) still work and still never authorize the new id."""
    yeni = -1009999
    assert not hasattr(ui, "grup_goc")
    # old group has a pending notification (service-layer data, only reachable in tests)
    from dipbot import outbox
    outbox.kuyruga_ekle(conn, hedef_sohbet=G1, grup_id=G1, tur="yeni_savas", idem_anahtari="x1",
                        veri={"savas_id": "s"}, simdi=ui.saat())
    assert yonetim.goc_kaydet(conn, ui.saat, G1, yeni) is True  # records only; grants/moves nothing
    assert yonetim.goc_kaydet(conn, ui.saat, G1, yeni) is False  # same pair: single record
    assert yeni not in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    assert not bildirimler(conn, "grup_goc")  # no 'grup_goc' notification is produced by the UI any more
    # system-side drop of the old approval (library code): scanning/sending stops, pending is cancelled, data kept
    yonetim.grup_kaldir_sistem(conn, ui.saat, KURUCU, G1, neden="grup_goc")
    assert G1 not in yonetim.durum_yukle(conn, KURUCU).onayli_gruplar
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE grup_id=? AND durum='bekliyor'", (G1,)).fetchone()[0] == 0
    assert conn.execute("SELECT durum FROM outbox WHERE idem_anahtari='x1'").fetchone()[0] == "iptal"
    assert run(ui.komut(gercek_grup_komut("oyuncular", grup=G1))) is None  # old group chat: silent
    from dipbot.takip import TakipServisi
    from fakes import SahteKaynak
    sv = TakipServisi(conn, ui.saat)
    k = SahteKaynak()
    run(sv.savaslari_tara(G1, k))  # no approval: no baseline/record written
    assert conn.execute("SELECT COUNT(*) FROM savaslar WHERE group_id=?", (G1,)).fetchone()[0] == 0
    r = conn.execute("SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (G1,)).fetchone()
    assert r is None or r[0] == 0
    # the new id stays unapproved: group chats stay silent, and the UI always works on the central area
    assert run(ui.komut(gercek_grup_komut("oyuncular", grup=yeni))) is None
    assert run(ui.komut(komut("oyuncular"))).metin  # private chat unaffected
    assert ui.kapsam == config.MERKEZ_ID
    # an unapproved group's migration record is not an error either
    assert yonetim.goc_kaydet(conn, ui.saat, G3, -10088) is True


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
    # kurucu bile grupta token menüsünü kullanamaz (gruplar sessizce yok sayılır: menü yok, uyarı yok, bekleme yok)
    for veri in ("tk", "tn", "tc"):
        y = run(ui.buton(gercek_grup_dugme(veri)))
        assert y.sessiz and not y.metin and not y.klavye and not y.uyari_goster
        assert ui._token_bekleyen == {}
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
