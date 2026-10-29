"""Çok hesaplı tarama zamanlayıcısı (TEK süreç, TEK SQLite, TEK bot, TEK bildirim kanalı).

- Parti ve savaş taramaları ile etkileşimli istekler BİRİNCİL hesapla (hesap_id'si en küçük) yapılır.
- Oyuncu taraması hesap başına AYRI çalışır: her hesap YALNIZ kendisine atanmış oyuncuları tarar (hesaplar.HesapYoneticisi.dagit).
- Bir hesap arıza verirse (token, proxy, rate limit, 403, ağ) YALNIZ o hesabın görevleri bekler; başka hesap DEVRALMAZ. Arıza ve
  bekleme durumları kalıcıdır (SQLite): yeniden başlatmada korunur. Proxy erişimi kesilirse doğrudan bağlantıya GEÇİLMEZ.
- Hız/kota: genel + hesap eşzamanlılığı ve istekler arası pacing (hesaplar.GenelSinir/HesapKaynagi); 429 -> hesap (ve aynı çıkış
  noktasını paylaşan hesaplar) için bekleme penceresi. Rate limit/erişim kontrollerini aşmaya yönelik hesap/IP değiştirme YOKTUR.
- Takip algoritmaları (TakipServisi), bildirim kuyruğu (outbox) ve kanal doğrulaması değişmemiştir.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from . import config, outbox
from .db import islem
from .hesaplar import (
    BirincilKaynak,
    DURUM_ERISIM_REDDI,
    DURUM_HAZIR,
    DURUM_HATA,
    DURUM_PROXY_HATASI,
    DURUM_RATE_LIMIT,
    DURUM_TOKEN_GECERSIZ,
    DURUM_TOKEN_YOK,
    SORUNLU_DURUMLAR,
    HesapYoneticisi,
)
from .kaynak import (
    ErisimReddedildi,
    IstekSiniri,
    KaynakHatasi,
    OzellikUygulanmadi,
    ProxyErisilemez,
    SinirliKaynak,
    TaramaOnbellegi,
    TokenGecersiz,
    TokenYok,
    hata_aciklamasi,
)
from .maske import maskele
from .takip import TakipServisi, TaramaSonucu
from .zaman import Saat, iso, iso_coz, monotonik_al, simdi_utc

log = logging.getLogger("dipbot.coklu")

HESAP_GEREKLI_TEKRAR = timedelta(hours=24)
TOKEN_HATIRLATMA_ARALIGI = timedelta(days=7)
PREV_OMRU = timedelta(seconds=config.TOKEN_PREV_OMRU_SANIYE)
BILDIRILEN = frozenset({DURUM_TOKEN_YOK, DURUM_TOKEN_GECERSIZ, DURUM_PROXY_HATASI})  # kurucuya geçiş bildirimi giden durumlar


class CokluZamanlayici:
    def __init__(
        self,
        conn,
        hesaplar: HesapYoneticisi,
        kurucu_id: int,
        saat: Saat = simdi_utc,
        *,
        kanal_id: int | None = None,
        kanal=None,
        grup_esler: int = config.SCAN_MAX_CONCURRENT_GROUPS,
    ):
        self.conn = conn
        self.hesaplar = hesaplar
        self.kurucu_id = kurucu_id
        self.saat = saat
        self._kanal_id = kanal_id
        self._kanal = kanal
        self._kanal_bildirilen_hazir_degil = False
        self.takip = TakipServisi(conn, saat, kanal_id=kanal_id)
        self._birincil = BirincilKaynak(hesaplar)
        self._kaynak = SinirliKaynak(self._birincil, config.SOURCE_MAX_CONCURRENT_REQUESTS, config.SOURCE_REQUEST_TIMEOUT_SECONDS)
        self._merkez_calisiyor = False
        self._hesap_calisan: set[int] = set()
        self._sem = asyncio.Semaphore(grup_esler)
        self._rl_sayac: dict[int, int] = {}

    # ---- Arayüz tarafı (Arayuz/ZamanlayiciKorumali bu API'yi kullanır) ----------------------------------------------
    @property
    def kapsamlar(self):
        return frozenset({config.MERKEZ_ID})

    @property
    def kanal_id(self) -> int:
        return self._kanal_id if self._kanal_id is not None else config.LOG_KANAL_ID

    @property
    def kaynak(self) -> SinirliKaynak:
        """Etkileşimli istekler için paylaşılan kaynak: BİRİNCİL hesap."""
        return self._kaynak

    @property
    def token_duraklatildi(self) -> bool:
        h = self.hesaplar.birincil()
        return h is not None and h["durum"] == DURUM_TOKEN_GECERSIZ

    def calisiyor_mu(self, grup_id: int) -> bool:
        return self._merkez_calisiyor or bool(self._hesap_calisan)

    def ag_onkontrol(self) -> None:
        """Etkileşimli istekten önce birincil hesabın durumunu denetler (açık kullanıcı mesajıyla hata)."""
        h = self.hesaplar.birincil()
        if h is None:
            e = TokenYok("no account")
            e.kullanici_mesaji = "no Diplomacia account is configured (Account Management → Add Account)"  # type: ignore[attr-defined]
            raise e
        if h["durum"] == DURUM_TOKEN_GECERSIZ:
            e = TokenGecersiz("token paused")
            e.kullanici_mesaji = "the primary account's token is invalid; update it in Account Management"  # type: ignore[attr-defined]
            raise e
        kalan = self.hesaplar.cooldown_kalan(h["hesap_id"])
        if kalan > 0:
            e = IstekSiniri("cooldown", kalan)
            e.kullanici_mesaji = f"the primary account is cooling down; try again in {int(kalan + 0.999)} s"  # type: ignore[attr-defined]
            raise e

    def token_gecersiz_bildir(self, kusak: int | None = None) -> None:
        h = self.hesaplar.birincil()
        if h is not None:
            self._hata_isle(h["hesap_id"], TokenGecersiz("rejected"), kusak)

    def istek_siniri_bildir(self, e: IstekSiniri) -> None:
        h = self.hesaplar.birincil()
        if h is not None:
            self._hata_isle(h["hesap_id"], e)

    def erisim_reddi_bildir(self) -> None:
        h = self.hesaplar.birincil()
        if h is not None:
            self._hata_isle(h["hesap_id"], ErisimReddedildi("403"))

    def token_devam_ettir(self) -> None:  # eski tek-token akışının arayüzü; çok hesapta token_guncelle durumu temizler
        pass

    # ---- hata -> hesap durumu ---------------------------------------------------------------------------------------
    def _bildir_hesap_durumu(self, hesap_id: int, ad: str, durum: str, ayrinti: str | None) -> None:
        simdi = self.saat()
        try:
            with islem(self.conn):
                outbox.kuyruga_ekle(
                    self.conn, hedef_sohbet=self.kurucu_id, grup_id=None, tur="hesap_durumu",
                    idem_anahtari=f"hesap_durumu:{hesap_id}:{durum}:{iso(simdi)}",
                    veri={"hesap_id": hesap_id, "hesap_ad": ad, "durum": durum, "ayrinti": (ayrinti or "")[:100], "zaman": iso(simdi)},
                    simdi=simdi,
                )
        except Exception:
            log.exception("hesap durum bildirimi kuyruğa alınamadı")

    def _durum_degisti(self, hesap_id: int, durum: str, ayrinti: str | None = None) -> None:
        h = self.hesaplar.getir(hesap_id)
        if h is None:
            return
        onceki = h["durum"]
        degisti = self.hesaplar.durum_yaz(hesap_id, durum, ayrinti)
        if not degisti:
            return
        if durum in BILDIRILEN:
            self._bildir_hesap_durumu(hesap_id, h["ad"], durum, ayrinti)
        elif durum == DURUM_HAZIR and onceki in BILDIRILEN:
            self._bildir_hesap_durumu(hesap_id, h["ad"], durum, None)

    def _hata_isle(self, hesap_id: int, e: BaseException, kusak: int | None = None) -> str:
        """Kaynak hatasını HESABIN durumuna işler (diğer hesaplara dokunmaz). Dönüş: yeni durum."""
        hy = self.hesaplar
        simdi = self.saat()
        if isinstance(e, TokenGecersiz):
            guncel = hy._token_kusaklari.get(hesap_id, 0)
            if kusak is not None and kusak != guncel:
                return DURUM_HAZIR  # istek eski tokenla başlamıştı; token sonradan yenilendi: bayat 401 yok sayılır
            self._durum_degisti(hesap_id, DURUM_TOKEN_GECERSIZ, "token rejected (401)")
            return DURUM_TOKEN_GECERSIZ
        if isinstance(e, ProxyErisilemez):
            h = hy.getir(hesap_id)
            n = (h["ardisik_hata"] if h else 0) + 1
            bekle = min(config.HESAP_PROXY_GERI_CEKILME_SN * (2 ** min(n - 1, 10)), config.HESAP_PROXY_GERI_CEKILME_UST_SN)
            with islem(self.conn):
                self.conn.execute("UPDATE hesaplar SET proxy_durum='hata', ardisik_hata=? WHERE hesap_id=?", (n, hesap_id))
            hy.cooldown_ayarla(hesap_id, bekle)
            self._durum_degisti(hesap_id, DURUM_PROXY_HATASI, "proxy unreachable (no direct connection)")
            return DURUM_PROXY_HATASI
        if isinstance(e, IstekSiniri):
            n = self._rl_sayac.get(hesap_id, 0) + 1
            self._rl_sayac[hesap_id] = n
            bekle = e.bekle_saniye if e.bekle_saniye is not None else config.RATE_LIMIT_DEFAULT_WAIT_SECONDS * (2 ** min(n - 1, 20))
            bekle = min(max(bekle, 0.0), config.RATE_LIMIT_MAX_WAIT_SECONDS)
            hy.cooldown_ayarla(hesap_id, bekle, rate_limit=True)
            self._durum_degisti(hesap_id, DURUM_RATE_LIMIT, f"rate limit; cooldown {int(bekle)} s")
            return DURUM_RATE_LIMIT
        if isinstance(e, ErisimReddedildi):
            hy.cooldown_ayarla(hesap_id, config.ERISIM_REDDI_BEKLEME_SECONDS)
            self._durum_degisti(hesap_id, DURUM_ERISIM_REDDI, "access denied (403)")
            return DURUM_ERISIM_REDDI
        if isinstance(e, TokenYok):
            self._durum_degisti(hesap_id, DURUM_TOKEN_YOK, "no token stored")
            return DURUM_TOKEN_YOK
        self._durum_degisti(hesap_id, DURUM_HATA, hata_aciklamasi(e))
        return DURUM_HATA

    def _basari(self, hesap_id: int) -> None:
        h = self.hesaplar.getir(hesap_id)
        self._rl_sayac.pop(hesap_id, None)
        with islem(self.conn):
            self.conn.execute(
                "UPDATE hesaplar SET ardisik_hata=0, cooldown_bitis=NULL, proxy_durum=CASE WHEN proxy_var=1 THEN 'calisiyor' ELSE 'yok' END"
                " WHERE hesap_id=?", (hesap_id,))
        if h is not None and h["durum"] != DURUM_HAZIR:
            self._durum_degisti(hesap_id, DURUM_HAZIR)

    # ---- kapasite uyarısı -------------------------------------------------------------------------------------------
    def _kapasite_uyarisi(self) -> None:
        oz = self.hesaplar.kapasite_ozeti()
        simdi = self.saat()
        r = self.conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='hesap_gerekli_zaman'").fetchone()
        if oz["bekleyen"] == 0:
            if r is not None:
                with islem(self.conn):
                    self.conn.execute("DELETE FROM sistem_durumu WHERE anahtar='hesap_gerekli_zaman'")
            return
        if r is not None:
            try:
                if simdi - iso_coz(r[0]) < HESAP_GEREKLI_TEKRAR:
                    return
            except ValueError:
                pass
        with islem(self.conn):
            outbox.kuyruga_ekle(
                self.conn, hedef_sohbet=self.kurucu_id, grup_id=None, tur="hesap_gerekli",
                idem_anahtari=f"hesap_gerekli:{iso(simdi)}",
                veri={"bekleyen": oz["bekleyen"], "kapasite": oz["kapasite"], "hesap_sayisi": oz["hesap_sayisi"], "zaman": iso(simdi)},
                simdi=simdi,
            )
            self.conn.execute("INSERT OR REPLACE INTO sistem_durumu(anahtar, deger) VALUES('hesap_gerekli_zaman', ?)", (iso(simdi),))

    # ---- planlama ---------------------------------------------------------------------------------------------------
    def _aralik_dk(self) -> int:
        r = self.conn.execute("SELECT tarama_araligi_dk FROM grup_ayarlari WHERE group_id=?", (config.MERKEZ_ID,)).fetchone()
        return r[0] if r else config.DEFAULT_SCAN_INTERVAL_MINUTES

    def _is_kaydet(self, is_tipi: str, ok: bool, hata: str | None) -> None:
        simdi = iso(self.saat())
        M = config.MERKEZ_ID
        with islem(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO tarama_isleri(group_id, is_tipi) VALUES(?,?)", (M, is_tipi))
            if ok:
                self.conn.execute("UPDATE tarama_isleri SET son_deneme=?, son_basari=?, son_hata=NULL WHERE group_id=? AND is_tipi=?",
                                  (simdi, simdi, M, is_tipi))
            else:
                self.conn.execute("UPDATE tarama_isleri SET son_deneme=?, son_hata=? WHERE group_id=? AND is_tipi=?",
                                  (simdi, (hata or "error")[:300], M, is_tipi))

    def _merkez_sonraki(self, tam: bool) -> None:
        M = config.MERKEZ_ID
        r = self.conn.execute("SELECT tarama_araligi_dk, ardisik_hata FROM grup_ayarlari WHERE group_id=?", (M,)).fetchone()
        if r is None:
            return
        simdi = self.saat()
        hata = 0 if tam else r["ardisik_hata"] + 1
        carpan = 1 if tam else min(2**hata, config.SCAN_FAILURE_BACKOFF_MAX_MULTIPLIER)
        with islem(self.conn):
            self.conn.execute(
                "UPDATE grup_ayarlari SET son_tarama_denemesi=?, sonraki_tarama=?, ardisik_hata=? WHERE group_id=?",
                (iso(simdi), iso(simdi + timedelta(minutes=r["tarama_araligi_dk"] * carpan)), hata, M))

    # ---- tick -------------------------------------------------------------------------------------------------------
    def tick(self) -> list[asyncio.Task]:
        simdi = self.saat()
        M = config.MERKEZ_ID
        self.hesaplar.dagit()
        self._kapasite_uyarisi()
        gorevler: list[asyncio.Task] = []
        self.conn.execute("INSERT OR IGNORE INTO grup_ayarlari(group_id) VALUES(?)", (M,))
        # --- merkez işleri (parti + savaş): birincil hesapla
        b = self.hesaplar.birincil()
        r = self.conn.execute("SELECT sonraki_tarama FROM grup_ayarlari WHERE group_id=?", (M,)).fetchone()
        if (b is not None and not self._merkez_calisiyor and b["durum"] not in (DURUM_TOKEN_GECERSIZ, DURUM_TOKEN_YOK)
                and self.hesaplar.cooldown_kalan(b["hesap_id"]) <= 0 and (r[0] is None or r[0] <= iso(simdi))):
            self._merkez_calisiyor = True
            gorevler.append(asyncio.ensure_future(self._merkez_calis(b["hesap_id"])))
        # --- oyuncu taramaları: hesap başına, YALNIZ kendi görevleri
        for h in self.hesaplar.listele():
            hid = h["hesap_id"]
            if hid in self._hesap_calisan:
                continue
            if h["durum"] in (DURUM_TOKEN_GECERSIZ, DURUM_TOKEN_YOK):
                continue  # kurucu token'ı güncelleyene kadar bekler (devralan yok)
            if self.hesaplar.cooldown_kalan(hid) > 0:
                continue
            if h["sonraki_tarama"] is not None and h["sonraki_tarama"] > iso(simdi):
                continue
            self._hesap_calisan.add(hid)
            gorevler.append(asyncio.ensure_future(self._hesap_calis(hid)))
        return gorevler

    async def tick_ve_bekle(self):
        return list(await asyncio.gather(*self.tick()))

    # ---- merkez işleri: parti + savaş (birincil) -----------------------------------------------------------------------
    async def _merkez_calis(self, hesap_id: int) -> list[TaramaSonucu]:
        M = config.MERKEZ_ID
        kaynak = TaramaOnbellegi(self._kaynak)
        sonuclar: list[TaramaSonucu] = []
        try:
            async with self._sem:
                for is_tipi, fn in (("partiler", self.takip.partiler_tara), ("savaslar", self.takip.savaslari_tara)):
                    try:
                        sonuc = await fn(M, kaynak)
                    except KaynakHatasi as e:
                        durum = self._hata_isle(hesap_id, e, getattr(e, "kusak", None))
                        sonuc = TaramaSonucu(is_tipi, hatalar=[hata_aciklamasi(e)])
                        sonuc.yapilandirma = isinstance(e, OzellikUygulanmadi)
                        self._is_kaydet(is_tipi, False, "; ".join(sonuc.hatalar))
                        sonuclar.append(sonuc)
                        if durum in (DURUM_TOKEN_GECERSIZ, DURUM_PROXY_HATASI, DURUM_ERISIM_REDDI, DURUM_RATE_LIMIT, DURUM_TOKEN_YOK):
                            break
                        continue
                    except Exception as e:
                        log.exception("beklenmeyen tarama hatası (is=%s)", is_tipi)
                        sonuc = TaramaSonucu(is_tipi, hatalar=[f"unexpected error: {type(e).__name__}"])
                    sonuclar.append(sonuc)
                    self._is_kaydet(is_tipi, sonuc.ok, "; ".join(sonuc.hatalar))
        finally:
            self._merkez_calisiyor = False
            tam = len(sonuclar) == 2 and all(x.ok or x.yapilandirma for x in sonuclar)
            self._merkez_sonraki(tam)
        return sonuclar

    # ---- hesap oyuncu taraması ---------------------------------------------------------------------------------------
    async def _hesap_calis(self, hesap_id: int) -> TaramaSonucu | None:
        M = config.MERKEZ_ID
        baslangic = time.monotonic()
        sonuc: TaramaSonucu | None = None
        durum_ok = False
        try:
            async with self._sem:
                idler = self.hesaplar.atanan_idler(hesap_id)
                if not idler:
                    durum_ok = True  # görev yok: tarama gerekmez
                    return None
                try:
                    kaynak = self.hesaplar.kaynak(hesap_id)
                except KaynakHatasi as e:  # ör. proxy dosyası okunamıyor: doğrudan bağlantıya DÜŞME
                    self._hata_isle(hesap_id, e)
                    return None
                try:
                    sonuc = await self.takip.oyuncular_tara(M, kaynak, oyuncu_idleri=idler, oyuncu_sonucu=self.hesaplar.oyuncu_sonucu)
                except KaynakHatasi as e:
                    self._hata_isle(hesap_id, e, getattr(e, "kusak", None))
                    return None
                except Exception:
                    log.exception("beklenmeyen hesap tarama hatası (hesap=%s)", hesap_id)
                    self._durum_degisti(hesap_id, DURUM_HATA, "unexpected error")
                    return None
                if sonuc.yapilandirma:
                    self._durum_degisti(hesap_id, DURUM_TOKEN_YOK, "no usable token")
                    return sonuc
                durum_ok = sonuc.ok
                if durum_ok:
                    self._basari(hesap_id)
                return sonuc
        finally:
            self._hesap_calisan.discard(hesap_id)
            sure = time.monotonic() - baslangic
            h = self.hesaplar.getir(hesap_id)
            if h is not None:
                simdi = self.saat()
                carpan = 1 if (durum_ok or sonuc is None) else 2  # kısmi başarısızlık: bir sonraki tarama iki kat geç
                with islem(self.conn):
                    self.conn.execute(
                        "UPDATE hesaplar SET son_tarama_zamani=?, son_tarama_sn=?, sonraki_tarama=? WHERE hesap_id=?",
                        (iso(simdi), round(sure, 2), iso(simdi + timedelta(minutes=self._aralik_dk() * carpan)), hesap_id))
                if durum_ok:
                    self._oyuncu_tarama_ozeti()
                elif sonuc is not None:
                    self._is_kaydet("oyuncular", False, "; ".join(sonuc.hatalar)[:300] or "account scan incomplete")

    def _oyuncu_tarama_ozeti(self) -> None:
        """'oyuncular' işinin son başarısı: görevi olan TÜM hesapların en eski başarılı taraması (hepsi o zamandan beri tarandı)."""
        satirlar = self.conn.execute(
            "SELECT h.hesap_id, MAX(a.son_basari) AS s FROM hesaplar h JOIN oyuncu_atamalari a ON a.hesap_id=h.hesap_id GROUP BY h.hesap_id"
        ).fetchall()
        if not satirlar or any(r["s"] is None for r in satirlar):
            return
        en_eski = min(r["s"] for r in satirlar)
        M = config.MERKEZ_ID
        with islem(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO tarama_isleri(group_id, is_tipi) VALUES(?,?)", (M, "oyuncular"))
            self.conn.execute("UPDATE tarama_isleri SET son_deneme=?, son_basari=?, son_hata=NULL WHERE group_id=? AND is_tipi='oyuncular'",
                              (iso(self.saat()), en_eski, M))

    # ---- haftalık token hatırlatması + .prev temizliği (hesap başına) ----------------------------------------------------
    def token_hatirlat(self) -> bool:
        gonderildi = False
        simdi = self.saat()
        for h in self.hesaplar.listele():
            if h["durum"] in (DURUM_TOKEN_GECERSIZ, DURUM_TOKEN_YOK) or not h["token_guncelleme"]:
                continue
            try:
                gecen = simdi - iso_coz(h["token_guncelleme"])
            except ValueError:
                continue
            if gecen < TOKEN_HATIRLATMA_ARALIGI:
                continue
            hafta = int(gecen / TOKEN_HATIRLATMA_ARALIGI)
            with islem(self.conn):
                gonderildi |= outbox.kuyruga_ekle(
                    self.conn, hedef_sohbet=self.kurucu_id, grup_id=None, tur="token_hatirlatma",
                    idem_anahtari=f"token_hatirlatma:{h['hesap_id']}:{h['token_guncelleme']}:{hafta}",
                    veri={"gun": gecen.days, "zaman": iso(simdi), "hesap_ad": h["ad"]}, simdi=simdi)
        return gonderildi

    def prev_politikasi_uygula(self) -> bool:
        """Her hesabın `.prev` yedeği 24 saati aşarsa silinir (yalnız unlink; ana token yoksa veya yaş belirsizse silinmez)."""
        silindi = False
        for h in self.hesaplar.listele():
            depo = self.hesaplar.depo.token(h["hesap_id"])
            if not depo.var_mi():
                continue
            yas = depo.prev_yasi(self.saat().timestamp())
            if yas is None or yas < PREV_OMRU.total_seconds():
                continue
            silindi |= depo.prev_sil()
        return silindi

    # ---- kanal doğrulaması + üretim döngüsü ---------------------------------------------------------------------------
    async def _kanal_kontrol(self) -> bool:
        if self._kanal is None:
            return True
        try:
            hazir = await self._kanal.hazirla()
        except Exception:
            log.exception("kanal doğrulama hatası")
            hazir = False
        durum = getattr(self._kanal, "durum", None)
        if not hazir and not self._kanal_bildirilen_hazir_degil:
            self._kanal_bildirilen_hazir_degil = True
            self._kanal_bildir(False, getattr(durum, "neden", "unknown"))
        elif hazir and self._kanal_bildirilen_hazir_degil:
            self._kanal_bildirilen_hazir_degil = False
            self._kanal_bildir(True, "ok")
        return hazir

    def _kanal_bildir(self, hazir: bool, neden: str) -> None:
        simdi = self.saat()
        try:
            with islem(self.conn):
                outbox.kuyruga_ekle(
                    self.conn, hedef_sohbet=self.kurucu_id, grup_id=None, tur="kanal_durumu",
                    idem_anahtari=f"kanal_durumu:{int(hazir)}:{iso(simdi)}",
                    veri={"hazir": hazir, "neden": neden[:200], "kanal_id": self.kanal_id, "zaman": iso(simdi)}, simdi=simdi)
        except Exception:
            log.exception("kanal durum bildirimi kuyruğa alınamadı")

    async def calis(self, gonderici: outbox.Gonderici, durdur: asyncio.Event) -> None:
        gorevler: set[asyncio.Task] = set()
        ardisik = 0
        mono = monotonik_al(self.saat)
        prev_son: float | None = None
        while not durdur.is_set():
            bekleme = config.TICK_SECONDS
            if prev_son is None or mono() - prev_son >= config.TOKEN_PREV_KONTROL_ARALIGI_SANIYE:
                prev_son = mono()
                try:
                    self.prev_politikasi_uygula()
                except Exception:
                    log.warning("token .prev politikası uygulanamadı")
            kanal_hazir = await self._kanal_kontrol()
            try:
                if kanal_hazir:  # kanal hazır değilken tarama yapılmaz: değişiklik bildirimsiz yutulmasın
                    for g in self.tick():
                        gorevler.add(g)
                        g.add_done_callback(gorevler.discard)
                else:
                    self.hesaplar.dagit()
                ardisik = 0
                self.token_hatirlat()
            except Exception as e:
                ardisik += 1
                log.error("tick hatası (art arda %d): %s", ardisik, maskele(f"{type(e).__name__}: {str(e)[:200]}"))
                bekleme = min(config.TICK_SECONDS * (2**ardisik), config.SCHEDULER_ERROR_BACKOFF_MAX_SECONDS)
            try:
                await outbox.bekleyenleri_gonder(
                    self.conn, gonderici, self.saat, izinli_hedefler={self.kurucu_id, self.kanal_id},
                    izinli_kapsamlar=self.kapsamlar)
            except Exception:
                log.exception("outbox gönderim turu hatası")
            try:
                await asyncio.wait_for(durdur.wait(), timeout=bekleme)
            except asyncio.TimeoutError:
                pass
        if gorevler:
            await asyncio.gather(*gorevler, return_exceptions=True)
