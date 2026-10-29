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


_DURUM_ETIKET = {"preparing": "Preparing", "active": "Active"}
_TUR_ETIKET = {"standard": "Standard", "rebellion": "Rebellion", "independence": "Independence"}


def savas_baglantisi(war_number) -> str | None:
    """Kullanıcı tarafından doğrulanan biçim: {SITE}/wars/war/{war_number}. Yalnız POZİTİF tamsayı
    (bool/str/float/0/negatif/None -> None); URL'ye başka hiçbir girdi enjekte edilmez."""
    if isinstance(war_number, bool) or not isinstance(war_number, int) or war_number <= 0:
        return None
    return f"{SITE}/wars/war/{war_number}"


def durum_etiketi(durum) -> str:
    """Tanınan durum Türkçe etiket; tanınmayan ham değer (HTML kaçışlı); yoksa 'bilinmiyor'."""
    if not durum:
        return "unknown"
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
SAVAS_EMOJI_LEJANDI = "⚔️ standard · 🔥 rebellion · 🏳️ independence"
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
        return f"War #{war_number}"
    return kisalt(savas_id, 12) or "War"


def savas_liste_satiri(sira: int, s) -> str:
    """Sade liste satırı: '<sıra>. <tür emojisi> <Saldıran – Savunan> · <Durum> · #<numara>' (ayrıntı detayda).
    Numara geçerliyse başlık savaşa tıklanabilir bağlantıdır."""
    govde = esc(savas_kisa_ad(s.attacker_name, s.defender_name, s.ad, s.war_number, s.savas_id))
    url = savas_baglantisi(s.war_number)
    if url:
        govde = f'<a href="{esc(url)}">{govde}</a>'
    parcalar = [f"{sira}. {tur_emoji(s.tur)} {govde}", durum_etiketi(s.durum) if s.durum else "status unknown"]
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
        return f"War{sayi}"
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
        return "unknown"
    try:
        return yerel_goster(iso_coz(s))
    except ValueError:
        return "unknown"


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
_ALAN_ADI = {"ad": "name", "bolge": "region", "vatandaslik_ulkesi": "citizenship country"}
_ALAN_BASLIK = {
    "ad": "✏️ Player changed name",
    "bolge": "📍 Player changed region",
    "vatandaslik_ulkesi": "🌍 Player's citizenship country changed",
}
_KONTROL_NOTU = "<i>Detected by interval checks; not the exact time of the change.</i>"


def _b(s: str) -> str:
    return f"<b>{s}</b>"


def _oyuncu_degisti(v: dict) -> str:
    degisimler = v.get("degisimler") or []
    if len(degisimler) == 1 and degisimler[0].get("alan") in _ALAN_BASLIK:
        baslik = _ALAN_BASLIK[degisimler[0]["alan"]]
    else:
        baslik = "🔔 Player details changed"
    satirlar = [_b(baslik), f"Player: {oyuncu_link(v.get('oyuncu_id'), v.get('ad'))}"]
    for d in degisimler:
        etiket = _ALAN_ADI.get(d.get("alan"), str(d.get("alan")))
        satirlar.append(f"Previous {esc(etiket)}: {esc(d.get('eski'))}")
        satirlar.append(f"New {esc(etiket)}: {esc(d.get('yeni'))}")
    satirlar.append(f"Detected: {tarih(v.get('tespit'))}")
    satirlar.append(_KONTROL_NOTU)
    return "\n".join(satirlar)


def _parti_ad(v: dict) -> str:
    return "\n".join(
        [
            _b("🏛 Party name changed"),
            f"Old name: {esc(v.get('eski_ad'))}",
            f"New name: {parti_link(v.get('parti_id'), v.get('yeni_ad'))}",
            f"Detected: {tarih(v.get('tespit'))}",
            _KONTROL_NOTU,
        ]
    )


