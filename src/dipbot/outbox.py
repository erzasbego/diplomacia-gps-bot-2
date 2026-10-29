"""Bildirim outbox'ı.

Değişiklik kaydı ile outbox satırı AYNI transaction'da yazılır (kuyruğa_ekle çağıran
transaction'a katılır, kendi commit'ini yapmaz). Gönderim ayrı adımdır; başarısız
gönderim satırı silmez, üstel geri çekilmeyle yeniden denenir.

Dürüstlük notu: gönderim sonrası ama 'gonderildi' işaretinden önce süreç ölürse aynı
mesaj tekrar gönderilebilir (en az bir kez teslim). Telegram tarafında tam "yalnızca
bir kez" garantisi verilemez; idempotency anahtarı yalnız kuyruğa çift yazımı önler.

Talep (kilit): gönderimden hemen önce satırın `sonraki_deneme` değeri OUTBOX_CLAIM_LEASE_SECONDS
ileri alınır (koşullu UPDATE = atomik talep). Eşzamanlı ikinci çağrı satırı bu yüzden göndermez.
Süreç gönderim ortasında ölürse satır talep süresi dolunca yeniden denenir: bu durumda mesaj
BİR KEZ DAHA gidebilir (en-az-bir-kez teslim; belgelenmiş sınır).

Geçici hata: kalıcı 'basarisiz' yalnız satır yaşı OUTBOX_MAX_AGE_SECONDS'i aşınca olur (deneme
sayısı değil). 429 (bekle_saniye verilmiş): deneme sayacı ARTMAZ; bekleme üst sınırlıdır.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from . import config
from .db import alan_aktif, alan_donemi, islem
from .maske import maskele
from .zaman import Saat, iso, iso_coz


class GonderimHatasi(Exception):
    def __init__(self, mesaj: str = "", *, kalici: bool = False, bekle_saniye: float | None = None):
        super().__init__(mesaj)
        self.kalici = kalici
        self.bekle_saniye = bekle_saniye


class Gonderici(Protocol):
    """Telegram sonra bağlanacak. Başarısızlıkta GonderimHatasi fırlatmalı."""

    async def gonder(self, sohbet_id: int, tur: str, veri: dict) -> None: ...


@dataclass
class GonderimOzeti:
    gonderilen: int = 0
    yeniden_denenecek: int = 0
    basarisiz: int = 0
    iptal: int = 0


def kuyruga_ekle(
    conn: sqlite3.Connection,
    *,
    hedef_sohbet: int,
    grup_id: int | None,
    tur: str,
    idem_anahtari: str,
    veri: dict,
    simdi,
) -> bool:
    """Satır ekler; aynı (hedef, anahtar) varsa eklemez. True = yeni eklendi.

    Gruba bağlı satır, üretildiği andaki grup onay DÖNEMİNİ (artan tamsayı) saklar; grup o an onaylı
    değilse dönem 0'dır (gönderimde iptal edilir). Zaman damgasına bakılmaz (D7)."""
    donem = None
    if grup_id is not None:
        donem = alan_donemi(conn, grup_id) or 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox(hedef_sohbet, grup_id, tur, idem_anahtari, veri,"
        " sonraki_deneme, olusturuldu, onay_donemi) VALUES(?,?,?,?,?,?,?,?)",
        (
            hedef_sohbet,
            grup_id,
            tur,
            idem_anahtari,
            json.dumps(veri, ensure_ascii=False, sort_keys=True),
            iso(simdi),
            iso(simdi),
            donem,
        ),
    )
    return cur.rowcount == 1


def grup_bekleyenlerini_iptal_et(conn: sqlite3.Connection, grup_id: int) -> int:
    with islem(conn):
        cur = conn.execute(
            "UPDATE outbox SET durum='iptal', son_hata='grup onayi kaldirildi'"
            " WHERE grup_id=? AND durum='bekliyor'",
            (grup_id,),
        )
        return cur.rowcount


def izinsiz_hedefleri_iptal_et(conn: sqlite3.Connection, izinli_hedefler) -> int:
    """Bekleyen satırlardan hedefi `izinli_hedefler` (kurucu özel sohbeti + log kanalı) DIŞINDA olanları İPTAL eder
    (silmez; durum='iptal'). Kanal geçişinden önce eski grup sohbetleri için kuyruğa alınmış bildirimler böylece
    ne eski gruba ne de yanlışlıkla log kanalına gider. Dönüş: iptal edilen satır sayısı."""
    izinli = sorted({int(x) for x in izinli_hedefler})
    if not izinli:
        raise ValueError("izinli hedef listesi boş olamaz")
    yer = ",".join("?" * len(izinli))
    with islem(conn):
        cur = conn.execute(
            f"UPDATE outbox SET durum='iptal', son_hata='hedef log kanali/kurucu degil (eski grup bildirimi)'"
            f" WHERE durum='bekliyor' AND hedef_sohbet NOT IN ({yer})",
            izinli,
        )
        return cur.rowcount


