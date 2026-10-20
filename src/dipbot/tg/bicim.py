"""Metin biçimlendirme: HTML kaçış, bağlantılar, bölme ve outbox bildirim metinleri.

Tüm dış kaynaklı metin (oyuncu/parti adı vb.) `esc` ile kaçırılır. Mesajlar HTML parse_mode
ile gönderilir. Bölme satır sınırında yapılır; böylece bir HTML etiketi iki parçaya bölünmez.
"""
from __future__ import annotations

import html
import re
from urllib.parse import quote

from ..zaman import iso_coz, yerel_goster

SITE = "https://diplomacia.com.tr"
MESAJ_AZAMI = 4096
BOLME_HEDEFI = 3500  # 4096 sınırının altında güvenli pay

_ETIKET = re.compile(r"<[^>]+>")


def esc(deger) -> str:
    """HTML kaçışı (tırnaklar dahil: href içinde de güvenli)."""
    return html.escape("" if deger is None else str(deger), quote=True)


_DURUM_ETIKET = {"preparing": "Hazırlık", "active": "Aktif"}
_TUR_ETIKET = {"standard": "Standart", "rebellion": "Ayaklanma", "independence": "Bağımsızlık"}


def savas_baglantisi(war_number) -> str | None:
    """Kullanıcı tarafından doğrulanan biçim: {SITE}/wars/war/{war_number}. Yalnız POZİTİF tamsayı
    (bool/str/float/0/negatif/None -> None); URL'ye başka hiçbir girdi enjekte edilmez."""
    if isinstance(war_number, bool) or not isinstance(war_number, int) or war_number <= 0:
        return None
    return f"{SITE}/wars/war/{war_number}"


def durum_etiketi(durum) -> str:
    """Tanınan durum Türkçe etiket; tanınmayan ham değer (HTML kaçışlı); yoksa 'bilinmiyor'."""
    if not durum:
        return "bilinmiyor"
    return _DURUM_ETIKET.get(durum) or esc(durum)


def tur_etiketi(tur) -> str | None:
    if not tur:
        return None
    return _TUR_ETIKET.get(tur) or esc(tur)


# Emoji anlamları (tutarlı): TÜR emojisi satır başında TEK emoji; DURUM metinle yazılır, ayrıntıda emoji alır.
#   ⚔️ standart savaş (tür bilinmiyorsa/tanınmıyorsa da genel savaş)   🔥 ayaklanma   🏳️ bağımsızlık
#   Ayrıntıda: ⚔️ saldıran taraf · 🛡️ savunan taraf · ⏳ hazırlık · 🟢 aktif · ❔ durum bilinmiyor/tanınmıyor
TUR_EMOJI = {"standard": "⚔️", "rebellion": "🔥", "independence": "🏳️"}
GENEL_SAVAS_EMOJI = "⚔️"
DURUM_EMOJI = {"preparing": "⏳", "active": "🟢"}
SAVAS_EMOJI_LEJANDI = "⚔️ standart · 🔥 ayaklanma · 🏳️ bağımsızlık"
AD_AZAMI = 40  # liste satırı/buton için taraf adı üst sınırı (Telegram sınırlarına pay)


def tur_emoji(tur) -> str:
    return TUR_EMOJI.get(tur or "", GENEL_SAVAS_EMOJI)


def durum_emoji(durum) -> str:
    return DURUM_EMOJI.get(durum or "", "❔")


def kisalt(metin, n: int = AD_AZAMI) -> str | None:
    """Boşsa None; n karakteri aşarsa '…' ile keser (HTML kaçışından ÖNCE uygulanır)."""
    if metin is None:
        return None
    m = str(metin).strip()
    if not m:
        return None
    return m if len(m) <= n else m[: n - 1].rstrip() + "…"


def savas_kisa_ad(saldiran, savunan, ad, war_number, savas_id) -> str:
    """DÜZ METİN (kaçışsız; düğme etiketi için): 'Saldıran – Savunan', yoksa savaş adı, yoksa '#numara'/kimlik."""
    sad, sav = kisalt(saldiran), kisalt(savunan)
    if sad and sav:
        return f"{sad} – {sav}"
    if kisalt(ad, 60):
        return kisalt(ad, 60)
    if savas_baglantisi(war_number):
        return f"Savaş #{war_number}"
    return kisalt(savas_id, 12) or "Savaş"


def savas_liste_satiri(sira: int, s) -> str:
    """Sade liste satırı: '<sıra>. <tür emojisi> <Saldıran – Savunan> · <Durum> · #<numara>' (ayrıntı detayda).
    Numara geçerliyse başlık savaşa tıklanabilir bağlantıdır."""
    govde = esc(savas_kisa_ad(s.attacker_name, s.defender_name, s.ad, s.war_number, s.savas_id))
    url = savas_baglantisi(s.war_number)
    if url:
        govde = f'<a href="{esc(url)}">{govde}</a>'
    parcalar = [f"{sira}. {tur_emoji(s.tur)} {govde}", durum_etiketi(s.durum) if s.durum else "durum bilinmiyor"]
    if url:
        parcalar.append(f"#{s.war_number}")
    return " · ".join(parcalar)