def _parti_uyelik(v: dict) -> str:
    satirlar = [_b("👥 Party membership changed"), f"Party: {parti_link(v.get('parti_id'), v.get('parti_ad'))}"]
    katilan = v.get("katilanlar") or []
    ayrilan = v.get("ayrilanlar") or []
    if katilan:
        satirlar.append("Joined (now tracked):")
        satirlar += [f"• {oyuncu_link(u.get('oyuncu_id'), u.get('ad'))}" for u in katilan]
    if ayrilan:
        satirlar.append("Left (individual tracking continues):")
        satirlar += [f"• {oyuncu_link(u.get('oyuncu_id'), u.get('ad'))}" for u in ayrilan]
    satirlar.append(f"Detected: {tarih(v.get('tespit'))}")
    satirlar.append(_KONTROL_NOTU)
    return "\n".join(satirlar)


def _yeni_savas(v: dict) -> str:
    satirlar = [
        _b(f"{tur_emoji(v.get('tur'))} New war detected"),
        "War: " + savas_baglantili(
            v.get("ad"), v.get("war_number"), v.get("attacker_name"), v.get("defender_name"), v.get("savas_id")
        ),
    ]
    if v.get("durum"):
        satirlar.append(f"Status: {durum_etiketi(v.get('durum'))}")
    if v.get("tur"):
        satirlar.append(f"Type: {tur_etiketi(v.get('tur'))}")
    satirlar += [
        f'<a href="{esc(SITE)}/wars">Wars page</a>',
        f"Detected: {tarih(v.get('tespit'))}",
        "<i>Use the /wars command for details and strength values.</i>",
    ]
    return "\n".join(satirlar)


def _yeni_savas_ozet(v: dict) -> str:
    liste = v.get("savaslar") or []
    satirlar = [_b(f"⚔️ {esc(v.get('sayi'))} new wars detected")]
    for s in liste:
        satirlar.append("• " + savas_baglantili(s.get("ad"), s.get("war_number"), None, None, s.get("savas_id")))
    if (v.get("sayi") or 0) > len(liste):
        satirlar.append(f"… and {esc((v.get('sayi') or 0) - len(liste))} more wars")
    satirlar += [f"Detected: {tarih(v.get('tespit'))}", "<i>Use the /wars command for the full list.</i>"]
    return "\n".join(satirlar)


_ISLEM_GOSTER = {
    "/oyuncuekle": "/addplayer",
    "/oyuncusil": "/removeplayer",
    "/partiekle": "/addparty",
    "/partisil": "/removeparty",
    "/oyuncular": "/players",
    "/partiler": "/parties",
    "/savaslar": "/wars",
    "/tarama": "/scan",
    "/durum": "/status",
    "/yardim": "/help",
    "/yetki_ver": "/grant",
    "/yetki_al": "/revoke",
    "/grup_onayla": "/approve_group",
    "/grup_kaldir": "/remove_group",
    "/grup_tasi": "/move_group",
    "onaysiz_gruba_eklendi": "added to an unapproved group",
}


def _islem_goster(islem: str) -> str:
    """Kaydedilen işlem adını görüntü için İngilizce komut adına çevirir (yalnız gösterim katmanı)."""
    if islem.startswith("buton:"):
        return "button:" + islem[6:]
    return _ISLEM_GOSTER.get(islem, islem)


def _yetkisiz(v: dict) -> str:
    islem = str(v.get("islem") or "")
    gid = v.get("grup_id")
    if islem == "onaysiz_gruba_eklendi":
        satirlar = [_b("⚠️ Bot was added to an unapproved group")]
    else:
        satirlar = [_b("⚠️ Unauthorized usage attempt")]
    satirlar.append(f"User ID: <code>{esc(v.get('kullanici_id') if v.get('kullanici_id') is not None else 'unknown')}</code>")
    satirlar.append(f"Group ID: <code>{esc(gid if gid is not None else 'private chat/none')}</code>")
    satirlar.append(f"Action: {esc(_islem_goster(islem))}")
    satirlar.append(f"Time: {tarih(v.get('zaman'))}")
    if islem == "onaysiz_gruba_eklendi" and gid is not None:
        satirlar.append(f"To approve: <code>/approve_group {esc(gid)}</code>")
    return "\n".join(satirlar)


def _yetkisiz_ozet(v: dict) -> str:
    return "\n".join(
        [
            _b("⚠️ Many unauthorized attempts"),
            f"Hourly notification limit exceeded; attempts not reported separately: {esc(v.get('sayi', '?'))}",
            f"Time: {tarih(v.get('zaman'))}",
        ]
    )


