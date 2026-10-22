"""Tek kriter: XP >= 1.500.000. Sabit 100 oyuncu sınırı YOK; API'nin tek istekte döndürdüğü 100 üye bir OKUMA sınırıdır
(eksik okuma açıkça bildirilir, tam liste sayılmaz). Eşzamanlı çift üyelik varsayımı yok; eski/gecikmiş kayıtlar yanlış
bildirim üretmez. MOCK; gerçek sayfalama yöntemi DOĞRULANMADI."""
import copy
import json
import logging
import uuid
from pathlib import Path

import httpx
import pytest

from conftest import G1, G2, KURUCU, bildirimler, run, takipte
from tg_fakes import grup_komut

from dipbot import config
from dipbot.diplomacia import ayristir as a
from dipbot.kaynak import PartiAnlik, PartiUyesi
from dipbot.tg.arayuz import Arayuz
from fakes import SahteKaynak

FX = json.loads((Path(__file__).parent / "fixtures" / "diplomacia_ornek.json").read_text(encoding="utf-8"))
ESIK = 1_500_000


def test_esik_2_milyon_tek_kriter():
    assert config.PARTI_XP_ESIGI == ESIK
    assert not hasattr(config, "PARTI_TAKIP_UST_SINIRI"), "sabit oyuncu sayısı sınırı yok"


# ============================================================ A) adaptör: tam liste ne kadar büyük olursa olsun KESİLMEZ
def _govde(n, bas=1, xp=lambda i: ESIK + 10 * (10_000 - i)):
    taban = copy.deepcopy(FX["party_members_response"]["members"][0])
    uyeler = []
    for i in range(bas, bas + n):
        m = copy.deepcopy(taban)
        m.update(player_id=str(uuid.UUID(int=30_000 + i)), player_number=i, username=f"U{i}", xp=xp(i))
        uyeler.append(m)
    return {"members": uyeler, "role_limits": {}, "loyal": {}}


@pytest.mark.parametrize("n", [101, 120, 250, 329])
def test_api_hepsini_dondururse_liste_tam_sayilir_ve_kesilmez(n):
    u = a.uyeler_ayristir(_govde(n), n)
    assert u.tamam is True and u.kismi is False and len(u.uyeler) == n


@pytest.mark.parametrize("toplam", [101, 129, 329])
def test_api_tek_istekte_100_dondururse_kismi_okuma_eksik_liste(toplam):
    u = a.uyeler_ayristir(_govde(100), toplam)
    assert u.kismi is True and u.tamam is False and len(u.uyeler) == 100


@pytest.mark.parametrize("n,toplam", [(99, 329), (50, 329), (101, 329), (100, 99)])
def test_aciklanamayan_eksiklik_hala_reddedilir(n, toplam):
    u = a.uyeler_ayristir(_govde(n), toplam)
    assert u.kismi is False and u.tamam is False and "uyuşmuyor" in u.neden


def test_api_okuma_boyutu_gozlem_sabiti_politika_degil():
    assert a.API_UYE_OKUMA_BOYUTU == 100
    assert not hasattr(a, "UYE_SINIRI")


# ============================================================ B) sayfalama: parametre UYDURULMAZ, tanılama kanıt toplar
def _istemci(tmp_path, n_uye, member_count, basliklar=None, govde_ekstra=None):
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
            g = _govde(n_uye)
            g.update(govde_ekstra or {})
            return httpx.Response(200, json=g, headers=basliklar or {})
        return httpx.Response(404)

    return IstemciKaynagi(OrtamKimlik(depo, {}), transport=httpx.MockTransport(isle)), istekler


def test_kismi_okumada_ek_istek_atilmaz_baslikta_link_olsa_bile(tmp_path, caplog):
    k, istekler = _istemci(tmp_path, 100, 329, basliklar={"Link": '<https://x.test/p2>; rel="next"', "X-Total-Count": "329"})
    with caplog.at_level(logging.INFO, logger="dipbot.diplomacia.istemci"):
        p = run(k.fetch_party("1244"))
    assert p.kismi and len(p.uyeler) == 100 and p.toplam_uye == 329
    assert [r.url.path for r in istekler] == ["/api/parties/1244", "/api/parties/1244/members"]
    assert all(not r.url.query for r in istekler), "kanıtlanmamış page/offset/limit parametresi gönderilmez"
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "KISMİ okundu" in log and "okunan=100 toplam=329" in log
    # tanılama: sonraki gerçek günlükte sayfalama kanıtı görülebilsin (başlık adları + sayfalamayla ilgili değerler)
    assert "link" in log.lower() and "x-total-count" in log.lower() and "role_limits" in log
    assert "aaa.bbb.ccc" not in log