@dataclass
class HizalamaOzeti:
    eski_kapsam_iptal: int = 0  # eski grup kapsamlı satırlar (arşiv): iptal, GÖNDERİLMEZ
    kanala_yonlendirilen: int = 0  # merkezi kapsamlı satırlar: kanal değiştiyse yeni kanala alındı
    hedef_iptal: int = 0  # kurucu/kanal dışı hedefli artıklar: iptal


def bekleyenleri_hizala(conn: sqlite3.Connection, kurucu_id: int, kanal_id: int) -> HizalamaOzeti:
    """Açılışta BEKLEYEN satırları tek merkezli mimariye hizalar (silmez; yalnız durum/hedef günceller):
    1) kapsamı (grup_id) merkezi alan OLMAYAN eski grup satırları İPTAL (ne gruba ne kanala gider);
    2) merkezi kapsamlı satırlar başka/eski bir kanala yönelikse GÜNCEL kanala yönlendirilir (kanal değişiminde bildirim
       kaybolmaz; takip geçmişi etkilenmez); (hedef, anahtar) çakışırsa mükerrer olduğundan iptal;
    3) hedefi kurucu veya kanal olmayan kalan artıklar iptal. Kurucuya özel operasyonel satırlara dokunulmaz."""
    o = HizalamaOzeti()
    with islem(conn):
        o.eski_kapsam_iptal = conn.execute(
            "UPDATE outbox SET durum='iptal', son_hata='eski grup kapsami (arsiv): gonderilmez'"
            " WHERE durum='bekliyor' AND grup_id IS NOT NULL AND grup_id<>?",
            (config.MERKEZ_ID,),
        ).rowcount
        o.kanala_yonlendirilen = conn.execute(
            "UPDATE OR IGNORE outbox SET hedef_sohbet=? WHERE durum='bekliyor' AND grup_id=? AND hedef_sohbet<>?",
            (kanal_id, config.MERKEZ_ID, kanal_id),
        ).rowcount
        o.hedef_iptal = conn.execute(
            "UPDATE outbox SET durum='iptal', son_hata='hedef log kanali/kurucu degil (mukerrer/eski)'"
            " WHERE durum='bekliyor' AND hedef_sohbet NOT IN (?,?)",
            (kurucu_id, kanal_id),
        ).rowcount
    return o


def _onayli_mi(conn, grup_id) -> bool:
    return alan_aktif(conn, grup_id)


def _guncel_onay_donemi(conn, grup_id, satir_donemi) -> bool:
    """Grup onaylı VE satır onun GÜNCEL onay döneminde üretilmiş mi? Dönemi bilinmeyen (NULL/0) satır
    geçerliliği belirsiz sayılır: gönderilmez (fail-closed)."""
    d = alan_donemi(conn, grup_id)
    return d is not None and bool(satir_donemi) and satir_donemi == d


def _geri_cekilme(deneme: int) -> int:
    return min(
        config.OUTBOX_BACKOFF_BASE_SECONDS * (2 ** min(max(deneme - 1, 0), 20)),
        config.OUTBOX_BACKOFF_MAX_SECONDS,
    )


