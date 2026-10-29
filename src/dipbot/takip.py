"""Takip mantığı: oyuncu, parti ve savaş. Kaynak olarak yalnız DiplomaciaSource kullanır.

Temel kurallar:
- Hiçbir DB transaction'ı `await` boyunca açık kalmaz: önce kaynaktan veri alınır, sonra
  kısa, senkron bir transaction içinde durum + outbox birlikte yazılır.
- Başarısız/eksik yanıt eski veriyi ezmez ve değişiklik sayılmaz.
- Her şey (group_id, ...) ile ayrıdır; gruplar arası durum paylaşılmaz.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from . import config, outbox
from .db import alan_aktif, islem
from .kaynak import (
    DiplomaciaSource,
    EksikVeri,
    IstekSiniri,
    KaynakHatasi,
    OyuncuAnlik,
    ErisimReddedildi,
    OzellikUygulanmadi,
    PartiAnlik,
    SavasDetay,
    SavasTaraf,
    TokenGecersiz,
    VarlikBulunamadi,
    hata_aciklamasi,
    metin_temizle,
)
from .zaman import Saat, iso, iso_coz, simdi_utc

log = logging.getLogger("dipbot.takip")

_OYUNCU_ALANLARI = ("ad", "bolge", "vatandaslik_ulkesi")


class GrupOnaysiz(Exception):
    """Grup onaylı değil (hiç onaylanmadı veya onayı kaldırıldı): değişiklik yapılmaz.

    KaynakHatasi DEĞİLDİR: kaynak hatası gibi yutulmamalı, çağıran açıkça ele almalıdır.
    """

    def __init__(self, group_id: int):
        super().__init__(f"group is not approved: {group_id}")
        self.group_id = group_id


# ---- Sonuç tipleri --------------------------------------------------------
@dataclass
class TaramaSonucu:
    is_tipi: str
    hatalar: list[str] = field(default_factory=list)
    uyarilar: list[str] = field(default_factory=list)
    degisiklik: int = 0  # üretilen bildirim sayısı
    eksik: int = 0  # bölge/vatandaşlık alanı boş/eksik gelen oyuncu sayısı (eski değer korundu)
    yapilandirma: bool = False  # hata geçici değil, kaynak özelliği uygulanmamış/doğrulanmamış
    istenen: int = 0  # isteği atılan kayıt sayısı (oyuncu/parti)
    gozlenen: int = 0  # başarıyla gözlenen kayıt sayısı (404/hata hariç)

    @property
    def ok(self) -> bool:
        """Yalnız hatasız tarama 'başarılı' kaydedilir."""
        return not self.hatalar


@dataclass(frozen=True)
class OyuncuEklemeSonucu:
    durum: str  # "eklendi" | "zaten_takipte"
    oyuncu_id: str
    ad: str | None = None
    bolge: str | None = None
    vatandaslik_ulkesi: str | None = None


@dataclass(frozen=True)
class PartiEklemeSonucu:
    durum: str  # "eklendi" | "zaten_takipte"
    parti_id: str
    ad: str | None = None
    uye_sayisi: int = 0
    yeni_eklenen: int = 0
    zaten_takipte: int = 0
    engelli: int = 0
    # Başlangıç özeti (YALNIZ komut yanıtında gösterilir; bildirim değildir):
    ilk_veri_alinan: int = 0  # ilk verisi (bölge/vatandaşlık/gözlem zamanı) komutta alınan yeni üye sayısı
    ilk_veri_bekleyen: int = 0  # ayrıntısı ilk taramaya kalan yeni üye sayısı
    bolge_bilinen: int = 0  # alınanlar arasında bölgesi dolu olan
    vatandaslik_bilinen: int = 0  # alınanlar arasında vatandaşlık ülkesi dolu olan
    ilk_veri_notu: str | None = None  # ayrıntı eksik kaldıysa kısa neden
    # Büyük parti: API tek istekte 100 üye döndürür -> KISMİ okuma (eksik liste; tam liste sayılmaz). Takip edilen oyuncu
    # sayısı sınırlanmaz: tek kriter XP süzgecidir.
    kismi: bool = False
    toplam_uye: int = 0  # partinin gerçek üye sayısı (member_count; bilinmiyorsa liste uzunluğu)
    takip_edilen: int = 0  # bu partiden şu an takipte olan oyuncu sayısı (yeni + zaten takipte; engelliler hariç)
    xp_sirali: bool | None = None  # yanıtta xp alanları azalan sıradaydı mı (None: doğrulanamadı)
    # XP süzgeci (>= config.PARTI_XP_ESIGI) — API'den İNCELENEN üyeler üzerinden:
    incelenen: int = 0  # API'den OKUNAN üye sayısı (kısmi okumada toplam_uye'den küçük)
    min_xp: int | None = None  # okunan üyeler arasındaki en düşük GEÇERLİ xp (yoksa None)
    xp_uygun: int = 0  # XP >= eşik
    xp_alti: int = 0  # geçerli XP < eşik
    xp_bilinmeyen: int = 0  # XP eksik/geçersiz (takibe alınmadı)
    zaten_nitelikli: int = 0  # XP'si uygun ve zaten takipte olanlar (takip_edilen = yeni + zaten_nitelikli)


@dataclass(frozen=True)
class KaldirmaOnizleme:
    basarili: bool
    parti_id: str
    neden: str | None = None  # başarısızsa Türkçe açıklama
    parti_ad: str | None = None
    uye_sayisi: int = 0
    etkilenen: tuple[str, ...] = ()  # takipten çıkarılacak oyuncu kimlikleri
    ozet_anahtari: str = ""  # onay butonuna gömülecek; üyelik değişirse değişir
    kismi: bool = False  # liste EKSİK okundu (tek istekte 100 üye): okunmayan üyeler doğrulanamadığı için ÇIKARILMAZ
    toplam_uye: int = 0


@dataclass(frozen=True)
class OyuncuOzeti:
    oyuncu_id: str
    ad: str | None = None


@dataclass(frozen=True)
class KaldirmaSonucu:
    durum: str  # "silindi" | "tekrar_onay" | "dogrulanamadi" | "bulunamadi"
    onizleme: KaldirmaOnizleme | None = None
    silinen: tuple[str, ...] = ()
    neden: str | None = None
    # Ayrıntı: takipten çıkarılanlar (ad+kimlik) ve bunlardan başka takipli partide
    # göründüğü için yeniden-ekleme ENGELİ konulanlar.
    silinen_oyuncular: tuple[OyuncuOzeti, ...] = ()
    engellenen: tuple[OyuncuOzeti, ...] = ()


@dataclass(frozen=True)
class SavasGorunumu:
    detay: SavasDetay | None
    guncel: bool  # False: eski önbellek verisi
    zaman: datetime | None  # verinin alındığı an
    hata: str | None = None


class TakipServisi:
    def __init__(self, conn: sqlite3.Connection, saat: Saat = simdi_utc, kanal_id: int | None = None):
        self.conn = conn
        self.saat = saat
        self._kanal_id = kanal_id

    @property
    def kanal_id(self) -> int:
        """Takip bildirimlerinin TEK hedefi: log kanalı (varsayılan config.LOG_KANAL_ID)."""
        return self._kanal_id if self._kanal_id is not None else config.LOG_KANAL_ID

    # ---- yardımcılar -----------------------------------------------------
    def _onay_sart(self, group_id: int) -> None:
        """Servis katmanı yetki sınırı: onaysız grup için hiçbir değişiklik yapılmaz."""
        if not alan_aktif(self.conn, group_id):
            raise GrupOnaysiz(group_id)

    @staticmethod
    def _oyuncu_temiz(snap: OyuncuAnlik, beklenen_id: str) -> OyuncuAnlik:
        """Oyuncu yanıtını TEK yerde normalize eder. Kimlik uyuşmazlığı veya boş ad: EksikVeri.
        Boş/boşluk bölge ve vatandaşlık None olur (= yanıtta yok; eski değer korunur)."""
        if snap.oyuncu_id != beklenen_id:
            raise EksikVeri("player response: ID mismatch")
        ad = metin_temizle(snap.ad)
        if ad is None:
            raise EksikVeri("player response: name is empty")
        return OyuncuAnlik(
            beklenen_id, ad, metin_temizle(snap.bolge), metin_temizle(snap.vatandaslik_ulkesi),
            metin_temizle(snap.uid),
        )

    def _takipte_mi(self, group_id: int, oyuncu_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (group_id, oyuncu_id)
            ).fetchone()
            is not None
        )

    def _uid_baska_kayit(self, group_id: int, uid: str | None, oyuncu_id: str) -> str | None:
        """Aynı grupta aynı UUID'nin BAŞKA oyuncu_id altında takibi varsa o numarayı döner (mükerrer önleme)."""
        if not uid:
            return None
        r = self.conn.execute(
            "SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=? AND uid=? AND oyuncu_id<>? LIMIT 1",
            (group_id, uid, oyuncu_id),
        ).fetchone()
        return r[0] if r else None

    def _onayli_mi(self, group_id: int) -> bool:
        return alan_aktif(self.conn, group_id)

    def _yetki_var_mi(self, islem_yapan: int | None, kurucu_id: int | None) -> bool:
        """GÜNCEL DB durumuyla yetki denetimi. islem_yapan None: eski çağrı biçimi (denetim yok)."""
        if islem_yapan is None:
            return True
        if kurucu_id is not None and islem_yapan == kurucu_id:
            return True
        return (
            self.conn.execute(
                "SELECT 1 FROM yetkili_kullanicilar WHERE user_id=?", (islem_yapan,)
            ).fetchone()
            is not None
        )

    def _grup_kuyruk(self, group_id, tur, idem, veri, simdi) -> bool:
        if not alan_aktif(self.conn, group_id):
            return False  # uçuştaki tarama sırasında onay kalktıysa bildirim üretilmez
        # Takip bildirimleri YALNIZ log kanalına gider; group_id yalnız takip LİSTESİNİN kimliğidir (onay dönemi,
        # idempotency anahtarı). Eski grup sohbetine hiçbir bildirim hedeflenmez.
        return outbox.kuyruga_ekle(
            self.conn,
            hedef_sohbet=self.kanal_id,
            grup_id=group_id,
            tur=tur,
            idem_anahtari=f"{tur}:{group_id}:{idem}",
            veri=veri,
            simdi=simdi,
        )

    def _partide_mi(self, group_id: int, oyuncu_id: str, haric_parti: str | None = None) -> bool:
        sql = "SELECT 1 FROM parti_uyeleri WHERE group_id=? AND oyuncu_id=?"
        args: list = [group_id, oyuncu_id]
        if haric_parti is not None:
            sql += " AND parti_id<>?"
            args.append(haric_parti)
        return self.conn.execute(sql, args).fetchone() is not None

    # ======================= OYUNCU =======================================
    async def oyuncu_ekle(self, group_id: int, oyuncu_id: str, kaynak: DiplomaciaSource) -> OyuncuEklemeSonucu:
        """Baseline ile ekler. Bildirim ÜRETMEZ (çağıran özeti komut yanıtında gösterir)."""
        self._onay_sart(group_id)
        oyuncu_id = str(oyuncu_id)
        if self._takipte_mi(group_id, oyuncu_id):
            # Kullanıcı bu oyuncuyu elle de eklemek istedi: otomatik (parti) kaydı "elle eklendi" olur ve
            # parti sıralamasından düşse bile takipten çıkarılmaz.
            with islem(self.conn):
                self.conn.execute(
                    "UPDATE takip_oyuncular SET elle_eklendi=1 WHERE group_id=? AND oyuncu_id=?", (group_id, oyuncu_id)
                )
            return OyuncuEklemeSonucu("zaten_takipte", oyuncu_id)
        snap = self._oyuncu_temiz(await kaynak.fetch_player(oyuncu_id), oyuncu_id)
        simdi = self.saat()
        with islem(self.conn):
            self._onay_sart(group_id)  # await sırasında onay kaldırılmış olabilir
            baska = self._uid_baska_kayit(group_id, snap.uid, oyuncu_id)
            if baska is not None:  # aynı kişi başka numara biçimiyle zaten takipte: mükerrer yok
                return OyuncuEklemeSonucu("zaten_takipte", baska)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO takip_oyuncular(group_id, oyuncu_id, ad, bolge,"
                " vatandaslik_ulkesi, son_gozlem, baseline_tamam, elle_eklendi, eklendi, uid)"
                " VALUES(?,?,?,?,?,?,1,1,?,?)",
                (group_id, oyuncu_id, snap.ad, snap.bolge, snap.vatandaslik_ulkesi, iso(simdi), iso(simdi),
                 snap.uid),
            )
            if cur.rowcount == 0:
                return OyuncuEklemeSonucu("zaten_takipte", oyuncu_id)
            # Kullanıcı elle yeniden eklerse yeniden-ekleme engeli kalkar.
            self.conn.execute("DELETE FROM engeller WHERE group_id=? AND oyuncu_id=?", (group_id, oyuncu_id))
        return OyuncuEklemeSonucu("eklendi", oyuncu_id, snap.ad, snap.bolge, snap.vatandaslik_ulkesi)

    def oyuncu_sil(self, group_id: int, oyuncu_id: str) -> bool:
        """Yalnız bu grubun takibinden çıkarır. Takip edilen bir partinin üyesiyse
        sessiz yeniden eklemeyi önlemek için engel kaydı bırakır."""
        self._onay_sart(group_id)
        oyuncu_id = str(oyuncu_id)
        simdi = self.saat()
        with islem(self.conn):
            cur = self.conn.execute(
                "DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (group_id, oyuncu_id)
            )
            if cur.rowcount == 0:
                return False
            self._mukerrer_engelini_kaldir(group_id, [oyuncu_id])
            if self._partide_mi(group_id, oyuncu_id):
                self._engel_koy(group_id, oyuncu_id, "elle_silindi", simdi)
            return True

    def _mukerrer_engelini_kaldir(self, group_id: int, silinenler) -> None:
        """Karşıdaki (asıl) kayıt silindi: onun yüzünden konmuş 'mukerrer_uid:<asıl>' engelleri kalkar."""
        for oid in silinenler:
            self.conn.execute(
                "DELETE FROM engeller WHERE group_id=? AND neden=?", (group_id, f"mukerrer_uid:{oid}")
            )

    def _uyelik_bildiriminden_cikar(self, group_id: int, oyuncu_id: str) -> None:
        """Mükerrer çıkan numara, henüz gönderilmemiş 'parti_uyelik' bildiriminde 'katılan' olarak
        LİSTELENMEZ; içerik boşalırsa bildirim iptal edilir. Gönderilmiş bildirim geri alınamaz."""
        for r in self.conn.execute(
            "SELECT id, veri FROM outbox WHERE tur='parti_uyelik' AND grup_id=? AND durum='bekliyor'", (group_id,)
        ).fetchall():
            v = json.loads(r["veri"])
            kalan = [k for k in v.get("katilanlar") or [] if k.get("oyuncu_id") != oyuncu_id]
            if len(kalan) == len(v.get("katilanlar") or []):
                continue
            if not kalan and not v.get("ayrilanlar"):
                self.conn.execute(
                    "UPDATE outbox SET durum='iptal', son_hata='mukerrer numara' WHERE id=? AND durum='bekliyor'",
                    (r["id"],),
                )
            else:
                v["katilanlar"] = kalan
                self.conn.execute(
                    "UPDATE outbox SET veri=? WHERE id=? AND durum='bekliyor'",
                    (json.dumps(v, ensure_ascii=False, sort_keys=True), r["id"]),
                )

    def _engel_koy(self, group_id, oyuncu_id, neden, simdi) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO engeller(group_id, oyuncu_id, neden, olusturuldu) VALUES(?,?,?,?)",
            (group_id, oyuncu_id, neden, iso(simdi)),
        )

    def _gozlem_uygula(self, group_id: int, snap) -> str:
        """Başarılı bir gözlemi uygular. Dönüş: 'yok' | 'baseline' | 'degisti' | 'ayni'."""
        simdi = self.saat()
        with islem(self.conn):
            if not self._onayli_mi(group_id):
                return "onaysiz"  # uçuştaki tarama sırasında onay kalktı: durum YAZILMAZ (yeniden onayda yutulmaz)
            r = self.conn.execute(
                "SELECT * FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (group_id, snap.oyuncu_id)
            ).fetchone()
            if r is None:
                return "yok"
            if snap.uid is not None:
                if r["uid"] is not None and r["uid"] != snap.uid:
                    # Aynı numara, farklı UUID: veri/kimlik uyuşmazlığı. Eski kayıt ezilmez, bildirim yok.
                    log.warning("oyuncu kimlik uyuşmazlığı (grup=%s, oyuncu=%s): kayıtlı UUID ile gelen farklı",
                                group_id, snap.oyuncu_id)
                    return "uid_uyusmazligi"
                if r["uid"] is None:
                    baska = self._uid_baska_kayit(group_id, snap.uid, snap.oyuncu_id)
                    if baska is not None:
                        # Aynı kişi başka numarayla zaten takipte (uid'siz otomatik ekleme mükerrer üretmişti).
                        if not r["elle_eklendi"]:
                            self.conn.execute(
                                "DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?",
                                (group_id, snap.oyuncu_id),
                            )
                            self._engel_koy(group_id, snap.oyuncu_id, f"mukerrer_uid:{baska}", simdi)
                            self._uyelik_bildiriminden_cikar(group_id, snap.oyuncu_id)
                            log.warning("mükerrer takip çözüldü (grup=%s): %s zaten %s olarak takipte",
                                        group_id, snap.oyuncu_id, baska)
                        else:
                            log.warning("mükerrer takip (grup=%s): %s ve %s aynı UUID; elle eklendiği için korundu",
                                        group_id, snap.oyuncu_id, baska)
                        return "mukerrer"
                if r["uid"] is None:
                    self.conn.execute(
                        "UPDATE takip_oyuncular SET uid=? WHERE group_id=? AND oyuncu_id=? AND uid IS NULL",
                        (snap.uid, group_id, snap.oyuncu_id),
                    )
            yeni_degerler = {a: getattr(snap, a) for a in _OYUNCU_ALANLARI}
            if not r["baseline_tamam"]:
                # İlk gözlem: sessizce kaydet.
                self.conn.execute(
                    "UPDATE takip_oyuncular SET ad=COALESCE(?,ad), bolge=COALESCE(?,bolge),"
                    " vatandaslik_ulkesi=COALESCE(?,vatandaslik_ulkesi), son_gozlem=?,"
                    " baseline_tamam=1 WHERE group_id=? AND oyuncu_id=?",
                    (*yeni_degerler.values(), iso(simdi), group_id, snap.oyuncu_id),
                )
                return "baseline"
            degisimler = []
            guncel = {}
            for alan in _OYUNCU_ALANLARI:
                yeni = yeni_degerler[alan]
                eski = r[alan]
                if yeni is None:
                    continue  # alan bu yanıtta yok: değişiklik değildir, eski değer korunur
                guncel[alan] = yeni
                if eski is not None and yeni != eski:
                    degisimler.append({"alan": alan, "eski": eski, "yeni": yeni})
            sets = ", ".join(f"{a}=?" for a in guncel)
            self.conn.execute(
                f"UPDATE takip_oyuncular SET {sets + ', ' if sets else ''}son_gozlem=?"
                " WHERE group_id=? AND oyuncu_id=?",
                (*guncel.values(), iso(simdi), group_id, snap.oyuncu_id),
            )
            if not degisimler:
                return "ayni"
            ozet = hashlib.sha1(json.dumps(degisimler, sort_keys=True).encode()).hexdigest()[:12]
            self._grup_kuyruk(
                group_id,
                "oyuncu_degisti",
                f"{snap.oyuncu_id}:{iso(simdi)}:{ozet}",
                {
                    "oyuncu_id": snap.oyuncu_id,
                    "ad": guncel.get("ad", r["ad"]),
                    "degisimler": degisimler,
                    "tespit": iso(simdi),
                },
                simdi,
            )
            return "degisti"

    async def oyuncular_tara(self, group_id: int, kaynak: DiplomaciaSource) -> TaramaSonucu:
        sonuc = TaramaSonucu("oyuncular")
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT oyuncu_id FROM takip_oyuncular WHERE group_id=? ORDER BY oyuncu_id", (group_id,)
            )
        ]
        bulunamayan: list[str] = []
        gozlenen: list[str] = []
        for pid in ids:
            sonuc.istenen += 1
            try:
                snap = self._oyuncu_temiz(await kaynak.fetch_player(pid), pid)
            except (TokenGecersiz, IstekSiniri, ErisimReddedildi):
                raise  # tüm taramayı ilgilendirir; zamanlayıcı karar verir
            except VarlikBulunamadi:
                bulunamayan.append(pid)
                sonuc.uyarilar.append(f"{pid}: not found at the source (record was not deleted)")
            except OzellikUygulanmadi as e:  # token yok / kaynak yapılandırılmadı: tek hata, geri çekilme yok
                sonuc.hatalar.append(hata_aciklamasi(e))
                sonuc.yapilandirma = True
                return sonuc
            except KaynakHatasi as e:
                sonuc.hatalar.append(f"{pid}: {hata_aciklamasi(e)}")
            else:
                sonuc.gozlenen += 1
                gozlenen.append(pid)
                if snap.bolge is None or snap.vatandaslik_ulkesi is None:
                    sonuc.eksik += 1
                    sonuc.uyarilar.append(f"{pid}: region/citizenship came back missing (old value kept)")
                durum = self._gozlem_uygula(group_id, snap)
                if durum == "degisti":
                    sonuc.degisiklik += 1
                elif durum == "uid_uyusmazligi":
                    sonuc.hatalar.append(f"{pid}: ID mismatch (same number, different UUID; record not changed)")
        if sonuc.istenen and not sonuc.gozlenen and not sonuc.hatalar:
            # İstenen hiçbir oyuncu gözlenemedi (hepsi 404): doğrulanmış tarama DEĞİL, başarılı kaydedilmez.
            sonuc.hatalar.append(f"no player could be observed ({len(bulunamayan)} records not found)")
        elif sonuc.gozlenen:
            self._bulunamadi_say(group_id, "oyuncu", bulunamayan, gozlenen)
        return sonuc

    # ---- art arda 404 işareti (otomatik silme YOK) -----------------------
    def _bulunamadi_say(self, group_id: int, tur: str, bulunamayan: list[str], gozlenen: list[str]) -> None:
        """Yalnız taramada en az bir kayıt başarıyla gözlendiyse (site çalışıyor) sayaçları günceller."""
        with islem(self.conn):
            if not self._onayli_mi(group_id):
                return
            for k in gozlenen:
                self.conn.execute(
                    "DELETE FROM bulunamadi_sayaclari WHERE group_id=? AND tur=? AND kimlik=?",
                    (group_id, tur, k),
                )
            for k in bulunamayan:
                self.conn.execute(
                    "INSERT INTO bulunamadi_sayaclari(group_id, tur, kimlik, sayac) VALUES(?,?,?,1)"
                    " ON CONFLICT(group_id, tur, kimlik) DO UPDATE SET sayac=sayac+1",
                    (group_id, tur, k),
                )

    def bulunamayan_oyuncular(self, group_id: int) -> list[str]:
        """Art arda config.NOT_FOUND_STREAK_THRESHOLD taramada bulunamayan, hâlâ takipteki oyuncular."""
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT b.kimlik FROM bulunamadi_sayaclari b JOIN takip_oyuncular t"
                " ON t.group_id=b.group_id AND t.oyuncu_id=b.kimlik"
                " WHERE b.group_id=? AND b.tur='oyuncu' AND b.sayac>=? ORDER BY b.kimlik",
                (group_id, config.NOT_FOUND_STREAK_THRESHOLD),
            )
        ]

    # ======================= PARTİ ========================================
    @staticmethod
    def _tam_liste(snap: PartiAnlik, parti_id: str) -> None:
        if snap.parti_id != parti_id:
            raise EksikVeri("party ID mismatch")
        if not snap.uyeler_tamam:
            # SINIRLI (ilk N) liste yalnız tutarlıysa kabul edilir: toplam bilinmeli ve listeden büyük olmalı.
            # Bozuk/yinelenen/sayfalamalı/eksik yanıtlar adaptörde kismi=False kalır ve burada reddedilir.
            if not snap.kismi or snap.toplam_uye is None or len(snap.uyeler) >= snap.toplam_uye:
                raise EksikVeri("member list pagination was not completed")
        ids = [u.oyuncu_id for u in snap.uyeler]
        if any(not i for i in ids) or len(set(ids)) != len(ids):
            raise EksikVeri("member list is malformed")

    async def parti_ekle(self, group_id: int, parti_id: str, kaynak: DiplomaciaSource) -> PartiEklemeSonucu:
        self._onay_sart(group_id)
        parti_id = str(parti_id)
        if self.conn.execute(
            "SELECT 1 FROM takip_partiler WHERE group_id=? AND parti_id=?", (group_id, parti_id)
        ).fetchone():
            return PartiEklemeSonucu("zaten_takipte", parti_id)
        snap = await kaynak.fetch_party(parti_id)
        self._tam_liste(snap, parti_id)
        if not snap.ad:
            raise EksikVeri("party name is missing")
        # Yeni üyelerin ilk verisi (bölge, vatandaşlık, uid) SINIRLI eşzamanlılık, üst sınır ve süre bütçesiyle
        # alınır (büyük parti/hız sınırı komutu bloke etmez). Alınamayanlar baseline_tamam=0 ile eklenir ve
        # ilk taramada bildirimsiz tamamlanır. Transaction'dan ÖNCE yapılır (await boyunca DB açık kalmaz).
        esik = config.PARTI_XP_ESIGI
        # Her üyenin XP'si AYRI denetlenir (API sıralamasına güvenilmez; erken kesme yok). Eksik/geçersiz XP =
        # bilinmiyor -> otomatik takibe alınmaz. Yalnız nitelikli YENİ üyelerin ilk verisi istenir.
        nitelikli_mi = {u.oyuncu_id: (u.xp is not None and u.xp >= esik) for u in snap.uyeler}
        xp_alti = sum(1 for u in snap.uyeler if u.xp is not None and u.xp < esik)
        xp_bilinmeyen = sum(1 for u in snap.uyeler if u.xp is None)
        adaylar = [
            u.oyuncu_id for u in snap.uyeler
            if nitelikli_mi[u.oyuncu_id]
            and not self._takipte_mi(group_id, u.oyuncu_id) and not self._engelli_mi(group_id, u.oyuncu_id)
        ]
        detay, ilk_not = await self._uye_ilk_verileri(adaylar, kaynak)
        simdi = self.saat()
        yeni = zaten = engelli = alinan = bolge_b = vatandas_b = zaten_n = 0
        with islem(self.conn):
            self._onay_sart(group_id)  # await sırasında onay kaldırılmış olabilir
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO takip_partiler(group_id, parti_id, ad, baseline_tamam,"
                " son_dogrulama, eklendi, kismi, toplam_uye, xp_sirali) VALUES(?,?,?,1,?,?,?,?,?)",
                (group_id, parti_id, snap.ad, iso(simdi), iso(simdi), int(bool(snap.kismi)),
                 snap.toplam_uye if snap.toplam_uye is not None else len(snap.uyeler),
                 None if snap.xp_sirali is None else int(snap.xp_sirali)),
            )
            if cur.rowcount == 0:
                return PartiEklemeSonucu("zaten_takipte", parti_id)
            self.conn.executemany(
                "INSERT INTO parti_uyeleri(group_id, parti_id, oyuncu_id, ad, xp) VALUES(?,?,?,?,?)",
                [(group_id, parti_id, u.oyuncu_id, u.ad, u.xp) for u in snap.uyeler],
            )
            for u in snap.uyeler:
                if self._takipte_mi(group_id, u.oyuncu_id):
                    zaten += 1
                    if nitelikli_mi[u.oyuncu_id]:
                        zaten_n += 1
                        self._xp_onayla(group_id, u.oyuncu_id)
                elif not nitelikli_mi[u.oyuncu_id]:
                    continue  # XP eşiğin altında veya bilinmiyor: otomatik takibe alınmaz
                elif self._engelli_mi(group_id, u.oyuncu_id):
                    engelli += 1
                else:
                    p = detay.get(u.oyuncu_id)
                    if self._otomatik_ekle(group_id, u.oyuncu_id, u.ad, p, simdi):
                        yeni += 1
                        if p is not None:
                            alinan += 1
                            bolge_b += p.bolge is not None
                            vatandas_b += p.vatandaslik_ulkesi is not None
        return PartiEklemeSonucu(
            "eklendi", parti_id, snap.ad, len(snap.uyeler), yeni, zaten, engelli,
            ilk_veri_alinan=alinan, ilk_veri_bekleyen=yeni - alinan, bolge_bilinen=bolge_b,
            vatandaslik_bilinen=vatandas_b, ilk_veri_notu=ilk_not if yeni > alinan else None,
            kismi=bool(snap.kismi),
            toplam_uye=snap.toplam_uye if snap.toplam_uye is not None else len(snap.uyeler),
            takip_edilen=yeni + zaten_n, xp_sirali=snap.xp_sirali, incelenen=len(snap.uyeler),
            xp_uygun=sum(nitelikli_mi.values()), xp_alti=xp_alti, xp_bilinmeyen=xp_bilinmeyen, zaten_nitelikli=zaten_n,
            min_xp=min((u.xp for u in snap.uyeler if u.xp is not None), default=None),
        )

    async def _uye_ilk_verileri(self, ids: list[str], kaynak: DiplomaciaSource) -> tuple[dict, str | None]:
        """Yeni üyelerin oyuncu verisini alır: en çok PARTI_EKLE_ILK_VERI_UST üye, PARTI_EKLE_ESZAMANLI eşzamanlılık,
        PARTI_EKLE_ILK_VERI_SURE_SN toplam süre. Token/istek sınırı/erişim reddi veya süre dolunca kalan istekler
        ATILMAZ. Hiçbir hata yükseltilmez: alınamayan üye ilk taramaya kalır. Dönüş: ({id: OyuncuAnlik}, neden)."""
        if not ids:
            return {}, None
        hedef = ids[: config.PARTI_EKLE_ILK_VERI_UST]
        sonuc: dict[str, OyuncuAnlik] = {}
        durum = {"dur": None}
        sem = asyncio.Semaphore(max(1, config.PARTI_EKLE_ESZAMANLI))

        async def al(oid: str) -> None:
            async with sem:
                if durum["dur"]:
                    return
                try:
                    sonuc[oid] = self._oyuncu_temiz(await kaynak.fetch_player(oid), oid)
                except TokenGecersiz:
                    durum["dur"] = "token problem"
                except IstekSiniri:
                    durum["dur"] = "rate limit"
                except ErisimReddedildi:
                    durum["dur"] = "access denied"
                except (KaynakHatasi, EksikVeri):
                    pass  # bu üye ilk taramaya kalır
                except Exception:  # beklenmeyen: komutu düşürme, üye ilk taramaya kalsın (ayrıntı günlükte, sır yok)
                    log.exception("parti üyesi ilk verisi alınırken beklenmeyen hata (oyuncu=%s)", oid)

        gorevler = [asyncio.ensure_future(al(o)) for o in hedef]
        try:
            _, bekleyen = await asyncio.wait(gorevler, timeout=config.PARTI_EKLE_ILK_VERI_SURE_SN)
        except BaseException:
            for g in gorevler:
                g.cancel()
            raise
        for g in bekleyen:
            g.cancel()
        if bekleyen:
            await asyncio.gather(*bekleyen, return_exceptions=True)
            durum["dur"] = durum["dur"] or "time budget exhausted"
        if durum["dur"] is None and len(ids) > len(hedef):
            durum["dur"] = f"at most {config.PARTI_EKLE_ILK_VERI_UST} members' data is requested per command"
        return sonuc, durum["dur"]

    def _engelli_mi(self, group_id, oyuncu_id) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM engeller WHERE group_id=? AND oyuncu_id=?", (group_id, oyuncu_id)
            ).fetchone()
            is not None
        )

    def _otomatik_ekle(self, group_id, oyuncu_id, ad, p, simdi) -> bool:
        """Partiden otomatik ekleme. Bildirim yok; p varsa baseline hazır, yoksa ilk gözlem bekler.
        Dönüş: eklendi mi (aynı UUID başka numarayla zaten takipteyse False: mükerrer yok)."""
        if p is not None and self._uid_baska_kayit(group_id, p.uid, oyuncu_id) is not None:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO takip_oyuncular(group_id, oyuncu_id, ad, bolge, vatandaslik_ulkesi,"
            " son_gozlem, baseline_tamam, elle_eklendi, eklendi, uid, xp_onayli) VALUES(?,?,?,?,?,?,?,0,?,?,1)",
            (
                group_id,
                oyuncu_id,
                (p.ad if p and p.ad else ad),
                p.bolge if p else None,
                p.vatandaslik_ulkesi if p else None,
                iso(simdi) if p else None,
                1 if p else 0,
                iso(simdi),
                p.uid if p else None,
            ),
        )
        return cur.rowcount == 1

    def _xp_onayla(self, group_id: int, oyuncu_id: str) -> None:
        """Oyuncu XP süzgecini geçti (xp >= eşik): kayıt XP kuralına tabi olur, art arda eşik-altı sayacı sıfırlanır."""
        self.conn.execute(
            "UPDATE takip_oyuncular SET xp_onayli=1, xp_alti_sayac=0 WHERE group_id=? AND oyuncu_id=?"
            " AND (xp_onayli=0 OR xp_alti_sayac<>0)",
            (group_id, oyuncu_id),
        )

    def _xp_alti_gozle(self, group_id: int, oyuncu_id: str) -> None:
        """Geçerli XP eşiğin ALTINDA gözlendi. YALNIZ şu oyuncu kademeli çıkarılır: otomatik eklenmiş (elle eklenmemiş),
        XP süzgecinden bir kez geçmiş (xp_onayli=1; eski kayıtlar korunur) oyuncu. (Bir oyuncu aynı anda iki partide olamaz;
        'başka partide nitelikli' koruması YOKTUR. Gecikmiş eski liste sayacı yalnız artırabilir, nitelikli gözlem sıfırlar.)
        Art arda config.PARTI_XP_ALTI_ARDISIK_TARAMA gözlemde silinir; tek geçici yanıt silmez.
        Engel konmaz (eşiğe sonradan ulaşırsa sessizce yeniden alınır)."""
        r = self.conn.execute(
            "SELECT elle_eklendi, xp_onayli, xp_alti_sayac FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?",
            (group_id, oyuncu_id),
        ).fetchone()
        if r is None or r["elle_eklendi"] or not r["xp_onayli"]:
            return
        sayac = r["xp_alti_sayac"] + 1
        if sayac >= config.PARTI_XP_ALTI_ARDISIK_TARAMA:
            self.conn.execute("DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (group_id, oyuncu_id))
            self._mukerrer_engelini_kaldir(group_id, [oyuncu_id])
            log.info("oyuncu XP eşiğinin altında kaldığı için otomatik takipten çıkarıldı (grup=%s, oyuncu=%s)",
                     group_id, oyuncu_id)
        else:
            self.conn.execute(
                "UPDATE takip_oyuncular SET xp_alti_sayac=? WHERE group_id=? AND oyuncu_id=?",
                (sayac, group_id, oyuncu_id),
            )

    def _parti_uygula(self, group_id: int, parti_id: str, snap: PartiAnlik) -> int:
        """Doğrulanmış (tam) üye listesini uygular. Dönüş: üretilen bildirim sayısı."""
        simdi = self.saat()
        bildirim = 0
        with islem(self.conn):
            if not self._onayli_mi(group_id):
                return 0  # uçuştaki tarama sırasında onay kalktı: durum YAZILMAZ
            p = self.conn.execute(
                "SELECT * FROM takip_partiler WHERE group_id=? AND parti_id=?", (group_id, parti_id)
            ).fetchone()
            if p is None:
                return 0
            eski = {
                r["oyuncu_id"]: r["ad"]
                for r in self.conn.execute(
                    "SELECT oyuncu_id, ad FROM parti_uyeleri WHERE group_id=? AND parti_id=?",
                    (group_id, parti_id),
                )
            }
            yeni = {u.oyuncu_id: u.ad for u in snap.uyeler}
            if not yeni and eski:
                raise EksikVeri("empty member list is suspicious; membership change not counted")
            xp_de = {u.oyuncu_id: u.xp for u in snap.uyeler}
            esik = config.PARTI_XP_ESIGI
            fark_katilan = [i for i in yeni if i not in eski]
            fark_ayrilan = [i for i in eski if i not in yeni]
            kismi = bool(snap.kismi)
            eski_kismi = bool(p["kismi"])
            # GERÇEK üyelik değişikliği yalnız İKİ TARAF DA TAM listeyse bilinir. Kısmi (eksik) okunan listede bir
            # oyuncunun sıralamadan düşmesi/girmesi parti üyeliği değişikliği DEĞİLDİR: bildirim yok.
            # Sınırlıdan tam listeye geçişte yeni listede OLMAYANLAR doğrulanmış ayrılmadır (tam liste), ama
            # yeni görünenlerin gerçek katılım olduğu doğrulanamaz (önceki liste kısmiydi): katılım bildirimi yok.
            if kismi:
                katilan = ayrilan = []
            elif eski_kismi:
                katilan, ayrilan = [], fark_ayrilan
            else:
                katilan, ayrilan = fark_katilan, fark_ayrilan

            self.conn.execute("DELETE FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (group_id, parti_id))
            # Bildirimde yalnız TAKİP İLE İLGİLİ olanlar yer alır: katılan -> XP'si uygun (otomatik takibe alınacak);
            # ayrılan -> o an takipte olan. Takip dışı (düşük/bilinmeyen XP) üyelerin giriş/çıkışı bildirilmez.
            katilan_bildirim = [i for i in katilan if xp_de.get(i) is not None and xp_de[i] >= esik]
            ayrilan_bildirim = [i for i in ayrilan if self._takipte_mi(group_id, i)]
            self.conn.executemany(
                "INSERT INTO parti_uyeleri(group_id, parti_id, oyuncu_id, ad, xp) VALUES(?,?,?,?,?)",
                [(group_id, parti_id, i, ad, xp_de.get(i)) for i, ad in yeni.items()],
            )
            self.conn.execute(
                "UPDATE takip_partiler SET ad=COALESCE(?,ad), son_dogrulama=?, baseline_tamam=1, kismi=?,"
                " toplam_uye=?, xp_sirali=? WHERE group_id=? AND parti_id=?",
                (snap.ad, iso(simdi), int(kismi),
                 snap.toplam_uye if snap.toplam_uye is not None else len(snap.uyeler),
                 None if snap.xp_sirali is None else int(snap.xp_sirali), group_id, parti_id),
            )
            # Takip uzlaştırma — HER üyenin XP'si ayrı denetlenir (sıralamaya güvenilmez):
            #   xp >= eşik  : takipte olmalı (engelli olanlar yalnız doğrulanmış gerçek katılımda döner); sayaç sıfırlanır
            #   xp <  eşik  : otomatik takipliyse kademeli çıkarma sayacı işler (aşağıda)
            #   xp bilinmez : ne eklenir ne sayılır
            # Listeden DÜŞME (kısmi okumada görünmeme / partiden ayrılma) XP kanıtı değildir: kimse silinmez.
            for oid, ad in yeni.items():
                xp = xp_de.get(oid)
                if xp is None:
                    continue
                if xp < esik:
                    self._xp_alti_gozle(group_id, oid)
                    continue
                if self._takipte_mi(group_id, oid):
                    if oid in katilan:
                        self.conn.execute("DELETE FROM engeller WHERE group_id=? AND oyuncu_id=?", (group_id, oid))
                    self._xp_onayla(group_id, oid)
                    continue
                if self._engelli_mi(group_id, oid) and oid not in katilan:
                    continue  # sessiz yeniden ekleme YOK
                self.conn.execute("DELETE FROM engeller WHERE group_id=? AND oyuncu_id=?", (group_id, oid))
                self._otomatik_ekle(group_id, oid, ad, None, simdi)

            if snap.ad and p["ad"] and snap.ad != p["ad"]:
                bildirim += self._grup_kuyruk(
                    group_id,
                    "parti_ad_degisti",
                    f"{parti_id}:{iso(simdi)}",
                    {"parti_id": parti_id, "eski_ad": p["ad"], "yeni_ad": snap.ad, "tespit": iso(simdi)},
                    simdi,
                )
            if katilan_bildirim or ayrilan_bildirim:
                bildirim += self._grup_kuyruk(
                    group_id,
                    "parti_uyelik",
                    f"{parti_id}:{iso(simdi)}",
                    {
                        "parti_id": parti_id,
                        "parti_ad": snap.ad or p["ad"],
                        "katilanlar": [{"oyuncu_id": i, "ad": yeni[i]} for i in katilan_bildirim],
                        "ayrilanlar": [{"oyuncu_id": i, "ad": eski[i]} for i in ayrilan_bildirim],
                        "tespit": iso(simdi),
                    },
                    simdi,
                )
        return bildirim

    async def partiler_tara(self, group_id: int, kaynak: DiplomaciaSource) -> TaramaSonucu:
        sonuc = TaramaSonucu("partiler")
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT parti_id FROM takip_partiler WHERE group_id=? ORDER BY parti_id", (group_id,)
            )
        ]
        bulunamayan = 0
        for tid in ids:
            sonuc.istenen += 1
            try:
                snap = await kaynak.fetch_party(tid)
                self._tam_liste(snap, tid)
                sonuc.degisiklik += self._parti_uygula(group_id, tid, snap)
                sonuc.gozlenen += 1
            except (TokenGecersiz, IstekSiniri, ErisimReddedildi):
                raise
            except VarlikBulunamadi:
                bulunamayan += 1
                sonuc.uyarilar.append(f"{tid}: not found at the source (record was not deleted)")
            except OzellikUygulanmadi as e:
                sonuc.hatalar.append(hata_aciklamasi(e))
                sonuc.yapilandirma = True
                return sonuc
            except KaynakHatasi as e:
                sonuc.hatalar.append(f"{tid}: {hata_aciklamasi(e)}")
        if sonuc.istenen and not sonuc.gozlenen and not sonuc.hatalar:
            sonuc.hatalar.append(f"no party could be observed ({bulunamayan} records not found)")
        return sonuc

    # ---- parti kaldırma (iki adımlı) -------------------------------------
    def _parti_takipte(self, group_id, parti_id) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM takip_partiler WHERE group_id=? AND parti_id=?", (group_id, parti_id)
        ).fetchone()

    async def parti_kaldirma_hazirla(
        self, group_id: int, parti_id: str, kaynak: DiplomaciaSource
    ) -> KaldirmaOnizleme:
        """Güncel üyeleri doğrular ve etkilenecek oyuncuları döner. HİÇBİR ŞEY değiştirmez."""
        self._onay_sart(group_id)
        parti_id = str(parti_id)
        p = self._parti_takipte(group_id, parti_id)
        if p is None:
            return KaldirmaOnizleme(False, parti_id, neden="party is not tracked in this group")
        try:
            snap = await kaynak.fetch_party(parti_id)
            self._tam_liste(snap, parti_id)
        except KaynakHatasi as e:
            return KaldirmaOnizleme(
                False,
                parti_id,
                neden=f"current member list could not be verified ({hata_aciklamasi(e)}); no players were removed",
            )
        eski_sayi = self.conn.execute(
            "SELECT COUNT(*) FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (group_id, parti_id)
        ).fetchone()[0]
        if not snap.uyeler and eski_sayi:
            return KaldirmaOnizleme(
                False, parti_id, neden="current member list came back empty, which is suspicious; no players were removed"
            )
        guncel = sorted(u.oyuncu_id for u in snap.uyeler)
        etkilenen = tuple(i for i in guncel if self._takipte_mi(group_id, i))
        anahtar = hashlib.sha256(
            json.dumps([guncel, list(etkilenen)]).encode()
        ).hexdigest()[:16]
        return KaldirmaOnizleme(
            True, parti_id, None, snap.ad or p["ad"], len(guncel), etkilenen, anahtar,
            kismi=bool(snap.kismi), toplam_uye=snap.toplam_uye if snap.toplam_uye is not None else len(guncel),
        )

    async def parti_kaldirma_onayla(
        self,
        group_id: int,
        parti_id: str,
        kaynak: DiplomaciaSource,
        onaylanan_anahtar: str,
        islem_yapan: int | None = None,
        kurucu_id: int | None = None,
    ) -> KaldirmaSonucu:
        """Onay anında üyeliği YENİDEN doğrular. Özet değiştiyse silmez, yeni özeti döner.

        islem_yapan verilirse yetki (kurucu/yetkili) await öncesi VE transaction İÇİNDE güncel DB
        durumuyla yeniden denetlenir; yetkisi alınmışsa durum 'yetkisiz' döner, hiçbir şey silinmez."""
        yetkisiz = KaldirmaSonucu("yetkisiz", neden="the acting user is no longer authorized; no players were removed")
        self._onay_sart(group_id)
        if not self._yetki_var_mi(islem_yapan, kurucu_id):
            return yetkisiz
        parti_id = str(parti_id)
        if self._parti_takipte(group_id, parti_id) is None:
            return KaldirmaSonucu("bulunamadi", neden="party is not tracked in this group")
        onizleme = await self.parti_kaldirma_hazirla(group_id, parti_id, kaynak)
        if not onizleme.basarili:
            return KaldirmaSonucu("dogrulanamadi", onizleme, neden=onizleme.neden)
        if onizleme.ozet_anahtari != onaylanan_anahtar:
            return KaldirmaSonucu("tekrar_onay", onizleme, neden="membership changed; please confirm the updated summary")
        simdi = self.saat()
        with islem(self.conn):
            self._onay_sart(group_id)  # hazirla'daki await sırasında onay kaldırılmış olabilir
            if not self._yetki_var_mi(islem_yapan, kurucu_id):
                return yetkisiz  # await sırasında yetkisi alınmış olabilir
            if self._parti_takipte(group_id, parti_id) is None:
                return KaldirmaSonucu("bulunamadi", neden="party is not tracked in this group")
            silinen = []
            adlar: dict[str, str | None] = {}
            for oid in onizleme.etkilenen:  # yalnız o anki üyeler; elle eklenmiş olsa da
                satir = self.conn.execute(
                    "SELECT ad FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (group_id, oid)
                ).fetchone()
                adlar[oid] = satir["ad"] if satir else None
                cur = self.conn.execute(
                    "DELETE FROM takip_oyuncular WHERE group_id=? AND oyuncu_id=?", (group_id, oid)
                )
                if cur.rowcount:
                    silinen.append(oid)
            self._mukerrer_engelini_kaldir(group_id, silinen)
            self.conn.execute("DELETE FROM parti_uyeleri WHERE group_id=? AND parti_id=?", (group_id, parti_id))
            self.conn.execute("DELETE FROM takip_partiler WHERE group_id=? AND parti_id=?", (group_id, parti_id))
            # Çatışma: silinen oyuncu hâlâ başka takipteki bir partinin listesinde → engel.
            engellenen = []
            for oid in silinen:
                if self._partide_mi(group_id, oid):
                    self._engel_koy(group_id, oid, "parti_kaldirildi", simdi)
                    engellenen.append(OyuncuOzeti(oid, adlar.get(oid)))
        return KaldirmaSonucu(
            "silindi",
            onizleme,
            tuple(silinen),
            silinen_oyuncular=tuple(OyuncuOzeti(o, adlar.get(o)) for o in silinen),
            engellenen=tuple(engellenen),
        )

    # ======================= SAVAŞ ========================================
    async def savaslari_tara(self, group_id: int, kaynak: DiplomaciaSource) -> TaramaSonucu:
        """İlk etkinleştirmede mevcut savaşlar bildirimsiz baseline olur. Hasar bildirimi YOK.

        Liste `tamam=False` ise, hata verirse, kimlikleri bozuksa veya (baseline öncesi) boşsa
        baseline TAMAMLANMAZ ve bildirim üretilmez. Boş-ama-tam liste baseline sonrası zararsızdır
        (savaş kayıtları silinmez)."""
        sonuc = TaramaSonucu("savaslar")
        try:
            liste = await kaynak.list_active_wars()
            savaslar = liste.savaslar
        except (TokenGecersiz, IstekSiniri, ErisimReddedildi):
            raise
        except OzellikUygulanmadi as e:
            sonuc.hatalar.append(str(e)[:120] or "war support is not implemented")
            sonuc.yapilandirma = True
            return sonuc
        except KaynakHatasi as e:
            sonuc.hatalar.append(hata_aciklamasi(e))
            return sonuc
        if not liste.tamam:
            sonuc.hatalar.append("war list is incomplete (" + hata_aciklamasi(EksikVeri()) + ")")
            return sonuc  # eksik liste: ne baseline ne bildirim
        if any(not metin_temizle(s.savas_id) for s in savaslar) or len({s.savas_id for s in savaslar}) != len(savaslar):
            sonuc.hatalar.append("war list is malformed (empty or duplicate ID)")
            return sonuc
        simdi = self.saat()
        with islem(self.conn):
            if not self._onayli_mi(group_id):
                return sonuc  # uçuştaki tarama sırasında onay kalktı: kayıt/baseline YAZILMAZ
            r = self.conn.execute(
                "SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (group_id,)
            ).fetchone()
            baseline_tamam = bool(r and r[0])
            if not savaslar and not baseline_tamam:
                # İlk baseline'da boş liste şüpheli: tamamlanmaz; sonraki dolu liste de bildirimsiz oturur.
                sonuc.uyarilar.append("empty war list is suspicious; baseline not completed")
                return sonuc
            yeniler = []
            taninmayan = 0
            for s in savaslar:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO savaslar(group_id, savas_id, ad, ilk_gorulme) VALUES(?,?,?,?)",
                    (group_id, s.savas_id, s.ad, iso(simdi)),
                )
                if cur.rowcount != 1:
                    continue
                if s.durum_taninmiyor or s.tur_taninmiyor:
                    # 'ended' gibi tanınmayan durumlar yeni savaş sayılmaz: kayıt var, bildirim yok.
                    taninmayan += 1
                elif baseline_tamam:
                    yeniler.append(s)
            # Sayaç kümülatif değil: bu taramada listede şu an tanınmayan durum/türlü savaş sayısı.
            guncel_taninmayan = sum(1 for s in savaslar if s.durum_taninmiyor or s.tur_taninmiyor)
            self.conn.execute(
                "INSERT INTO sistem_durumu(anahtar, deger) VALUES(?, ?)"
                " ON CONFLICT(anahtar) DO UPDATE SET deger=excluded.deger",
                (f"savas_taninmayan:{group_id}", str(guncel_taninmayan)),
            )
            if taninmayan:
                log.warning("tanınmayan durum/türde %d yeni savaş kaydedildi (bildirim yok)", taninmayan)
                sonuc.uyarilar.append(f"{taninmayan} new war(s) with an unrecognized status/type (not notified)")
            if len(yeniler) > config.YENI_SAVAS_BILDIRIM_UST:
                sonuc.degisiklik += self._grup_kuyruk(
                    group_id,
                    "yeni_savas_ozet",
                    f"{iso(simdi)}:{len(yeniler)}",
                    {
                        "sayi": len(yeniler),
                        "savaslar": [
                            {"savas_id": s.savas_id, "ad": s.ad, "war_number": s.war_number}
                            for s in yeniler[: config.YENI_SAVAS_BILDIRIM_UST]
                        ],
                        "tespit": iso(simdi),
                    },
                    simdi,
                )
                yeniler = []
            for s in yeniler:
                sonuc.degisiklik += self._grup_kuyruk(
                    group_id,
                    "yeni_savas",
                    s.savas_id,
                    {
                        "savas_id": s.savas_id,
                        "ad": s.ad,
                        "war_number": s.war_number,
                        "attacker_name": s.attacker_name,
                        "defender_name": s.defender_name,
                        "durum": s.durum,
                        "tur": s.tur,
                        "tespit": iso(simdi),
                    },
                    simdi,
                )
            if not baseline_tamam:
                self.conn.execute(
                    "UPDATE grup_ayarlari SET savas_baseline_tamam=1 WHERE group_id=?", (group_id,)
                )
        return sonuc

    async def savas_detayi(self, savas_id: str, kaynak: DiplomaciaSource) -> SavasGorunumu:
        """Kullanıcı isteğiyle güncel ayrıntıyı alır; alınamazsa tarihli son veriyi gösterir."""
        savas_id = str(savas_id)
        simdi = self.saat()
        try:
            d = await kaynak.get_war_detail(savas_id)
        except KaynakHatasi as e:
            r = self.conn.execute(
                "SELECT veri, zaman FROM savas_detay_onbellek WHERE savas_id=?", (savas_id,)
            ).fetchone()
            if r is None:
                return SavasGorunumu(None, False, None, hata_aciklamasi(e))
            ham = json.loads(r["veri"])
            ham["taraflar"] = tuple(SavasTaraf(**t) for t in ham["taraflar"])
            ham["alinma_zamani"] = iso_coz(ham["alinma_zamani"]) if ham.get("alinma_zamani") else None
            return SavasGorunumu(SavasDetay(**ham), False, iso_coz(r["zaman"]), hata_aciklamasi(e))
        d = dataclasses.replace(d, alinma_zamani=simdi)
        ham = dataclasses.asdict(d)
        ham["alinma_zamani"] = iso(simdi)
        with islem(self.conn):
            self.conn.execute(
                "INSERT INTO savas_detay_onbellek(savas_id, veri, zaman) VALUES(?,?,?)"
                " ON CONFLICT(savas_id) DO UPDATE SET veri=excluded.veri, zaman=excluded.zaman",
                (savas_id, json.dumps(ham, ensure_ascii=False), iso(simdi)),
            )
        return SavasGorunumu(d, True, simdi)