def test_tanilama_sir_sizdirmaz(tmp_path, caplog):
    k, _ = _istemci(tmp_path, 100, 329, basliklar={"Set-Cookie": "oturum=GIZLI", "Authorization": "Bearer GIZLI2"})
    with caplog.at_level(logging.INFO, logger="dipbot.diplomacia.istemci"):
        run(k.fetch_party("1244"))
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "GIZLI" not in log and "oturum=" not in log


# ============================================================ C) çekirdek: sabit 100 sınırı yok
def _src(n, toplam=None, xp=lambda i: ESIK + 1000):
    src = SahteKaynak()
    for i in range(1, n + 1):
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    src.parti("1244", "TEKNO", [(str(i), f"U{i}", xp(i)) for i in range(1, n + 1)], toplam=toplam)
    return src


@pytest.mark.parametrize("n", [75, 150, 329])
def test_tam_listede_uygun_tum_uyeler_takibe_alinir_sabit_sinir_yok(conn, servis, n):
    src = _src(n)
    s = run(servis.parti_ekle(G1, "1244", src))
    assert len(takipte(conn, G1)) == n and s.xp_uygun == n and not s.kismi
    assert bildirimler(conn) == []


def test_329_uyeli_partide_yalniz_xp_uygun_olanlar_alinir_dilim_sayisi_degil(conn, servis):
    # 329 üyenin 200'ü uygun, API 329'unu birden döndürüyor
    src = _src(329, xp=lambda i: ESIK + 1 if i % 3 != 0 else ESIK - 1)
    s = run(servis.parti_ekle(G1, "1244", src))
    beklenen = {str(i) for i in range(1, 330) if i % 3 != 0}
    assert takipte(conn, G1) == beklenen and len(beklenen) > 100
    assert s.xp_uygun == len(beklenen) and s.xp_alti == 329 - len(beklenen)


def test_kismi_okuma_eksik_oldugu_acikca_bildirilir_tam_liste_sayilmaz(conn, servis):
    src = _src(100, toplam=329)
    s = run(servis.parti_ekle(G1, "1244", src))
    assert s.kismi is True and s.incelenen == 100 and s.toplam_uye == 329
    r = conn.execute("SELECT * FROM takip_partiler WHERE group_id=?", (G1,)).fetchone()
    assert r["kismi"] == 1 and r["toplam_uye"] == 329


@pytest.fixture
def ui(conn, src, saat):
    return Arayuz(conn, src, KURUCU, saat=saat)


def _ui_parti(src, n, toplam, xp):
    for i in range(1, n + 1):
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    src.parti("1244", "Örnek Parti", [(str(i), f"U{i}", xp(i)) for i in range(1, n + 1)], toplam=toplam)


def test_ozet_kismi_okuma_hepsi_esik_ustu_okunmayan_arasinda_uygun_olabilir(conn, ui, src):
    _ui_parti(src, 100, 329, xp=lambda i: ESIK + 5000 - i)
    m = run(ui.komut(grup_komut("partiekle", "1244"))).metin
    for satir in ("Total members: 329", "Reviewed from the API: 100", "Tracked with 1,500,000+ XP: 100", "Below the XP threshold: 0"):
        assert satir in m, satir
    assert "INCOMPLETE" in m and "100/329" in m and "the other 229 members could not be read" in m
    assert "there may also be eligible players" in m, "okunan hepsi eşik üstü: okunmayanlar arasında da uygun olabilir"
    assert "ilk 100" not in m.lower() and "en fazla 100" not in m, "sabit 100 oyuncu sınırı anlatımı yok"


def test_ozet_kismi_okuma_en_dusuk_xp_esik_alti_ise_siralama_varsayilmaz(conn, ui, src):
    _ui_parti(src, 100, 329, xp=lambda i: ESIK + 1000 - i * 20)  # 100. üye eşik altı
    m = run(ui.komut(grup_komut("partiekle", "1244"))).metin
    assert "INCOMPLETE" in m and "the other 229 members could not be read" in m
    assert "cannot be verified" in m and "since the ordering is not trusted" in m
    assert "Tracked with 1,500,000+ XP: 50" in m and "Below the XP threshold: 50" in m


def test_ozet_tam_listede_eksik_uyarisi_yok_ve_sayfalama_kanitsiz_notu_yok(conn, ui, src):
    _ui_parti(src, 75, None, xp=lambda i: ESIK + i)
    m = run(ui.komut(grup_komut("partiekle", "1244"))).metin
    assert "INCOMPLETE" not in m and "could not be read" not in m and "Total members: 75" in m


