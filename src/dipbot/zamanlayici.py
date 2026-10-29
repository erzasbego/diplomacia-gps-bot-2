"""Tarama zamanlayıcısı çekirdeği (asyncio).

- Grup başına ayrı aralık; aralık bitmeden/önceki tarama sürerken aynı grup yeniden başlamaz.
- Gruplar ayrı görevlerde çalışır: yavaş bir grup diğerlerini beklettirmez.
- Eşzamanlılık sınırlı: grup görevleri semaforlu, kaynak istekleri SinirliKaynak ile sınırlı.
- Yalnız 401 (TokenGecersiz) tüm taramayı DURAKLATIR; 403 (ErisimReddedildi) geri çekilmeli hatadır, duraklatmaz.
  Token dosyası yok (TokenYok) = yapılandırma sınıfı: istek yok, geri çekilme yok, iş tipi başına tek uyarı.
- Token geçersizse tüm tarama DURAKLATILIR ve kurucuya TEK bildirim gider (yağmur yok).
- Hatalı/eksik tarama "başarılı" kaydedilmez (tarama_isleri.son_basari değişmez).
- İstek sınırında bekleme penceresi uygulanır; bekleme bellektedir (yeniden başlatmada sıfırlanır).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import config, outbox
from .db import alan_aktif, islem
from .maske import maskele
from .kaynak import (
    DiplomaciaSource,
    ErisimReddedildi,
    IstekSiniri,
    KaynakHatasi,
    SinirliKaynak,
    TaramaOnbellegi,
    TokenGecersiz,
    hata_aciklamasi,
)
from .takip import TakipServisi, TaramaSonucu
from .zaman import Saat, iso, iso_coz, monotonik_al, simdi_utc

log = logging.getLogger("dipbot.zamanlayici")


class TaramaZamanlayici:
    def __init__(
        self,
        conn,
        kaynak: DiplomaciaSource,
        kurucu_id: int,
        saat: Saat = simdi_utc,
        *,
        grup_esler: int = config.SCAN_MAX_CONCURRENT_GROUPS,
        istek_esler: int = config.SOURCE_MAX_CONCURRENT_REQUESTS,
        istek_zaman_asimi: float = config.SOURCE_REQUEST_TIMEOUT_SECONDS,
        token_deposu=None,
        kapsamlar=None,
        kanal_id: int | None = None,
        kanal=None,
    ):
        # kapsamlar: taranacak takip ALANLARI. ÜRETİMDE yalnız merkezi alan ({config.MERKEZ_ID}); eski grup kimlikleri
        # (arşiv) ASLA taranmaz. None = onaylı tüm alanlar (yalnız alan-yalıtımı/algoritma testleri için).
        self._aktif = None if kapsamlar is None else frozenset(int(x) for x in kapsamlar)
        self._kanal_id = kanal_id
        self._kanal = kanal  # KanalDogrulayici (veya None): hazır değilken tarama duraklar, kanal satırları bekler
        self._kanal_bildirilen_hazir_degil = False
        self._token_deposu = token_deposu  # yalnız var_mi()/son_degisiklik() için; token okunmaz
        self.conn = conn
        self.kurucu_id = kurucu_id
        self.saat = saat
        self.takip = TakipServisi(conn, saat)
        self._kaynak = SinirliKaynak(kaynak, istek_esler, istek_zaman_asimi, kusak_fn=lambda: self._token_kusagi)
        self._sem = asyncio.Semaphore(grup_esler)
        self._calisan: set[int] = set()  # çalışan grup taramaları (kilit)
        self._engel_bitis = None  # istek sınırı bekleme sonu
        self._erisim_reddi_bitis = None  # 403 sonrası arayüz için kısa geri çekilme penceresi
        self._istek_siniri_sayac = 0
        self._yapilandirma_uyarildi: set[str] = set()  # iş tipi başına süreç boyunca TEK uyarı
        r = conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='token_duraklatildi'").fetchone()
        self._token_dur = bool(r and r[0] == "1")
        self._token_kusagi = 0  # token_devam_ettir her çağrıda artar (bayat 401'leri ayırt etmek için)

    # ---- durum -----------------------------------------------------------
    @property
    def kapsamlar(self):
        """Taranan takip alanları (None: kısıtsız, yalnız testler)."""
        return self._aktif

    @property
    def kanal_id(self) -> int:
        return self._kanal_id if self._kanal_id is not None else config.LOG_KANAL_ID

    @property
    def token_duraklatildi(self) -> bool:
        return self._token_dur

    @property
    def kaynak(self) -> SinirliKaynak:
        """Zamanlayıcının SinirliKaynak'ı: arayüz de AYNI nesneyi kullanır (ortak eşzamanlılık sınırı)."""
        return self._kaynak

    def ag_onkontrol(self) -> None:
        """Arayüz ağa çıkmadan önce: duraklatma/istek sınırı/403 penceresindeyse açık mesajlı hata fırlatır."""
        simdi = self.saat()
        if self._token_dur:
            e = TokenGecersiz("token duraklatıldı")
            e.kullanici_mesaji = "token is invalid; the owner must renew it"  # type: ignore[attr-defined]
            raise e
        if self._engel_bitis is not None and simdi < self._engel_bitis:
            n = max(1, int((self._engel_bitis - simdi).total_seconds() + 0.999))
            e = IstekSiniri("istek sınırı penceresi", float(n))
            e.kullanici_mesaji = f"rate limit; try again in {n} s"  # type: ignore[attr-defined]
            raise e
        if self._erisim_reddi_bitis is not None and simdi < self._erisim_reddi_bitis:
            n = max(1, int((self._erisim_reddi_bitis - simdi).total_seconds() + 0.999))
            e = ErisimReddedildi("erişim reddi geri çekilmesi")
            e.kullanici_mesaji = f"Diplomacia connection unavailable (access denied); try again in {n} s"  # type: ignore[attr-defined]
            raise e

    def token_gecersiz_bildir(self, kusak: int | None = None) -> None:
        self._token_duraklat(kusak)

    def istek_siniri_bildir(self, e: IstekSiniri) -> None:
        self._istek_siniri(e)

    def erisim_reddi_bildir(self) -> None:
        self._erisim_reddi_bitis = self.saat() + timedelta(seconds=config.ERISIM_REDDI_BEKLEME_SECONDS)

    def calisiyor_mu(self, grup_id: int) -> bool:
        return grup_id in self._calisan

    def _token_duraklat(self, kusak: int | None = None) -> None:
        if kusak is not None and kusak != self._token_kusagi:
            return  # istek eski tokenla başlamıştı; token sonradan yenilendi: yalnız o istek hata sayılır
        # Eşzamanlı görevler aynı anda TokenGecersiz alsa da kontrol+kurma senkron: tek bildirim.
        if self._token_dur:
            return
        self._token_dur = True
        simdi = self.saat()
        with islem(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO sistem_durumu(anahtar, deger) VALUES('token_duraklatildi','1')"
            )
            outbox.kuyruga_ekle(
                self.conn,
                hedef_sohbet=self.kurucu_id,
                grup_id=None,
                tur="token_gecersiz",
                idem_anahtari=f"token_gecersiz:{iso(simdi)}",
                veri={"zaman": iso(simdi)},
                simdi=simdi,
            )

    def token_devam_ettir(self) -> None:
        """Yeni token DOĞRULANDIKTAN sonra çağrılır; taramalar kaldığı yerden sürer."""
        self._token_kusagi += 1
        self._token_dur = False
        with islem(self.conn):
            self.conn.execute("DELETE FROM sistem_durumu WHERE anahtar='token_duraklatildi'")
            self.conn.execute("UPDATE grup_ayarlari SET sonraki_tarama=NULL")

    # ---- token .prev yedeği politikası (Tur F2b) ---------------------------
    def _prev_sil(self, neden: str) -> bool:
        silindi = self._token_deposu is not None and self._token_deposu.prev_sil()
        if silindi:
            log.info("önceki token yedeği (.prev) silindi: %s", neden)  # içerik/yol dışında bilgi yok
        return silindi

    def prev_politikasi_uygula(self) -> bool:
        """`.prev` yedeği yaşı TOKEN_PREV_OMRU_SANIYE'yi aşmışsa siler. Dönüş: silindi mi.
        Güvenlik: (0) taramalar token nedeniyle duraklatılmışsa SİLMEZ; (1) ana token dosyası yok/boşsa SİLMEZ (.prev tek kopya olabilir); (2) yaş belirsizse (dosya zamanı
        gelecekte: saat geri gitmiş, ör. boot'ta hatalı RTC) SİLMEZ, bir kez uyarır; saat düzelince normal işler.
        Token dosyasına dokunmaz."""
        if self._token_deposu is None or self._token_dur:
            return False  # token duraklatılmışken (yeni token kanıtlanamadı) .prev son çalışan token olabilir
        yas = self._token_deposu.prev_yasi(self.saat().timestamp())
        if yas is None:
            return False
        if yas < -config.SAAT_TUTARSIZLIK_TOLERANSI_SANIYE:
            if "prev_belirsiz" not in self._yapilandirma_uyarildi:
                self._yapilandirma_uyarildi.add("prev_belirsiz")
                log.warning("token .prev yedeğinin yaşı belirsiz (dosya zamanı gelecekte; sunucu saati geride olabilir): "
                            "silinmedi, saat düzelince politika uygulanır")
            return False
        if yas < config.TOKEN_PREV_OMRU_SANIYE:
            return False
        try:
            ana_var = bool((self._token_deposu.oku() or "").strip())
        except OSError:
            ana_var = False
        if not ana_var:
            return False
        return self._prev_sil("yaş sınırı doldu")

    TOKEN_HATIRLATMA_ARALIGI = timedelta(days=7)

    def token_hatirlat(self) -> bool:
        """Haftalık token hatırlatması (YALNIZ kurucuya özel). Gerçek token ömrü doğrulanmadığından
        son başarılı token güncellemesinden itibaren 7 günde bir sorulur. Token geçersizken (zaten
        bildirildi) veya token dosyası yokken susar. Anahtar (son güncelleme, hafta no): tick
        yağmuru yok; bot uzun süre kapalı kalsa da o hafta için TEK mesaj. Dönüş: yeni mesaj eklendi mi."""
        if self._token_deposu is None or self._token_dur or not self._token_deposu.var_mi():
            return False
        simdi = self.saat()
        r = self.conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='token_son_guncelleme'").fetchone()
        if r is None:
            mt = self._token_deposu.son_degisiklik()
            ilk = simdi
            if mt is not None:
                dosya = datetime.fromtimestamp(mt, timezone.utc)
                ilk = min(dosya, simdi)
            with islem(self.conn):
                self.conn.execute(
                    "INSERT OR IGNORE INTO sistem_durumu(anahtar, deger) VALUES('token_son_guncelleme', ?)",
                    (iso(ilk),),
                )
            r = self.conn.execute("SELECT deger FROM sistem_durumu WHERE anahtar='token_son_guncelleme'").fetchone()
        son = iso_coz(r[0])
        gecen = simdi - son
        if gecen < self.TOKEN_HATIRLATMA_ARALIGI:
            return False
        hafta = int(gecen / self.TOKEN_HATIRLATMA_ARALIGI)
        with islem(self.conn):
            return outbox.kuyruga_ekle(
                self.conn,
                hedef_sohbet=self.kurucu_id,
                grup_id=None,
                tur="token_hatirlatma",
                idem_anahtari=f"token_hatirlatma:{r[0]}:{hafta}",
                veri={"gun": gecen.days, "zaman": iso(simdi)},
                simdi=simdi,
            )

    def _istek_siniri(self, e: IstekSiniri) -> None:
        self._istek_siniri_sayac += 1
        bekle = e.bekle_saniye
        if bekle is None:
            bekle = config.RATE_LIMIT_DEFAULT_WAIT_SECONDS * (2 ** min(self._istek_siniri_sayac - 1, 20))
        bekle = min(max(bekle, 0.0), config.RATE_LIMIT_MAX_WAIT_SECONDS)
        self._engel_bitis = self.saat() + timedelta(seconds=bekle)

    # ---- kayıt -----------------------------------------------------------
    def _is_kaydet(self, grup_id: int, is_tipi: str, ok: bool, hata: str | None) -> None:
        simdi = iso(self.saat())
        with islem(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO tarama_isleri(group_id, is_tipi) VALUES(?,?)", (grup_id, is_tipi)
            )
            if ok:
                self.conn.execute(
                    "UPDATE tarama_isleri SET son_deneme=?, son_basari=?, son_hata=NULL"
                    " WHERE group_id=? AND is_tipi=?",
                    (simdi, simdi, grup_id, is_tipi),
                )
            else:
                self.conn.execute(
                    "UPDATE tarama_isleri SET son_deneme=?, son_hata=? WHERE group_id=? AND is_tipi=?",
                    (simdi, (hata or "error")[:300], grup_id, is_tipi),
                )

    def _sonraki_planla(self, grup_id: int, hepsi_ok: bool) -> None:
        r = self.conn.execute(
            "SELECT tarama_araligi_dk, ardisik_hata FROM grup_ayarlari WHERE group_id=?", (grup_id,)
        ).fetchone()
        if r is None:
            return
        simdi = self.saat()
        hata = 0 if hepsi_ok else r["ardisik_hata"] + 1
        carpan = 1 if hepsi_ok else min(2**hata, config.SCAN_FAILURE_BACKOFF_MAX_MULTIPLIER)
        sonraki = simdi + timedelta(minutes=r["tarama_araligi_dk"] * carpan)
        with islem(self.conn):
            self.conn.execute(
                "UPDATE grup_ayarlari SET son_tarama_denemesi=?, sonraki_tarama=?, ardisik_hata=?"
                " WHERE group_id=?",
                (iso(simdi), iso(sonraki), hata, grup_id),
            )

    # ---- çalıştırma ------------------------------------------------------
    def tick(self) -> list[asyncio.Task]:
        """Vadesi gelen, onaylı ve o an çalışmayan grupların taramasını başlatır (beklemez)."""
        simdi = self.saat()
        if self._token_dur:
            return []
        if self._engel_bitis is not None and simdi < self._engel_bitis:
            return []
        kaynak = TaramaOnbellegi(self._kaynak)  # yalnız bu turda istek paylaşımı
        gorevler = []
        if self._aktif is None:  # yalnız testler: onaylı tüm alanlar
            alanlar = [r[0] for r in self.conn.execute("SELECT group_id FROM onayli_gruplar ORDER BY group_id")]
        else:
            alanlar = sorted(g for g in self._aktif if alan_aktif(self.conn, g))
        satirlar = []
        for gid in alanlar:
            a_ = self.conn.execute("SELECT sonraki_tarama FROM grup_ayarlari WHERE group_id=?", (gid,)).fetchone()
            satirlar.append({"gid": gid, "sonraki": a_[0] if a_ else None})
        for r in satirlar:
            gid = r["gid"]
            if gid in self._calisan:
                continue  # önceki tarama bitmeden yenisi başlamaz
            if r["sonraki"] is not None and r["sonraki"] > iso(simdi):
                continue
            self.conn.execute("INSERT OR IGNORE INTO grup_ayarlari(group_id) VALUES(?)", (gid,))
            self._calisan.add(gid)
            gorevler.append(asyncio.ensure_future(self._grup_calis(gid, kaynak)))
        return gorevler

    async def tick_ve_bekle(self) -> list[list[TaramaSonucu]]:
        return list(await asyncio.gather(*self.tick()))

    async def _grup_calis(self, gid: int, kaynak) -> list[TaramaSonucu]:
        sonuclar: list[TaramaSonucu] = []
        reddedildi = False
        mt0 = self._token_deposu.son_degisiklik() if self._token_deposu is not None else None
        try:
            async with self._sem:
                for is_tipi, fn in (
                    ("partiler", self.takip.partiler_tara),  # önce üyelik, sonra oyuncular
                    ("oyuncular", self.takip.oyuncular_tara),
                    ("savaslar", self.takip.savaslari_tara),
                ):
                    if self._token_dur:
                        break
                    if not alan_aktif(self.conn, gid):
                        break  # alan artık aktif değil (yalnız testler: onay kaldırıldı): tarama durur
                    try:
                        sonuc = await fn(gid, kaynak)
                    except TokenGecersiz as e:
                        self._token_duraklat(getattr(e, "kusak", None))
                        sonuc = TaramaSonucu(is_tipi, hatalar=["token invalid"])
                    except ErisimReddedildi as e:
                        reddedildi = True
                        self.erisim_reddi_bildir()
                        if "403" not in self._yapilandirma_uyarildi:
                            self._yapilandirma_uyarildi.add("403")
                            log.warning("Diplomacia erişimi reddedildi (403); geri çekilme uygulanır, "
                                        "token geçersiz sayılmaz (kimlik biçimi veya site engeli olabilir)")
                        sonuc = TaramaSonucu(is_tipi, hatalar=[hata_aciklamasi(e)])
                    except IstekSiniri as e:
                        self._istek_siniri(e)
                        sonuc = TaramaSonucu(is_tipi, hatalar=["rate limit"])
                    except KaynakHatasi as e:
                        sonuc = TaramaSonucu(is_tipi, hatalar=[hata_aciklamasi(e)])
                    except Exception as e:  # beklenmeyen: tarama çökmesin, başarılı sayılmasın
                        log.exception("beklenmeyen tarama hatası (grup=%s, is=%s)", gid, is_tipi)
                        sonuc = TaramaSonucu(is_tipi, hatalar=[f"unexpected error: {type(e).__name__}"])
                    sonuclar.append(sonuc)
                    if sonuc.yapilandirma and is_tipi not in self._yapilandirma_uyarildi:
                        self._yapilandirma_uyarildi.add(is_tipi)
                        log.warning("%s taraması kullanılamıyor (yapılandırma/uygulama eksiği: %s); "
                                    "yeniden denenir, tekrar loglanmaz", is_tipi, "; ".join(sonuc.hatalar)[:120])
                    self._is_kaydet(gid, is_tipi, sonuc.ok, "; ".join(sonuc.hatalar))
                    if reddedildi:
                        break  # 403: kalan iş tipleri aynı istekle denenmez (geri çekilme uygulanır)
                    if not sonuc.ok and not sonuc.yapilandirma and (self._token_dur or self._engel_bitis is not None and self.saat() < self._engel_bitis):
                        break
        finally:
            self._calisan.discard(gid)
            # Yapılandırma/uygulama eksiği (örn. savaş şeması doğrulanmadı) geri çekilmeye SAYILMAZ:
            # diğer taramaların ritmi bozulmaz; iş yine de başarısız kaydedilir (son_basari değişmez).
            tam = len(sonuclar) == 3 and all(x.ok or x.yapilandirma for x in sonuclar)
            if tam:
                self._istek_siniri_sayac = 0  # tam başarılı tur: üstel bekleme sayacı sıfırlanır
            self._sonraki_planla(gid, tam)
            # Yeni token ilk başarılı GERÇEK yetkili taramayla teyit edildi: .prev gereksiz. Yalnız token dosyası
            # tarama boyunca DEĞİŞMEMİŞSE (eski tokenla başlayan tarama yeni tokenı teyit edemez).
            # Teyit için en az bir oyuncu/parti isteği BAŞARIYLA gözlenmiş olmalı: yalnız /api/wars başarısı yetmez
            # (o uç noktanın kimlik gerektirdiği canlıda doğrulanmadı).
            if (len(sonuclar) == 3 and all(x.ok and not x.yapilandirma for x in sonuclar)
                    and any(x.gozlenen > 0 for x in sonuclar)
                    and mt0 is not None and self._token_deposu is not None
                    and mt0 == self._token_deposu.son_degisiklik()):
                self._prev_sil("yeni token ilk başarılı yetkili taramayla teyit edildi")
        return sonuclar

    async def _kanal_kontrol(self) -> bool:
        """Log kanalı hazır mı (Telegram API ile doğrulanmış)? Doğrulayıcı yoksa (testler) hazır sayılır.
        Hazır olmaktan çıkınca/olunca kurucuya TEK özel bildirim gider (yağmur yok)."""
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
                    self.conn,
                    hedef_sohbet=self.kurucu_id,
                    grup_id=None,
                    tur="kanal_durumu",
                    idem_anahtari=f"kanal_durumu:{int(hazir)}:{iso(simdi)}",
                    veri={"hazir": hazir, "neden": neden[:200], "kanal_id": self.kanal_id, "zaman": iso(simdi)},
                    simdi=simdi,
                )
        except Exception:
            log.exception("kanal durum bildirimi kuyruğa alınamadı")

    async def calis(self, gonderici: outbox.Gonderici, durdur: asyncio.Event) -> None:
        """Üretim döngüsü. `durdur` kurulunca devam eden grup taramalarını bitirip çıkar."""
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
                ardisik = 0
                self.token_hatirlat()
            except Exception as e:  # tick istisnası döngüyü öldürmez; maskeli, izsiz log
                ardisik += 1
                log.error("tick hatası (art arda %d): %s", ardisik, maskele(f"{type(e).__name__}: {str(e)[:200]}"))
                bekleme = min(config.TICK_SECONDS * (2**ardisik), config.SCHEDULER_ERROR_BACKOFF_MAX_SECONDS)
            try:
                await outbox.bekleyenleri_gonder(
                    self.conn, gonderici, self.saat, izinli_hedefler={self.kurucu_id, self.kanal_id},
                    izinli_kapsamlar=self._aktif,
                )
            except Exception:
                log.exception("outbox gönderim turu hatası")
            try:
                await asyncio.wait_for(durdur.wait(), timeout=bekleme)
            except asyncio.TimeoutError:
                pass
        if gorevler:
            await asyncio.gather(*gorevler, return_exceptions=True)