def savas_baslik(ad, war_number, saldiran, savunan, savas_id) -> str:
    """Kaçışlı görünen ad: war_name; yoksa 'Saldıran – Savunan (#numara)'; yoksa '#numara'; yoksa kimlik."""
    sayi = f" (#{war_number})" if savas_baglantisi(war_number) else ""
    if ad:
        return esc(ad)
    if saldiran and savunan:
        return f"{esc(saldiran)} – {esc(savunan)}{sayi}"
    if sayi:
        return f"Savaş{sayi}"
    return esc(savas_id)


def savas_baglantili(ad, war_number, saldiran, savunan, savas_id) -> str:
    """Tıklanabilir (numara geçerliyse) kaçışlı başlık. Adlı savaşta numara ayrıca yazılır."""
    baslik = savas_baslik(ad, war_number, saldiran, savunan, savas_id)
    url = savas_baglantisi(war_number)
    if url is None:
        return baslik
    govde = f'<a href="{esc(url)}">{baslik}</a>'
    return f"{govde} (#{war_number})" if ad else govde


def oyuncu_url(oyuncu_id) -> str:
    return f"{SITE}/player/{quote(str(oyuncu_id), safe='')}"


def parti_url(parti_id) -> str:
    return f"{SITE}/party/{quote(str(parti_id), safe='')}"


def oyuncu_link(oyuncu_id, ad=None) -> str:
    return f'<a href="{esc(oyuncu_url(oyuncu_id))}">{esc(ad or oyuncu_id)}</a>'


def parti_link(parti_id, ad=None) -> str:
    return f'<a href="{esc(parti_url(parti_id))}">{esc(ad or parti_id)}</a>'


def tarih(s: str | None) -> str:
    """Depodaki UTC ISO metnini Europe/Istanbul biçimine çevirir; yoksa 'bilinmiyor'."""
    if not s:
        return "bilinmiyor"
    try:
        return yerel_goster(iso_coz(s))
    except ValueError:
        return "bilinmiyor"


def _duz(satir: str) -> str:
    return html.unescape(_ETIKET.sub("", satir))