# ============================================================ D) eksik/hatalı yanıtlar kimseyi takipten çıkarmaz
def test_eksik_veya_hatali_yanitlar_takip_edilenleri_silmez(conn, servis):
    from dipbot.kaynak import EksikVeri, SiteErisilemez
    src = _src(100, toplam=329)
    run(servis.parti_ekle(G1, "1244", src))
    once = takipte(conn, G1)
    # 1) site hatası
    src.hatalar[("parti", "1244")] = SiteErisilemez("x")
    run(servis.partiler_tara(G1, src))
    # 2) açıklanamayan eksik (50/329) -> reddedilir
    del src.hatalar[("parti", "1244")]
    src.parti("1244", "TEKNO", [(str(i), f"U{i}", ESIK + 1) for i in range(1, 51)], toplam=329)
    src.partiler["1244"] = PartiAnlik("1244", "TEKNO", src.partiler["1244"].uyeler, False, kismi=False, toplam_uye=329)
    r = run(servis.partiler_tara(G1, src))
    assert r.hatalar and takipte(conn, G1) == once
    # 3) kısmi okuma tamamen farklı 100 üye (tarama sırasında liste kayması) -> kimse silinmez
    src.parti("1244", "TEKNO", [(str(i), f"U{i}", ESIK + 1) for i in range(201, 301)], toplam=329)
    for i in range(201, 301):
        src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
    run(servis.partiler_tara(G1, src))
    assert once <= takipte(conn, G1) and bildirimler(conn, "parti_uyelik") == []


# ============================================================ E) çift üyelik YOK: oyuncu parti değiştirir, eski kayıtlar yanlış bildirim üretmez
def _iki_parti(src, a_uyeleri, b_uyeleri):
    for pid, ad, uy in (("1", "A", a_uyeleri), ("2", "B", b_uyeleri)):
        for i in uy:
            src.oyuncu(str(i), f"U{i}", "Ankara", "TR")
        src.parti(pid, ad, [(str(i), f"U{i}", ESIK + 1) for i in uy])


@pytest.mark.parametrize("sira", [("1", "2"), ("2", "1")])
def test_oyuncu_partiden_partiye_gecince_tam_bir_ayrilma_ve_bir_katilma_sira_fark_etmez(conn, servis, src, sira):
    _iki_parti(src, [10, 11, 12], [20, 21])
    run(servis.parti_ekle(G1, "1", src)); run(servis.parti_ekle(G1, "2", src))
    _iki_parti(src, [11, 12], [20, 21, 10])  # 10: A -> B
    for pid in sira:
        run(servis.partiler_tara(G1, src))  # her iki parti taranır; sıra tarama içi sabit, tekrar edilir
    for _ in range(3):  # tekrar taramalar yeni bildirim üretmez
        run(servis.partiler_tara(G1, src))
    v = bildirimler(conn, "parti_uyelik")
    ay = [(b["veri"]["parti_id"], x["oyuncu_id"]) for b in v for x in b["veri"]["ayrilanlar"]]
    ka = [(b["veri"]["parti_id"], x["oyuncu_id"]) for b in v for x in b["veri"]["katilanlar"]]
    assert ay == [("1", "10")] and ka == [("2", "10")]
    assert "10" in takipte(conn, G1), "ayrılan takipte kalır (spec)"


def test_gecikmis_eski_liste_geri_gelirse_ayni_degisiklik_tekrar_bildirilmez(conn, servis, src):
    _iki_parti(src, [10, 11], [20])
    run(servis.parti_ekle(G1, "1", src)); run(servis.parti_ekle(G1, "2", src))
    _iki_parti(src, [11], [20, 10])
    run(servis.partiler_tara(G1, src))
    n = len(bildirimler(conn, "parti_uyelik"))
    assert n == 2
    # verisi gecikmiş (eski) yanıt bir kez daha geldi: A'da 10 hâlâ görünüyor -> gerçek değişiklik gibi bildirilir mi?
    _iki_parti(src, [10, 11], [20, 10])
    run(servis.partiler_tara(G1, src))
    # önceki sonuca göre A'ya "katıldı" diye tek bildirim gelebilir; AMA aynı yanıt tekrar taranınca yenisi gelmez
    sonra = len(bildirimler(conn, "parti_uyelik"))
    run(servis.partiler_tara(G1, src))
    run(servis.partiler_tara(G1, src))
    assert len(bildirimler(conn, "parti_uyelik")) == sonra, "aynı (gecikmiş) yanıt tekrar tarandığında yeni bildirim yok"


def test_baska_parti_korumasi_kalmadi_esik_alti_sayaci_yalniz_gozlenen_xp_ile_calisir(conn, servis):
    """Çift üyelik varsayımına dayalı 'başka partide nitelikli' koruması kaldırıldı."""
    from dipbot import takip
    assert not hasattr(takip.TakipServisi, "_baska_partide_nitelikli")
