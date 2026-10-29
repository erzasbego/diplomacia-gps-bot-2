"""Merkezi yapılandırma. Sır (token) içermez; token ayrı yerel dosyada tutulacak."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Tarama aralıkları (dakika): yalnızca bu dört seçenek kabul edilir.
ALLOWED_SCAN_INTERVALS_MINUTES = [5, 10, 15, 20]
DEFAULT_SCAN_INTERVAL_MINUTES = 10
# Aynı oyuncu art arda bu kadar taramada 404 dönerse durum ekranında 'bulunamıyor' işareti (otomatik silme YOK).
NOT_FOUND_STREAK_THRESHOLD = 3
# Tek taramada bu sayıdan fazla yeni savaş çıkarsa ayrı bildirimler yerine TEK özet bildirimi gider.
YENI_SAVAS_BILDIRIM_UST = 5

# MERKEZİ TAKİP ALANI: oyuncu/parti/savaş takibi Telegram gruplarından BAĞIMSIZDIR. Tüm takip verisi bu tek (Telegram
# kimliği olmayan) alan kimliği altında tutulur; kanal değişse de takip geçmişi korunur. Eski grup kimlikleri YALNIZ arşiv /
# geçiş kaynağıdır (satırlar silinmez; taranmaz, bildirim üretmez, komut kabul etmez). 0 hiçbir Telegram sohbet kimliğiyle çakışmaz.
MERKEZ_ID = 0
MERKEZ_DONEM = 1  # merkezi alanın sabit "onay dönemi" (onay mekanizması kullanılmaz; outbox satırlarında değişmez)

# BİLDİRİM KANALI: tüm otomatik takip bildirimleri (oyuncu/parti/savaş) YALNIZ bu Telegram kanalına gider.
# Eski gruplara bildirim gönderilmez ve grup komutları işlenmez; yönetim yalnız kurucunun özel sohbetindedir.
# Kanal değişirse takip verisi etkilenmez (veri modeli kanal kimliğine bağlı değildir; yalnız bekleyen bildirimler yeni kanala yönlendirilir).
# Ortamdan LOG_CHANNEL_ID ile değiştirilebilir (tg/yapilandirma.py); varsayılan üretim kanalıdır.
LOG_KANAL_ID = -1003998002227

# Zaman: kayıt UTC, kullanıcıya gösterim Europe/Istanbul.
STORAGE_TIMEZONE = "UTC"
DISPLAY_TIMEZONE = "Europe/Istanbul"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "dipbot.db"

# Bildirim outbox yeniden deneme ayarları.
# Geçici hatada kalıcı 'basarisiz' deneme sayısına değil satır YAŞINA bağlıdır.
OUTBOX_MAX_AGE_SECONDS = 24 * 3600
# Gönderim sırasında satır talep edilir (kilit); süre dolunca takılı satır yeniden denenir.
OUTBOX_CLAIM_LEASE_SECONDS = 300
# Telegram 429 'retry_after' bekleme üst sınırı.
OUTBOX_RETRY_AFTER_MAX_SECONDS = 3600
OUTBOX_BACKOFF_BASE_SECONDS = 30
OUTBOX_BACKOFF_MAX_SECONDS = 3600

# Yetkisiz deneme bildirimi: aynı kullanıcı/grup için en sık bu aralıkta bir kez.
UNAUTHORIZED_NOTIFY_INTERVAL_SECONDS = 900

# Yetkisiz deneme: kurucuya TOPLAM saatlik bildirim üst sınırı (fazlası tek özet), saklama.
UNAUTHORIZED_NOTIFY_MAX_PER_HOUR = 10
UNAUTHORIZED_RETENTION_DAYS = 30
UNAUTHORIZED_TABLE_MAX_ROWS = 10000

# Denetim bildirimi (yetki/grup değişikliği -> kurucuya özel mesaj).
# Kurucunun KENDİ yaptığı eylemler için bildirim YOK (komut yanıtı zaten gösterir); True yapılırsa bildirilir.
AUDIT_NOTIFY_OWNER_ACTIONS = False
# Aynı (eylem, aktör, hedef) için bu pencerede tek bildirim.
AUDIT_DEDUP_WINDOW_SECONDS = 600
# Kurucuya saatlik toplam denetim bildirimi üst sınırı; fazlası tek özet ('N değişiklik').
AUDIT_NOTIFY_MAX_PER_HOUR = 5

# D7 saat güvenliği: süreli onay/bekleme kayıtlarında duvar saati ile monotonic saatin geçen süreleri
# bu kadar saniyeden fazla ayrışırsa saat değişmiş sayılır ve kayıt geçersiz olur.
SAAT_TUTARSIZLIK_TOLERANSI_SANIYE = 30.0

# Token .prev yedeği süresiz tutulmaz: yeni token ilk başarılı yetkili taramayla teyit edilince VEYA
# bu süre dolunca otomatik silinir (unlink). Kontrol aralığı: zamanlayıcı döngüsünde en sık bu kadar.
TOKEN_PREV_OMRU_SANIYE = 24 * 3600
TOKEN_PREV_KONTROL_ARALIGI_SANIYE = 300.0

# TEK KRİTER: parti üyeleri arasından YALNIZ bu XP ve üzerindekiler (>=) otomatik takibe alınır; oyuncu SAYISI sınırlanmaz.
# Her üyenin XP'si ayrı denetlenir (API sıralamasına güvenilmez); XP eksik/geçersizse oyuncu eklenmez (bilinmiyor).
# Elle eklenenler etkilenmez.
PARTI_XP_ESIGI = 1_500_000
# Otomatik takipteki oyuncu eşiğin ALTINDA, art arda bu kadar taramada gözlenirse (geçerli XP ile) otomatik takipten
# çıkarılır. Tek geçici/hatalı yanıt silmez. XP eksik/geçersiz yanıtlar sayacı değiştirmez.
PARTI_XP_ALTI_ARDISIK_TARAMA = 3

# Parti eklenirken üyelerin ilk oyuncu verisi (bölge, vatandaşlık, gözlem zamanı): komut içinde SINIRLI iş yapılır;
# kalan üyeler baseline_tamam=0 ile eklenir ve ilk taramada (bildirimsiz) tamamlanır.
PARTI_EKLE_ESZAMANLI = 4  # aynı anda en çok bu kadar oyuncu isteği
PARTI_EKLE_ILK_VERI_UST = 300  # komutta en çok bu kadar üyenin ilk verisi istenir (takip sınırı DEĞİL; kalanı ilk taramada)
PARTI_EKLE_ILK_VERI_SURE_SN = 20.0  # toplam süre bütçesi; dolunca bekleyen istekler iptal edilir

# Zamanlayıcı.
SCHEDULER_ERROR_BACKOFF_MAX_SECONDS = 300.0
SCAN_MAX_CONCURRENT_GROUPS = 3
SOURCE_MAX_CONCURRENT_REQUESTS = 4
SOURCE_REQUEST_TIMEOUT_SECONDS = 30.0
SCAN_FAILURE_BACKOFF_MAX_MULTIPLIER = 8
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 120.0
RATE_LIMIT_MAX_WAIT_SECONDS = 1800.0
TICK_SECONDS = 15.0


@dataclass(frozen=True)
class Ayarlar:
    owner_telegram_id: int
    db_path: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Ayarlar":
        env = os.environ if env is None else env
        raw = env.get("OWNER_TELEGRAM_ID", "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError("OWNER_TELEGRAM_ID pozitif tamsayı bir Telegram kullanıcı kimliği olmalı")
        db = env.get("DB_PATH", "").strip()
        return cls(int(raw), Path(db) if db else DEFAULT_DB_PATH)
ERISIM_REDDI_BEKLEME_SECONDS = 60.0  # 403 sonrası arayüz isteklerinin kısa geri çekilmesi


# ---- ÇOK HESAPLI TARAMA ----------------------------------------------------------------------------------------
# Her Diplomacia hesabı EN FAZLA bu kadar oyuncudan SORUMLUDUR (SABİT GÖREV ATAMA KAPASİTESİ). Bu, API'nin bu kadar oyuncuyu belirli bir
# sürede sorgulamaya izin verdiği anlamına GELMEZ: gerçek hız/kota sınırları aşağıdaki pacing + hesap/genel eşzamanlılık + 429/403 geri
# çekilmesiyle ayrıca korunur. Hesaplar SIRAYLA doldurulur; kapasite dolunca yeni oyuncular veritabanında BEKLER (silinmez).
HESAP_KAPASITESI = 150
HESAP_ESZAMANLI = 2  # bir hesabın aynı anda en çok bu kadar isteği (arayüz + tarama toplamı)
GENEL_ESZAMANLI = SOURCE_MAX_CONCURRENT_REQUESTS  # TÜM hesapların toplam eşzamanlı isteği (hesap eklemek genel kotayı artırmaz)
HESAP_ISTEK_ARALIGI_SN = 0.25  # aynı hesabın ardışık istekleri arası en az bu kadar saniye
GENEL_ISTEK_ARALIGI_SN = 0.10  # tüm hesapların ardışık istekleri arası en az bu kadar saniye (genel kota koruması)
HESAP_PROXY_GERI_CEKILME_SN = 60.0  # proxy/bağlantı hatasında ilk bekleme; her ardışık hatada ikiye katlanır
HESAP_PROXY_GERI_CEKILME_UST_SN = 900.0
HESAP_GIRDI_BEKLEME_SN = 300  # token/proxy/arama girdisi bekleme ömrü (kurucu menüden başlatır)
HESAP_ARAMA_UST = 40  # arama sonucu en çok bu kadar satır gösterilir (sayfalı)
HESAP_COOLDOWN_ALT_SN = 5.0  # Retry-After: 0 gibi değerlerde bile en az bu kadar bekle
HESAP_ERISIM_REDDI_UST_SN = 900.0  # 403 geri çekilmesi (üstel) üst sınırı
HESAP_BILDIRIM_ARALIGI = 3600  # aynı hesap için aynı sorun bildirimi en sık bu aralıkta (saniye)