async def bekleyenleri_gonder(
    conn: sqlite3.Connection, gonderici: Gonderici, saat: Saat, *, limit: int = 50, izinli_hedefler=None,
    izinli_kapsamlar=None,
) -> GonderimOzeti:
    """`izinli_hedefler` verilirse (üretimde: kurucu + log kanalı) hedefi bunun dışındaki bekleyen satırlar
    GÖNDERİLMEZ, 'iptal' edilir (savunma katmanı: eski grup hedefli satır asla bir sohbete gitmez)."""
    ozet = GonderimOzeti()
    izinli = None if izinli_hedefler is None else {int(x) for x in izinli_hedefler}
    kapsamlar = None if izinli_kapsamlar is None else {int(x) for x in izinli_kapsamlar}
    simdi = saat()
    satirlar = conn.execute(
        "SELECT id FROM outbox WHERE durum='bekliyor' AND sonraki_deneme<=? ORDER BY id LIMIT ?",
        (iso(simdi), limit),
    ).fetchall()
    bekleyen: dict[int, str] = {}  # bu turda bekleme alan sohbet -> sonraki_deneme (iso)
    for s in satirlar:
        # Her gönderimden hemen önce güncel durum + grup onayı yeniden okunur.
        r = conn.execute("SELECT * FROM outbox WHERE id=?", (s["id"],)).fetchone()
        if r is None or r["durum"] != "bekliyor":
            continue
        if izinli is not None and r["hedef_sohbet"] not in izinli:
            conn.execute(
                "UPDATE outbox SET durum='iptal', son_hata='hedef log kanali/kurucu degil (eski grup bildirimi)'"
                " WHERE id=? AND durum='bekliyor'",
                (r["id"],),
            )
            ozet.iptal += 1
            continue
        if kapsamlar is not None and r["grup_id"] is not None and r["grup_id"] not in kapsamlar:
            # Eski grup (arşiv) kapsamlı satır: ASLA gönderilmez (üretimde izinli kapsam yalnız merkezi alandır).
            conn.execute(
                "UPDATE outbox SET durum='iptal', son_hata='eski grup kapsami (arsiv): gonderilmez'"
                " WHERE id=? AND durum='bekliyor'",
                (r["id"],),
            )
            ozet.iptal += 1
            continue
        if r["hedef_sohbet"] in bekleyen:
            # Aynı turda RetryAfter/hız sınırı alan sohbet: gönderme, bekleme sonuna ertele.
            conn.execute(
                "UPDATE outbox SET sonraki_deneme=? WHERE id=? AND durum='bekliyor' AND sonraki_deneme<?",
                (bekleyen[r["hedef_sohbet"]], r["id"], bekleyen[r["hedef_sohbet"]]),
            )
            continue
        if r["grup_id"] is not None and not _guncel_onay_donemi(conn, r["grup_id"], r["onay_donemi"]):
            # Onaysız grup VEYA satır önceki bir onay döneminde (onay kaldırılıp yeniden verilmiş) üretilmiş.
            conn.execute(
                "UPDATE outbox SET durum='iptal', son_hata='grup onaysiz/bayat' WHERE id=? AND durum='bekliyor'", (r["id"],)
            )
            ozet.iptal += 1
            continue
        kira = iso(saat() + timedelta(seconds=config.OUTBOX_CLAIM_LEASE_SECONDS))
        talep = conn.execute(
            "UPDATE outbox SET sonraki_deneme=? WHERE id=? AND durum='bekliyor' AND sonraki_deneme=?",
            (kira, r["id"], r["sonraki_deneme"]),
        )
        if talep.rowcount != 1:
            continue  # başka bir çağrı bu satırı talep etti
        try:
            await gonderici.gonder(r["hedef_sohbet"], r["tur"], json.loads(r["veri"]))
        except GonderimHatasi as e:
            _hata_isle(conn, r, e, e.kalici, e.bekle_saniye, saat, ozet)
            if e.bekle_saniye is not None and not e.kalici:
                bekleyen[r["hedef_sohbet"]] = iso(
                    saat() + timedelta(seconds=min(max(e.bekle_saniye, 0), config.OUTBOX_RETRY_AFTER_MAX_SECONDS))
                )
        except Exception as e:  # beklenmeyen gönderici hatası: geçici say
            _hata_isle(conn, r, e, False, None, saat, ozet)
        else:
            # Uçuştaki gönderim sırasında grup_kaldir 'iptal' yazdıysa ezilmez.
            conn.execute(
                "UPDATE outbox SET durum='gonderildi', gonderildi=?, son_hata=NULL WHERE id=? AND durum='bekliyor'",
                (iso(saat()), r["id"]),
            )
            ozet.gonderilen += 1
    return ozet


def _hata_isle(conn, r, exc, kalici, bekle, saat, ozet: GonderimOzeti) -> None:
    simdi = saat()
    limitli = bekle is not None  # 429 benzeri: hata değil, bekleme isteği
    deneme = r["deneme"] if limitli else r["deneme"] + 1
    metin = maskele(f"{type(exc).__name__}: {str(exc)[:200]}")
    yas = simdi - iso_coz(r["olusturuldu"])
    if kalici or (not limitli and yas >= timedelta(seconds=config.OUTBOX_MAX_AGE_SECONDS)):
        conn.execute(
            "UPDATE outbox SET durum='basarisiz', deneme=?, son_hata=? WHERE id=? AND durum='bekliyor'",
            (r["deneme"] + 1, metin, r["id"]),
        )
        ozet.basarisiz += 1
        return
    if limitli:
        bekleme = min(max(bekle, 0), config.OUTBOX_RETRY_AFTER_MAX_SECONDS)
    else:
        bekleme = _geri_cekilme(deneme)
    sonraki = simdi + timedelta(seconds=bekleme)
    conn.execute(
        "UPDATE outbox SET deneme=?, son_hata=?, sonraki_deneme=? WHERE id=? AND durum='bekliyor'",
        (deneme, metin, iso(sonraki), r["id"]),
    )
    ozet.yeniden_denenecek += 1
