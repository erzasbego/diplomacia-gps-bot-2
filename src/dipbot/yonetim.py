"""Yönetim işlemleri (DB): yetki verme/alma, grup onayı, tarama aralığı, yetkisiz deneme.

Her işlem GÜNCEL DB durumundan yetki okur; eski buton/önbellek yetkisine güvenilmez.
Telegram katmanı önce yetki.karar_ver ile sohbet/kullanıcıyı denetlemelidir.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from . import config, outbox, yetki
from .db import islem
from .yetki import YetkiDurumu
from .zaman import Saat, iso, iso_coz


log = logging.getLogger(__name__)


class YonetimHatasi(Exception):
    pass


class YetkiHatasi(YonetimHatasi):
    pass


def durum_yukle(conn: sqlite3.Connection, kurucu_id: int) -> YetkiDurumu:
    yetkililer = frozenset(r[0] for r in conn.execute("SELECT user_id FROM yetkili_kullanicilar"))
    gruplar = frozenset(r[0] for r in conn.execute("SELECT group_id FROM onayli_gruplar"))
    return YetkiDurumu(kurucu_id, yetkililer, gruplar)


def _aktor_denetle(conn, kurucu_id: int, aktor_id: int) -> YetkiDurumu:
    d = durum_yukle(conn, kurucu_id)
    if not yetki.yetkili_mi(d, aktor_id):
        raise YetkiHatasi("işlemi yapan kullanıcı yetkili değil")
    return d


# Telegram'in özel hesapları: GroupAnonymousBot (anonim yönetici), Channel_Bot (kanal adına mesaj).
OZEL_TELEGRAM_KIMLIKLERI = frozenset({1087968824, 136817688})


def denetim_bildir(conn, saat: Saat, kurucu_id: int, *, eylem: str, aktor_id: int | None, hedef_id: int) -> bool:
    """Yetki/grup DEĞİŞİKLİĞİNİ kurucuya özel sohbetten (outbox, grup_id=NULL) bildirir.

    Çağıranın transaction'ında çalışır (aynı transaction'da kuyruğa ekler). İçerik YALNIZ: eylem,
    aktör kimliği (None = sistem), hedef kimliği, zaman. Kurucunun kendi eylemi
    (config.AUDIT_NOTIFY_OWNER_ACTIONS=False iken) bildirilmez. Aynı (eylem, aktör, hedef) pencere
    içinde tek bildirim; saatlik üst sınır aşılırsa tek özet. Kuyruğa ekleme hatası asıl işlemi bozmaz.
    """
    if aktor_id == kurucu_id and not config.AUDIT_NOTIFY_OWNER_ACTIONS:
        return False
    try:
        simdi = saat()
        with islem(conn):
            anahtar = f"denetim:{eylem}:{aktor_id}:{hedef_id}"
            r = conn.execute("SELECT son_bildirim FROM bildirim_sinirlari WHERE anahtar=?", (anahtar,)).fetchone()
            if r is not None and not yetki.bildirim_gerekli_mi(
                iso_coz(r[0]), simdi, timedelta(seconds=config.AUDIT_DEDUP_WINDOW_SECONDS)
            ):
                return False
            n = conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE tur='denetim' AND hedef_sohbet=? AND olusturuldu>?",
                (kurucu_id, iso(simdi - timedelta(hours=1))),
            ).fetchone()[0]
            if n >= config.AUDIT_NOTIFY_MAX_PER_HOUR:
                oz = f"denetim_ozet:{simdi.strftime('%Y%m%d%H')}"
                yeni = outbox.kuyruga_ekle(
                    conn, hedef_sohbet=kurucu_id, grup_id=None, tur="denetim_ozet", idem_anahtari=oz,
                    veri={"sayi": 1, "zaman": iso(simdi)}, simdi=simdi,
                )
                if not yeni:
                    conn.execute(
                        "UPDATE outbox SET veri=json_set(veri,'$.sayi',json_extract(veri,'$.sayi')+1)"
                        " WHERE hedef_sohbet=? AND idem_anahtari=? AND durum='bekliyor'",
                        (kurucu_id, oz),
                    )
                return False
            conn.execute(
                "INSERT INTO bildirim_sinirlari(anahtar, son_bildirim) VALUES(?,?)"
                " ON CONFLICT(anahtar) DO UPDATE SET son_bildirim=excluded.son_bildirim",
                (anahtar, iso(simdi)),
            )
            return outbox.kuyruga_ekle(
                conn, hedef_sohbet=kurucu_id, grup_id=None, tur="denetim",
                idem_anahtari=f"denetim:{iso(simdi)}:{eylem}:{aktor_id}:{hedef_id}",
                veri={"eylem": eylem, "aktor_id": aktor_id, "hedef_id": hedef_id, "zaman": iso(simdi)},
                simdi=simdi,
            )
    except Exception:
        log.exception("denetim bildirimi kuyruğa alınamadı (asıl işlem etkilenmedi)")
        return False


def yetki_ver(conn, saat: Saat, kurucu_id: int, aktor_id: int, hedef_id: int) -> bool:
    _aktor_denetle(conn, kurucu_id, aktor_id)
    if not isinstance(hedef_id, int) or hedef_id <= 0:
        raise YonetimHatasi("yetki yalnız kullanıcı kimliğine (pozitif) verilir")
    if hedef_id in OZEL_TELEGRAM_KIMLIKLERI:
        raise YonetimHatasi("bu kimlik Telegram'ın özel hesabı (anonim yönetici/kanal); gerçek kullanıcı kimliği girin")
    if hedef_id == kurucu_id:
        return False  # kurucu zaten her zaman yetkili; ayrı kayıt gerekmez
    with islem(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO yetkili_kullanicilar(user_id, eklendi) VALUES(?,?)",
            (hedef_id, iso(saat())),
        )
        if cur.rowcount == 1:
            denetim_bildir(conn, saat, kurucu_id, eylem="yetki_ver", aktor_id=aktor_id, hedef_id=hedef_id)
        return cur.rowcount == 1


def yetki_al(conn, saat: Saat, kurucu_id: int, aktor_id: int, hedef_id: int) -> bool:
    d = _aktor_denetle(conn, kurucu_id, aktor_id)
    karar = yetki.yetki_alinabilir_mi(d, hedef_id)
    if not karar:
        raise YetkiHatasi(karar.neden)
    with islem(conn):
        cur = conn.execute("DELETE FROM yetkili_kullanicilar WHERE user_id=?", (hedef_id,))
        if cur.rowcount == 1:
            denetim_bildir(conn, saat, kurucu_id, eylem="yetki_al", aktor_id=aktor_id, hedef_id=hedef_id)
        return cur.rowcount == 1


def grup_onayla(conn, saat: Saat, kurucu_id: int, aktor_id: int, grup_id: int) -> bool:
    _aktor_denetle(conn, kurucu_id, aktor_id)
    if not isinstance(grup_id, int) or grup_id >= 0:
        raise YonetimHatasi("grup kimliği negatif olmalı (özel sohbet kimliği onaylanamaz)")
    with islem(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO onayli_gruplar(group_id, onaylandi) VALUES(?,?)",
            (grup_id, iso(saat())),
        )
        yeni = cur.rowcount == 1
        conn.execute("INSERT OR IGNORE INTO grup_ayarlari(group_id) VALUES(?)", (grup_id,))
        if yeni:
            # Yeniden etkinleştirme = ilk etkinleştirme: mevcut savaşlar bildirimsiz baseline olur.
            conn.execute(
                "UPDATE grup_ayarlari SET savas_baseline_tamam=0, sonraki_tarama=NULL,"
                " ardisik_hata=0 WHERE group_id=?",
                (grup_id,),
            )
            denetim_bildir(conn, saat, kurucu_id, eylem="grup_onayla", aktor_id=aktor_id, hedef_id=grup_id)
        return yeni


def grup_kaldir(conn, saat: Saat, kurucu_id: int, aktor_id: int, grup_id: int) -> int:
    """Onayı kaldırır; bekleyen bildirimleri iptal eder. İptal edilen bildirim sayısını döner.

    YALNIZ KURUCU (aktor_id == kurucu_id); yetkili kullanıcı veya aktörsüz (None) çağrı reddedilir.
    Sistem kaynaklı kaldırma için grup_kaldir_sistem kullanılır.
    Takip verisi silinmez (yeniden onayda korunur) ama taramalar ve gönderimler durur.
    """
    if aktor_id is None or not yetki.kurucu_mu(durum_yukle(conn, kurucu_id), aktor_id):
        raise YetkiHatasi("grup onayını yalnız kurucu kaldırabilir")
    return _grup_kaldir(conn, saat, kurucu_id, grup_id, "grup_kaldir", aktor_id)


def grup_kaldir_sistem(conn, saat: Saat, kurucu_id: int, grup_id: int, *, neden: str) -> int:
    """Sistem kaynaklı kaldırma (ör. grup göçünde eski kimlik): kullanıcı aktörü gerektirmez."""
    eylem = "grup_goc_dusur" if neden == "grup_goc" else "grup_kaldir_sistem"
    return _grup_kaldir(conn, saat, kurucu_id, grup_id, eylem, None)


def _grup_kaldir(conn, saat: Saat, kurucu_id: int, grup_id: int, eylem: str, aktor_id: int | None) -> int:
    with islem(conn):
        cur = conn.execute("DELETE FROM onayli_gruplar WHERE group_id=?", (grup_id,))
        if cur.rowcount == 1:
            denetim_bildir(conn, saat, kurucu_id, eylem=eylem, aktor_id=aktor_id, hedef_id=grup_id)
        return outbox.grup_bekleyenlerini_iptal_et(conn, grup_id)


# ----------------------------------------------------------------------------- D9: grup göçü ve veri taşıma
class TasimaHatasi(YonetimHatasi):
    """Taşıma ön koşulu sağlanmadı; HİÇBİR ŞEY değişmedi. Mesaj kurucuya gösterilebilir."""


# group_id taşıyan tablolar: eski -> yeni taşınır. Şema taraması testi bu iki kümenin
# (TASINAN_TABLOLAR + TASIMA_ISTISNALARI) group_id/grup_id içeren TÜM tabloları kapsadığını doğrular.
TASINAN_TABLOLAR = (
    "takip_oyuncular", "takip_partiler", "parti_uyeleri", "engeller", "savaslar",
    "bulunamadi_sayaclari", "tarama_isleri", "grup_ayarlari",
)
TASIMA_ISTISNALARI = {
    "onayli_gruplar": "onay taşınmaz: yeni grup kurucuca /grup_onayla ile, eski düşürülmüş olmalı",
    "outbox": "eski id'deki bekleyen satırlar iptal edilir; gönderilmiş/iptal geçmişi kalır",
    "yetkisiz_denemeler": "denetim günlüğü; olduğu gibi kalır",
    "tg_islemler": "eski id'ye bağlı bekleyen onay işlemleri silinir",
}
# sistem_durumu içinde grup kimliğini anahtara gömen girdiler ('<önek><group_id>'): taşınır.
TASINAN_DURUM_ANAHTARLARI = ("savas_taninmayan:",)
# Yeni grupta BOŞ olması, eski grupta en az birinin dolu olması gereken takip verisi tabloları.
_VERI_TABLOLARI = (
    "takip_oyuncular", "takip_partiler", "parti_uyeleri", "engeller", "savaslar", "bulunamadi_sayaclari",
)


def _grup_kimligi_gecerli(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x < 0


def grup_verisi_say(conn, grup_id: int) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE group_id=?", (grup_id,)).fetchone()[0]
        for t in _VERI_TABLOLARI
    }


def grup_verisi_var_mi(conn, grup_id: int) -> bool:
    return any(grup_verisi_say(conn, grup_id).values())


def goc_kaydet(conn, saat: Saat, eski_id: int, yeni_id: int) -> bool:
    """Telegram migrate olayını KAYDEDER (yetki vermez, veri taşımaz). Aynı çift tek kayıt.
    Dönüş: yeni kayıt oluştu mu."""
    if not (_grup_kimligi_gecerli(eski_id) and _grup_kimligi_gecerli(yeni_id)) or eski_id == yeni_id:
        return False
    with islem(conn):
        return conn.execute(
            "INSERT OR IGNORE INTO grup_gocleri(eski_id, yeni_id, tespit_zamani, durum) VALUES(?,?,?,'bekliyor')",
            (eski_id, yeni_id, iso(saat())),
        ).rowcount == 1


def _tasima_kontrol(conn, eski: int, yeni: int) -> dict[str, int]:
    """Tüm ön koşulları denetler; sağlanmazsa TasimaHatasi. Taşınacak veri sayılarını döner.
    Gerçek taşımada transaction İÇİNDE çağrılır (denetim ile uygulama arası yarış yok)."""
    if not (_grup_kimligi_gecerli(eski) and _grup_kimligi_gecerli(yeni)):
        raise TasimaHatasi("grup kimlikleri negatif tam sayı olmalı")
    if eski == yeni:
        raise TasimaHatasi("eski ve yeni kimlik aynı olamaz")
    r = conn.execute("SELECT durum FROM grup_gocleri WHERE eski_id=? AND yeni_id=?", (eski, yeni)).fetchone()
    if r is None:
        raise TasimaHatasi("bu çift için kayıtlı bir grup göçü yok (yalnız Telegram göç olayı kayıtlı çiftler taşınır)")
    if r[0] != "bekliyor":
        raise TasimaHatasi(f"bu göç kaydı artık beklemiyor (durum: {r[0]}); tekrar taşınamaz")
    onayli = {x[0] for x in conn.execute("SELECT group_id FROM onayli_gruplar")}
    if yeni not in onayli:
        raise TasimaHatasi(f"yeni grup onaylı değil; önce /grup_onayla {yeni}")
    if eski in onayli:
        raise TasimaHatasi("eski grup hâlâ onaylı; taşıma için onayının düşmüş olması gerekir")
    if any(grup_verisi_say(conn, yeni).values()):
        raise TasimaHatasi("yeni grupta zaten takip verisi var; karışmaması için taşıma yapılmaz")
    a = conn.execute("SELECT savas_baseline_tamam FROM grup_ayarlari WHERE group_id=?", (yeni,)).fetchone()
    if a is not None and a[0]:
        raise TasimaHatasi("yeni grupta savaş taraması başlamış; karışmaması için taşıma yapılmaz")
    sayilar = grup_verisi_say(conn, eski)
    if not any(sayilar.values()):
        raise TasimaHatasi("eski grupta taşınacak takip verisi yok")
    return sayilar


def tasima_degerlendir(conn, eski: int, yeni: int) -> dict[str, int]:
    """Salt okunur ön denetim (onay düğmesinden önce). Sağlanmazsa TasimaHatasi; hiçbir şey yazmaz."""
    return _tasima_kontrol(conn, eski, yeni)


def grup_tasi(conn, saat: Saat, kurucu_id: int, aktor_id: int | None, eski: int, yeni: int) -> dict[str, int]:
    """Kayıtlı göç çiftinin takip verisini eski -> yeni TEK transaction'da taşır (hata = tam geri alma).

    YALNIZ KURUCU. Ön koşullar transaction içinde yeniden doğrulanır; biri sağlanmazsa TasimaHatasi ve
    hiçbir şey değişmez. Dönüş: taşınan satır sayıları (takip_oyuncular, takip_partiler, ...).
    """
    if aktor_id is None or not yetki.kurucu_mu(durum_yukle(conn, kurucu_id), aktor_id):
        raise YetkiHatasi("grup verisini yalnız kurucu taşıyabilir")
    with islem(conn):
        sayilar = _tasima_kontrol(conn, eski, yeni)
        for t in _VERI_TABLOLARI:
            conn.execute(f"UPDATE {t} SET group_id=? WHERE group_id=?", (yeni, eski))
        # Tarama durumu: yeni grubun (boş) kayıtları atılır, eskinin kayıtları taşınır.
        conn.execute("DELETE FROM tarama_isleri WHERE group_id=?", (yeni,))
        conn.execute("UPDATE tarama_isleri SET group_id=? WHERE group_id=?", (yeni, eski))
        # Grup ayarları (tarama aralığı, savaş baseline bayrağı, zamanlama): eski değerler yeniye geçer.
        conn.execute("INSERT OR IGNORE INTO grup_ayarlari(group_id) VALUES(?)", (yeni,))
        e = conn.execute("SELECT * FROM grup_ayarlari WHERE group_id=?", (eski,)).fetchone()
        if e is not None:
            conn.execute(
                "UPDATE grup_ayarlari SET tarama_araligi_dk=?, savas_baseline_tamam=?, son_tarama_denemesi=?,"
                " sonraki_tarama=?, ardisik_hata=? WHERE group_id=?",
                (e["tarama_araligi_dk"], e["savas_baseline_tamam"], e["son_tarama_denemesi"],
                 e["sonraki_tarama"], e["ardisik_hata"], yeni),
            )
            conn.execute("DELETE FROM grup_ayarlari WHERE group_id=?", (eski,))
        for onek in TASINAN_DURUM_ANAHTARLARI:
            conn.execute("DELETE FROM sistem_durumu WHERE anahtar=?", (f"{onek}{yeni}",))
            conn.execute("UPDATE sistem_durumu SET anahtar=? WHERE anahtar=?", (f"{onek}{yeni}", f"{onek}{eski}"))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_islemler'").fetchone():
            conn.execute(
                "DELETE FROM tg_islemler WHERE grup_id=? OR json_extract(veri,'$.grup')=?"
                " OR json_extract(veri,'$.eski')=?",
                (eski, eski, eski),
            )
        outbox.grup_bekleyenlerini_iptal_et(conn, eski)
        conn.execute("UPDATE grup_gocleri SET durum='tamamlandi' WHERE eski_id=? AND yeni_id=?", (eski, yeni))
        # Aynı eski/yeni kimliği içeren diğer bekleyen kayıtlar artık geçersiz.
        conn.execute(
            "UPDATE grup_gocleri SET durum='iptal' WHERE durum='bekliyor' AND (eski_id=? OR yeni_id=?)",
            (eski, yeni),
        )
        denetim_bildir(conn, saat, kurucu_id, eylem="grup_tasi", aktor_id=aktor_id, hedef_id=yeni)
        return sayilar


def tarama_araligi_ayarla(conn, grup_id: int, dakika: int) -> None:
    if dakika not in config.ALLOWED_SCAN_INTERVALS_MINUTES:
        raise ValueError(f"geçersiz aralık: izinli değerler {config.ALLOWED_SCAN_INTERVALS_MINUTES}")
    with islem(conn):
        if conn.execute("SELECT 1 FROM onayli_gruplar WHERE group_id=?", (grup_id,)).fetchone() is None:
            raise YonetimHatasi("grup onaylı değil")
        conn.execute("UPDATE grup_ayarlari SET tarama_araligi_dk=? WHERE group_id=?", (dakika, grup_id))


def tarama_araligi(conn, grup_id: int) -> int:
    r = conn.execute("SELECT tarama_araligi_dk FROM grup_ayarlari WHERE group_id=?", (grup_id,)).fetchone()
    return r[0] if r else config.DEFAULT_SCAN_INTERVAL_MINUTES


def _temizle(conn, simdi) -> None:
    """Saklama süresi + satır üst sınırı; eski hız-sınırı kayıtları."""
    conn.execute(
        "DELETE FROM yetkisiz_denemeler WHERE zaman<?",
        (iso(simdi - timedelta(days=config.UNAUTHORIZED_RETENTION_DAYS)),),
    )
    conn.execute(
        "DELETE FROM yetkisiz_denemeler WHERE id <= (SELECT MAX(id) FROM yetkisiz_denemeler) - ?",
        (config.UNAUTHORIZED_TABLE_MAX_ROWS,),
    )
    conn.execute("DELETE FROM bildirim_sinirlari WHERE son_bildirim<?", (iso(simdi - timedelta(days=1)),))


def yetkisiz_deneme_kaydet(
    conn,
    saat: Saat,
    kurucu_id: int,
    *,
    kullanici_id: int | None,
    grup_id: int | None,
    islem_adi: str,
    aralik: timedelta | None = None,
) -> bool:
    """Denemeyi kaydeder; hız sınırı izin verirse kurucuya özel bildirimi kuyruğa alır.

    Aynı kullanıcı VEYA aynı grup aralık içinde zaten bildirildiyse bildirim üretilmez.
    Dönüş: kurucu bildirimi kuyruğa alındı mı.
    """
    aralik = aralik or timedelta(seconds=config.UNAUTHORIZED_NOTIFY_INTERVAL_SECONDS)
    simdi = saat()
    anahtarlar = []
    if kullanici_id is not None:
        anahtarlar.append(f"kullanici:{kullanici_id}")
    if grup_id is not None:
        anahtarlar.append(f"grup:{grup_id}")
    with islem(conn):
        conn.execute(
            "INSERT INTO yetkisiz_denemeler(zaman, user_id, group_id, islem) VALUES(?,?,?,?)",
            (iso(simdi), kullanici_id, grup_id, islem_adi[:100]),
        )
        _temizle(conn, simdi)
        for a in anahtarlar:
            r = conn.execute("SELECT son_bildirim FROM bildirim_sinirlari WHERE anahtar=?", (a,)).fetchone()
            if r is not None and not yetki.bildirim_gerekli_mi(iso_coz(r[0]), simdi, aralik):
                return False
        n = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE tur='yetkisiz_deneme' AND hedef_sohbet=? AND olusturuldu>?",
            (kurucu_id, iso(simdi - timedelta(hours=1))),
        ).fetchone()[0]
        if n >= config.UNAUTHORIZED_NOTIFY_MAX_PER_HOUR:
            # Toplam saatlik sınır aşıldı: tek özet satırı (saat başına), sayaç artar. Denemeler kayıtlıdır.
            anahtar = f"yetkisiz_ozet:{simdi.strftime('%Y%m%d%H')}"
            yeni = outbox.kuyruga_ekle(
                conn, hedef_sohbet=kurucu_id, grup_id=None, tur="yetkisiz_ozet", idem_anahtari=anahtar,
                veri={"sayi": 1, "zaman": iso(simdi)}, simdi=simdi,
            )
            if not yeni:  # özet henüz gönderilmediyse sayaç artar (gönderildiyse kayıt tabloda kalır)
                conn.execute(
                    "UPDATE outbox SET veri=json_set(veri,'$.sayi',json_extract(veri,'$.sayi')+1)"
                    " WHERE hedef_sohbet=? AND idem_anahtari=? AND durum='bekliyor'",
                    (kurucu_id, anahtar),
                )
            return False
        for a in anahtarlar:
            conn.execute(
                "INSERT INTO bildirim_sinirlari(anahtar, son_bildirim) VALUES(?,?)"
                " ON CONFLICT(anahtar) DO UPDATE SET son_bildirim=excluded.son_bildirim",
                (a, iso(simdi)),
            )
        return outbox.kuyruga_ekle(
            conn,
            hedef_sohbet=kurucu_id,
            grup_id=None,
            tur="yetkisiz_deneme",
            idem_anahtari=f"yetkisiz:{iso(simdi)}:{kullanici_id}:{grup_id}",
            veri={
                "kullanici_id": kullanici_id,
                "grup_id": grup_id,
                "islem": islem_adi[:100],
                "zaman": iso(simdi),
            },
            simdi=simdi,
        )