def _token_hatirlatma(v: dict) -> str:
    return "\n".join(
        [
            _b("🔑 Weekly Diplomacia token reminder"),
            f"{esc(v.get('gun', '?'))} days have passed since the last token update.",
            "The token's actual validity period has not been verified; you may want to renew it from the "
            "Administration > Token menu before it becomes invalid. If the token becomes invalid, scans stop and you will be notified separately.",
        ]
    )


def _token_gecersiz(v: dict) -> str:
    return "\n".join(
        [
            _b("🔑 Diplomacia site rejected the token (401)"),
            "The cause has not been verified (it may have expired, been changed, or there may be another problem). Scans are paused. In a private chat with the bot, send "
            "a new token via Administration > Renew Token.",
            f"Time: {tarih(v.get('zaman'))}",
        ]
    )


def _grup_goc(v: dict) -> str:
    return "\n".join(
        [
            _b("♻️ Group ID changed (upgraded to supergroup)"),
            f"Old ID: <code>{esc(v.get('eski'))}</code>",
            f"New ID: <code>{esc(v.get('yeni'))}</code> — NOT APPROVED, no actions can be performed.",
            "The old ID's approval was automatically revoked (scanning and sending stopped); the old tracking data was NOT deleted.",
            "To move the data to the new ID, do the following in order (owner, private chat):",
            f"1) <code>/approve_group {esc(v.get('yeni'))}</code>",
            f"2) <code>/move_group {esc(v.get('eski'))} {esc(v.get('yeni'))}</code> (confirmed with a button)",
            "No data is moved without owner approval.",
            f"Time: {tarih(v.get('zaman'))}",
        ]
    )


_DENETIM_ETIKET = {
    "yetki_ver": "Permission granted",
    "yetki_al": "Permission revoked",
    "grup_onayla": "Group approved",
    "grup_kaldir": "Group approval removed",
    "grup_goc_dusur": "Old group's approval automatically revoked during group migration",
    "grup_kaldir_sistem": "Group approval removed by the system",
    "grup_tasi": "Group tracking data moved to the new ID",
}


def _denetim(v: dict) -> str:
    """Yalnız kimlikler ve zaman; ad/metin/token YOK."""
    eylem = str(v.get("eylem") or "")
    aktor = v.get("aktor_id")
    hedef_tur = "group" if eylem.startswith("grup") else "user"
    return "\n".join(
        [
            _b("🔔 Permission/group change"),
            f"Action: {esc(_DENETIM_ETIKET.get(eylem, eylem))}",
            f"Performed by: <code>{esc(aktor) if aktor is not None else 'system'}</code>",
            f"Target ({hedef_tur}): <code>{esc(v.get('hedef_id'))}</code>",
            f"Time: {tarih(v.get('zaman'))}",
        ]
    )


def _denetim_ozet(v: dict) -> str:
    return "\n".join(
        [
            _b("🔔 Many permission/group changes"),
            f"Hourly notification limit exceeded; changes not reported separately: {esc(v.get('sayi', '?'))}",
            f"Time: {tarih(v.get('zaman'))}",
        ]
    )


def _kanal_durumu(v: dict) -> str:
    kanal = f"Channel ID: <code>{esc(v.get('kanal_id'))}</code>"
    if v.get("hazir"):
        return "\n".join(
            [
                _b("✅ Notification channel is ready again"),
                kanal,
                "Tracking scans have resumed; queued notifications will be delivered to the channel.",
                f"Time: {tarih(v.get('zaman'))}",
            ]
        )
    return "\n".join(
        [
            _b("⚠️ Notification channel is not ready"),
            kanal,
            f"Reason: {esc(v.get('neden'))}",
            "Tracking scans are paused and no tracking notifications are sent until the bot can post in the channel "
            "(add the bot as an administrator with the 'Post messages' permission).",
            f"Time: {tarih(v.get('zaman'))}",
        ]
    )


_BICIMLER = {
    "kanal_durumu": _kanal_durumu,
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
        return f"{_b('Notification')}\nType: {esc(tur)}"
    return f(veri or {})