def bol(metin: str, sinir: int = BOLME_HEDEFI) -> list[str]:
    """Metni satır sınırlarında `sinir` karakteri aşmayan parçalara böler.

    Tek bir satır sınırı aşarsa etiketler soyulur ve düz metin olarak yeniden kaçırılıp
    sert bölünür (etiket/varlık ortasından bölünmesin diye).
    """
    if sinir < 10 or sinir > MESAJ_AZAMI:
        raise ValueError("geçersiz bölme sınırı")
    parcalar: list[str] = []
    cur: list[str] = []
    uzunluk = 0

    def bosalt():
        nonlocal cur, uzunluk
        if cur:
            parcalar.append("\n".join(cur))
        cur, uzunluk = [], 0

    for satir in metin.split("\n"):
        if len(satir) > sinir:
            bosalt()
            duz = _duz(satir)
            adim = max(sinir // 6, 1)  # kaçışla şişmeye karşı pay
            for i in range(0, len(duz), adim):
                parca = esc(duz[i : i + adim])
                if uzunluk + len(parca) + 1 > sinir:
                    bosalt()
                cur.append(parca)
                uzunluk += len(parca) + 1
            continue
        if uzunluk + len(satir) + 1 > sinir:
            bosalt()
        cur.append(satir)
        uzunluk += len(satir) + 1
    bosalt()
    return parcalar or [""]


# ---- Outbox bildirim metinleri --------------------------------------------
_ALAN_ADI = {"ad": "ad", "bolge": "bölge", "vatandaslik_ulkesi": "vatandaşlık ülkesi"}
_ALAN_BASLIK = {
    "ad": "✏️ Oyuncu adını değiştirdi",
    "bolge": "📍 Oyuncu bölge değiştirdi",
    "vatandaslik_ulkesi": "🌍 Oyuncunun vatandaşlık ülkesi değişti",
}
_KONTROL_NOTU = "<i>Aralıklı kontrolle tespit edildi; kesin değişiklik zamanı değildir.</i>"


def _b(s: str) -> str:
    return f"<b>{s}</b>"


def _oyuncu_degisti(v: dict) -> str:
    degisimler = v.get("degisimler") or []
    if len(degisimler) == 1 and degisimler[0].get("alan") in _ALAN_BASLIK:
        baslik = _ALAN_BASLIK[degisimler[0]["alan"]]
    else:
        baslik = "🔔 Oyuncu bilgileri değişti"
    satirlar = [_b(baslik), f"Oyuncu: {oyuncu_link(v.get('oyuncu_id'), v.get('ad'))}"]
    for d in degisimler:
        etiket = _ALAN_ADI.get(d.get("alan"), str(d.get("alan")))
        satirlar.append(f"Önceki {esc(etiket)}: {esc(d.get('eski'))}")
        satirlar.append(f"Yeni {esc(etiket)}: {esc(d.get('yeni'))}")
    satirlar.append(f"Tespit: {tarih(v.get('tespit'))}")
    satirlar.append(_KONTROL_NOTU)
    return "\n".join(satirlar)


def _parti_ad(v: dict) -> str:
    return "\n".join(
        [
            _b("🏛 Parti adı değişti"),
            f"Eski ad: {esc(v.get('eski_ad'))}",
            f"Yeni ad: {parti_link(v.get('parti_id'), v.get('yeni_ad'))}",
            f"Tespit: {tarih(v.get('tespit'))}",
            _KONTROL_NOTU,
        ]
    )


def _parti_uyelik(v: dict) -> str:
    satirlar = [_b("👥 Parti üyeliği değişti"), f"Parti: {parti_link(v.get('parti_id'), v.get('parti_ad'))}"]
    katilan = v.get("katilanlar") or []
    ayrilan = v.get("ayrilanlar") or []
    if katilan:
        satirlar.append("Katılanlar (takibe alındı):")
        satirlar += [f"• {oyuncu_link(u.get('oyuncu_id'), u.get('ad'))}" for u in katilan]
    if ayrilan:
        satirlar.append("Ayrılanlar (bireysel takipleri sürer):")
        satirlar += [f"• {oyuncu_link(u.get('oyuncu_id'), u.get('ad'))}" for u in ayrilan]
    satirlar.append(f"Tespit: {tarih(v.get('tespit'))}")
    satirlar.append(_KONTROL_NOTU)
    return "\n".join(satirlar)


def _yeni_savas(v: dict) -> str:
    satirlar = [
        _b(f"{tur_emoji(v.get('tur'))} Yeni savaş tespit edildi"),
        "Savaş: " + savas_baglantili(
            v.get("ad"), v.get("war_number"), v.get("attacker_name"), v.get("defender_name"), v.get("savas_id")
        ),
    ]
    if v.get("durum"):
        satirlar.append(f"Durum: {durum_etiketi(v.get('durum'))}")
    if v.get("tur"):
        satirlar.append(f"Tür: {tur_etiketi(v.get('tur'))}")
    satirlar += [
        f'<a href="{esc(SITE)}/wars">Savaşlar sayfası</a>',
        f"Tespit: {tarih(v.get('tespit'))}",
        "<i>Ayrıntı ve güç değerleri için /savaslar komutunu kullanın.</i>",
    ]
    return "\n".join(satirlar)


def _yeni_savas_ozet(v: dict) -> str:
    liste = v.get("savaslar") or []
    satirlar = [_b(f"⚔️ {esc(v.get('sayi'))} yeni savaş tespit edildi")]
    for s in liste:
        satirlar.append("• " + savas_baglantili(s.get("ad"), s.get("war_number"), None, None, s.get("savas_id")))
    if (v.get("sayi") or 0) > len(liste):
        satirlar.append(f"… ve {esc((v.get('sayi') or 0) - len(liste))} savaş daha")
    satirlar += [f"Tespit: {tarih(v.get('tespit'))}", "<i>Tam liste için /savaslar komutunu kullanın.</i>"]
    return "\n".join(satirlar)


def _yetkisiz(v: dict) -> str:
    islem = str(v.get("islem") or "")
    gid = v.get("grup_id")
    if islem == "onaysiz_gruba_eklendi":
        satirlar = [_b("⚠️ Bot onaysız bir gruba eklendi")]
    else:
        satirlar = [_b("⚠️ Yetkisiz kullanım denemesi")]
    satirlar.append(f"Kullanıcı kimliği: <code>{esc(v.get('kullanici_id') if v.get('kullanici_id') is not None else 'bilinmiyor')}</code>")
    satirlar.append(f"Grup kimliği: <code>{esc(gid if gid is not None else 'özel sohbet/yok')}</code>")
    satirlar.append(f"İşlem: {esc(islem)}")
    satirlar.append(f"Zaman: {tarih(v.get('zaman'))}")
    if islem == "onaysiz_gruba_eklendi" and gid is not None:
        satirlar.append(f"Onaylamak için: <code>/grup_onayla {esc(gid)}</code>")
    return "\n".join(satirlar)


def _yetkisiz_ozet(v: dict) -> str:
    return "\n".join(
        [
            _b("⚠️ Çok sayıda yetkisiz deneme"),
            f"Saatlik bildirim sınırı aşıldı; ayrıca bildirilmeyen deneme sayısı: {esc(v.get('sayi', '?'))}",
            f"Zaman: {tarih(v.get('zaman'))}",
        ]
    )


def _token_hatirlatma(v: dict) -> str:
    return "\n".join(
        [
            _b("🔑 Haftalık Diplomacia token hatırlatması"),
            f"Son token güncellemesinin üzerinden {esc(v.get('gun', '?'))} gün geçti.",
            "Token'ın gerçek geçerlilik süresi doğrulanmadı; geçersizleşmeden önce Yönetim > Token "
            "menüsünden yenilemek isteyebilirsiniz. Token geçersizleşirse taramalar durur ve ayrıca bildirilir.",
        ]
    )


def _token_gecersiz(v: dict) -> str:
    return "\n".join(
        [
            _b("🔑 Diplomacia site token'ı reddetti (401)"),
            "Neden doğrulanmadı (süresi dolmuş, değiştirilmiş veya başka bir sorun olabilir). Taramalar duraklatıldı. Bot ile özel sohbette Yönetim > Token Yenile menüsünden "
            "yeni token gönderin.",
            f"Zaman: {tarih(v.get('zaman'))}",
        ]
    )


def _grup_goc(v: dict) -> str:
    return "\n".join(
        [
            _b("♻️ Grup kimliği değişti (süper gruba yükseltme)"),
            f"Eski kimlik: <code>{esc(v.get('eski'))}</code>",
            f"Yeni kimlik: <code>{esc(v.get('yeni'))}</code> — ONAYSIZ, işlem yapılamaz.",
            "Eski kimliğin onayı otomatik düşürüldü (tarama ve gönderim durdu); eski takip verisi SİLİNMEDİ.",
            "Veriyi yeni kimliğe taşımak için sırayla (kurucu, özel sohbet):",
            f"1) <code>/grup_onayla {esc(v.get('yeni'))}</code>",
            f"2) <code>/grup_tasi {esc(v.get('eski'))} {esc(v.get('yeni'))}</code> (düğmeyle onaylanır)",
            "Kurucu onayı olmadan hiçbir veri taşınmaz.",
            f"Zaman: {tarih(v.get('zaman'))}",
        ]
    )


_DENETIM_ETIKET = {
    "yetki_ver": "Yetki verildi",
    "yetki_al": "Yetki alındı",
    "grup_onayla": "Grup onaylandı",
    "grup_kaldir": "Grup onayı kaldırıldı",
    "grup_goc_dusur": "Grup göçünde eski grubun onayı otomatik düşürüldü",
    "grup_kaldir_sistem": "Grup onayı sistem tarafından kaldırıldı",
    "grup_tasi": "Grup takip verisi yeni kimliğe taşındı",
}


def _denetim(v: dict) -> str:
    """Yalnız kimlikler ve zaman; ad/metin/token YOK."""
    eylem = str(v.get("eylem") or "")
    aktor = v.get("aktor_id")
    hedef_tur = "grup" if eylem.startswith("grup") else "kullanıcı"
    return "\n".join(
        [
            _b("🔔 Yetki/grup değişikliği"),
            f"Eylem: {esc(_DENETIM_ETIKET.get(eylem, eylem))}",
            f"Eylemi yapan: <code>{esc(aktor) if aktor is not None else 'sistem'}</code>",
            f"Hedef ({hedef_tur}): <code>{esc(v.get('hedef_id'))}</code>",
            f"Zaman: {tarih(v.get('zaman'))}",
        ]
    )


def _denetim_ozet(v: dict) -> str:
    return "\n".join(
        [
            _b("🔔 Çok sayıda yetki/grup değişikliği"),
            f"Saatlik bildirim sınırı aşıldı; ayrıca bildirilmeyen: {esc(v.get('sayi', '?'))} değişiklik",
            f"Zaman: {tarih(v.get('zaman'))}",
        ]
    )


_BICIMLER = {
    "denetim": _denetim,
    "denetim_ozet": _denetim_ozet,
    "oyuncu_degisti": _oyuncu_degisti,
    "parti_ad_degisti": _parti_ad,
    "parti_uyelik": _parti_uyelik,
    "yeni_savas": _yeni_savas,
    "yeni_savas_ozet": _yeni_savas_ozet,
    "yetkisiz_deneme": _yetkisiz,
    "yetkisiz_ozet": _yetkisiz_ozet,
    "token_gecersiz": _token_gecersiz,
    "token_hatirlatma": _token_hatirlatma,
    "grup_goc": _grup_goc,
}


def bildirim_metni(tur: str, veri: dict) -> str:
    f = _BICIMLER.get(tur)
    if f is None:
        return f"{_b('Bildirim')}\nTür: {esc(tur)}"
    return f(veri or {})
