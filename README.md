# Diplomacia GPS Botu

[Diplomacia](https://diplomacia.com.tr) oyunundaki **oyuncuları, partileri ve savaşları** izleyen, Telegram'dan yönetilen, tek VPS'te sürekli çalışacak bir bot. Değişiklikleri (isim, bölge, vatandaşlık ülkesi, parti girişi/çıkışı, yeni savaş) ilgili onaylı Telegram grubuna Türkçe bildirir.

Python 3.11+ · `python-telegram-bot` 22.8 (long polling; alan adı/HTTPS gerekmez) · SQLite · Telegram arayüzü ve mesajlar Türkçe · gösterilen zamanlar `Europe/Istanbul`, kayıtlar UTC.

> ## ⚠️ Doğrulama durumu — önce bunu okuyun
> Bu bot **henüz gerçek Diplomacia'ya veya gerçek Telegram'a bağlanılarak denenmedi.** Şu an elde olan: **679 otomatik test** (sahte veri kaynağı, `httpx.MockTransport`, sahte Telegram nesneleri; Python 3.11/3.12/3.13'te geçti). Bunlar iş kurallarını kanıtlar; **canlı entegrasyonu kanıtlamaz.** Ayrıntı için [Doğrulananlar ve doğrulanmayanlar](#doğrulananlar-ve-doğrulanmayanlar).

## İçindekiler
1. [Nasıl çalışır](#nasıl-çalışır)
2. [Kurulum (geliştirme)](#kurulum-geliştirme)
3. [Yapılandırma](#yapılandırma)
4. [Yetki modeli](#yetki-modeli)
5. [Komutlar ve butonlar](#komutlar-ve-butonlar)
6. [Takip kuralları](#takip-kuralları)
7. [Diplomacia token'ı ve yenileme](#diplomacia-tokenı-ve-yenileme)
8. [Veri klasörü, yedek ve geri yükleme](#veri-klasörü-yedek-ve-geri-yükleme)
9. [VPS (Ubuntu) kurulumu](#vps-ubuntu-kurulumu)
10. [Bilinen sınırlar](#bilinen-sınırlar)
11. [Doğrulananlar ve doğrulanmayanlar](#doğrulananlar-ve-doğrulanmayanlar)
12. [Proje yapısı ve testler](#proje-yapısı-ve-testler)

## Nasıl çalışır
- Bot, kurucunun verdiği **Diplomacia oturum token'ı** ile sitenin kendi JSON API'sini (`/api/players/…`, `/api/parties/…`, `/api/wars`) okur. Başlıklar: `Authorization: Bearer <token>` ve `X-Client-Platform: web` (biçim, kullanıcı tarafından tarayıcı DevTools'unda doğrulandı; botla canlıda denenmedi).
- Her onaylı grubun **ayrı** takip listesi, ayarı ve bildirimi vardır; gruplar birbirinin verisini görmez.
- Tarama aralığı grup başınadır: **5, 10, 15 veya 20 dakika** (varsayılan 10). Aynı iş önceki bitmeden yeniden başlamaz; siteye eşzamanlı istek sayısı sınırlıdır; hata/istek sınırında geri çekilme uygulanır.
- Değişiklik kaydı ile bildirim kuyruğu (outbox) **aynı veritabanı işleminde** yazılır; Telegram hatasında bildirim kaybolmaz, kontrollü yeniden denenir.

### "Aralıklı kontrol" sınırı (önemli)
Bot olayları **dinlemez, aralıklarla yoklar.** Bu yüzden: iki kontrol arasında olup geri alınan bir değişiklik (örn. oyuncu bölge değiştirip geri döndü) **görülmez**; bildirimdeki zaman **tespit zamanıdır, hareketin kesin zamanı değildir.** Savaşlarda yalnızca **yeni savaş** bildirilir; hasar/güç değişimi mesaj üretmez.

## Kurulum (geliştirme)
```bash
python3.11 -m venv .venv && . .venv/bin/activate     # Python 3.11+ (3.12 ve 3.13 de denendi)
pip install -r requirements.txt
python -m pytest -q                                  # beklenen: 679 passed (MOCK)
```
Paket `src/` altındadır ve pip ile **kurulmaz**; çalıştırırken `PYTHONPATH=src` verilir (testler için `pytest.ini` bunu zaten yapar):
```bash
export BOT_TOKEN=... OWNER_TELEGRAM_ID=...           # kendi değerleriniz; depoya YAZMAYIN
PYTHONPATH=src python -m dipbot
```
Uygulama `.env` dosyasını **okumaz**; yalnız süreç ortamını okur (systemd `EnvironmentFile` veya kabuk). `BOT_TOKEN`/`OWNER_TELEGRAM_ID` yoksa çıkış kodu 2 ile kapanır.

## Yapılandırma
Örnek: [`.env.example`](.env.example). Gerçek değerleri depoya eklemeyin.

| Değişken | Zorunlu | Anlamı |
|---|---|---|
| `BOT_TOKEN` | evet | Telegram botunun token'ı (BotFather). **Diplomacia token'ı değildir.** |
| `OWNER_TELEGRAM_ID` | evet | Kurucunun sayısal Telegram kullanıcı kimliği (pozitif tamsayı). |
| `DB_PATH` | hayır | SQLite dosyası. Boşsa `<proje>/data/dipbot.db`. |
| `DIPLOMACIA_TOKEN_DOSYASI` | hayır | Diplomacia token'ının tutulduğu dosya (0600). Boşsa `DB_PATH` dizinindeki `diplomacia.token`. |
| `DIPLOMACIA_AUTH_BASLIK`, `DIPLOMACIA_AUTH_SEMA` | hayır | Kimlik başlığı override'ı (varsayılan `Authorization` / `Bearer`). Genelde boş bırakılır. |
| `DIPLOMACIA_DOGRULAMA_OYUNCU` | hayır | Yeni token doğrulanırken sorgulanan herkese açık oyuncu numarası (varsayılan `72400`). |

Kod sabitleri (`src/dipbot/config.py`): tarama aralıkları `[5,10,15,20]`, denetim bildirimi sınırları, outbox/istek sınırı süreleri, `TOKEN_PREV_OMRU_SANIYE` (24 sa), `SAAT_TUTARSIZLIK_TOLERANSI_SANIYE` (30 sn).

Çıkış kodları: `0` normal, `2` yapılandırma hatası, `3` Telegram `BOT_TOKEN` reddedildi veya `Conflict` (aynı token'la ikinci örnek), `130` Ctrl-C (yalnız geliştirmede).

## Yetki modeli
Üç ayrı kavram vardır; kontroller kullanıcı **adıyla değil sayısal Telegram kimlikleriyle** yapılır.

| Kavram | Ne demek |
|---|---|
| **Kurucu** | `OWNER_TELEGRAM_ID` ile tanımlanan asıl sahip. Yetkisi başkaları tarafından alınamaz. **Yalnız kurucu:** özel sohbeti kullanır, token'ı yenileyebilir, `/grup_kaldir` ve `/grup_tasi` yapar. |
| **Yetkili kullanıcı** | Kurucunun `/yetki_ver` ile yetki verdiği kişiler. Onaylı gruplarda takip yönetimi, ayar, yetki verme/alma ve grup **onaylama** dahil normal yönetim işlemlerini yapar. Özel sohbette botu kullanamaz. |
| **Onaylı grup** | `/grup_onayla` ile izin verilen Telegram grubu. Grup onaylı olmak **gruptaki herkese yetki vermez**: işlemi yapan kişi de yetkili olmalıdır. |

- Onaysız grupta veya yetkisiz kişiden gelen **her komut ve buton** reddedilir ve kurucuya (hız sınırlı) bildirilir. Yetkisi alınan kullanıcı eski mesajlardaki butonlarla işlem yapamaz.
- Kurucuya **denetim bildirimi** gider: yetki verme/alma, grup onaylama/kaldırma, grup göçü/taşıma (yalnız kimlikler ve zaman; token yok). Kurucunun kendi eylemleri bildirilmez; aynı eylem 10 dk içinde tek bildirim, saatte en çok 5, fazlası tek özet.
- Bir grubun onayı kaldırılınca o grubun taramaları ve **bekleyen bildirimleri durur** (veri silinmez; yeniden onayda korunur).

## Komutlar ve butonlar
Gruplarda komutlar yalnız **onaylı grupta yetkili kullanıcıya** açıktır. Kurucu özel sohbette önce **Grup Seç** ile bir onaylı grup seçer, sonra aynı komutları/butonları kullanır. `/start` ana menüyü açar.

| Komut | Ne yapar |
|---|---|
| `/oyuncu_ekle <profil linki veya numara>` | Oyuncuyu takibe alır (ilk ekleme değişiklik bildirimi üretmez). Örn. `https://diplomacia.com.tr/player/72400` |
| `/oyuncular`, `/oyuncu_sil <numara>` | Listeler / takipten çıkarır (onay düğmesiyle). |
| `/parti_ekle <parti linki veya numara>` | Partiyi ve **o anki üyelerini** takibe alır; ayrıntı ilk taramada doldurulur. Sonradan katılan üye otomatik takibe alınır. |
| `/partiler`, `/parti_sil <numara>` | Listeler / parti takibini kaldırır (aşağıdaki "Parti kaldırma"). |
| `/savaslar` | Aktif savaşları listeler; ayrıntı ekranında `…/wars/war/{numara}` bağlantısı bulunur. |
| `/tarama <5\|10\|15\|20>` | Grubun tarama aralığı. |
| `/durum` | Bot durumu: son tarama zamanları, hatalar, token durumu. |
| `/yetki_ver <kullanıcı_id>`, `/yetki_al <kullanıcı_id>` | Yetki verir/alır. |
| `/grup_onayla <grup_id>` | Grubu onaylar (grup kimlikleri negatiftir). |
| `/grup_kaldir <grup_id>` | Grup onayını kaldırır — **yalnız kurucu** (komut ve düğme aynı kural). |
| `/grup_tasi <eski_id> <yeni_id>` | Supergroup'a yükselen grubun verisini yeni kimliğe taşır — **yalnız kurucu, yalnız özel sohbet**, düğmeyle onay. |
| `/yardim` | Komut özeti. |

**Ana menü düğmeleri:** Oyuncular · Partiler · Savaşlar · Tarama Ayarları · Bot Durumu · Yönetim · Yardım · Grup Seç (özelde). *Token Yenile* ana menüde değil, **Yönetim** ekranındadır (yalnız kurucu, özel sohbette). Silme/kaldırma ve yetki işlemleri **onay düğmesi** ister; onay tek kullanımlık, 10 dakika geçerli ve yalnız işlemi başlatan kullanıcıya/sohbete bağlıdır.

## Takip kuralları
- **Kimlik:** takip, sabit oyuncu kimliği üzerindendir; ad, bölge ve vatandaşlık ülkesi ayrı alanlar olarak bağımsız izlenir. Aynı oyuncu farklı yollarla (bağlantı, numara, parti, baştaki sıfırlı numara) eklense bile **tek** kayıttır.
- **Eksik/hatalı cevap** (boş alan, sayfalaması tamamlanmamış parti listesi, ağ hatası) değişiklik veya üye çıkışı sayılmaz; önceki doğru veri korunur. Hiç gözlem yapılamayan tarama "başarılı" kaydedilmez.
- **Oyuncu bulunamıyor (404):** art arda 3 taramada 404 alan oyuncu durum ekranında "Bulunamıyor" işaretlenir; **otomatik silinmez.**
- **Parti kaldırma:** önce partinin güncel üyeleri doğrulanır; doğrulanamazsa **hiçbir şey silinmez**. Doğrulanırsa, o anda partide olan herkes (elle eklenmiş olsa da) ilgili grubun takibinden çıkar; daha önce ayrılmış üyeler takipte kalır; diğer gruplar etkilenmez; kaldırılanlar otomatik yeniden eklenmez.
- **Savaşlar:** ilk taramada mevcut savaşlar bildirimsiz kaydedilir (bildirim yağmuru yok); tek taramada 5'ten fazla yeni savaş çıkarsa tek özet mesajı gider; tanınmayan durum/türdeki kayıt "yeni savaş" bildirimi üretmez.
- **Bildirim teslimi "en az bir kez"dir:** gönderimden hemen sonra süreç ölürse aynı mesaj bir kez daha gidebilir; çok parçalı uzun mesajda bir parça tekrarlanabilir. Telegram'da "tam bir kez" garantisi verilemez.

## Diplomacia token'ı ve yenileme
Token **veritabanında, ortam değişkeninde, logda ve Git'te tutulmaz**; yalnız `DIPLOMACIA_TOKEN_DOSYASI` dosyasında (0600) durur ve her istekte dosyadan okunur (yeniden başlatma gerekmez). Dosya yoksa/boşsa **hiç istek atılmaz** ve hata yağmuru olmaz.

**Gerçek geçerlilik süresi doğrulanmadı.** "Yaklaşık haftalık" beklentisi nedeniyle, son başarılı güncellemeden 7 gün sonra kurucuya (yalnız kurucuya) hatırlatma gider. Token daha erken geçersizleşirse (401) taramalar durur ve kurucuya **tek** mesaj gider.

**Yenileme adımları (yalnız kurucu, botla özel sohbette):**
1. Menü → **Yönetim** → **Token Yenile** → **Yeni token gönder** (bekleme bu düğmeyle başlar).
2. Yeni token'ı **tek bir mesajla** gönderin (5 dakika içinde). Bot mesajı silmeye çalışır; silemezse elle silin.
3. Bot adayı, mevcut dosyaya dokunmadan `GET /api/players/72400` ile sınar (200 **ve** oyuncu verisi olarak ayrıştırılabilen gövde şart; HTML/boş/401/403/5xx reddedilir). Başarısızsa mevcut token **değişmez**.
4. Başarılıysa eski token `…/diplomacia.token.prev` olarak yedeklenir, yenisi atomik yazılır ve taramalar kaldığı yerden devam eder.

**`.prev` politikası:** `.prev` eski token'ı içerir ve **süresiz tutulmaz.** Yeni token **ilk başarılı yetkili taramayla** teyit edilince **veya 24 saat** dolunca otomatik silinir (yalnız `unlink`). Geri dönüş yolu (yalnız `.prev` hâlâ varsa): `.prev` dosyasını token dosyasının üzerine taşıyın (`mv diplomacia.token.prev diplomacia.token`). Silindikten sonra geri dönüş yoktur. Windows'ta 0600 uygulanmaz (üretim hedefi Linux).

**Bekleyen onay ve token bekleme** süreç belleğindedir: **yeniden başlatmada geçersiz olur**, sunucu saati atlarsa (30 sn'den fazla tutarsızlık) uygulanmaz ve işlemin yeniden başlatılması istenir.

## Veri klasörü, yedek ve geri yükleme
- **Veri:** varsayılan `<proje>/data/` (sunucuda `/var/lib/dipbot`): `dipbot.db` (+ `-wal`, `-shm`) ve `diplomacia.token`. Servis yeniden başlatmalarında ve güncellemelerde korunur. Yetkili kullanıcılar, onaylı gruplar, ayarlar, takipler, son bilgiler, bilinen savaşlar, bildirim durumu ve engeller veritabanındadır; **Telegram mesaj geçmişi yerine kullanılmaz.**
- **Yedek:** `deploy/backup.sh` çalışan DB'nin düz kopyasını almaz; `sqlite3 .backup` kullanır, bütünlüğü doğrular (0600, zaman damgalı, son 14 tutulur). **Token dosyası, `.prev` ve `dipbot.env` yedeğe girmez**; sırları ayrıca güvenli yerde saklayın. Geri yüklemede/yeni sunucuda Diplomacia token'ı yeniden verilmelidir.
- **Geri yükleme:** `deploy/restore.sh <yedek.db>` (servisi durdurur, mevcut DB'yi güvenlik yedeğine alır, yedeği yükler). Yedek tarihinden sonraki veriler kaybolur.
- Ayrıntı ve zamanlama: [`deploy/README-DEPLOY.md`](deploy/README-DEPLOY.md).

## VPS (Ubuntu) kurulumu
Hedef: **Ubuntu 24.04 LTS** (Python 3.12), tek VPS, tek bot süreci, systemd. Adım adım kurulum, servis dosyası (`deploy/dipbot.service`), ön kontrol betiği (`deploy/onkontrol.sh`), yedek/geri yükleme, log ve güncelleme: [`deploy/README-DEPLOY.md`](deploy/README-DEPLOY.md). Kurulum sonrası **canlı doğrulama planı** (hangi adımda ne beklendiği, sonuç tablosu): [`deploy/CANLI-DOGRULAMA-PLANI.md`](deploy/CANLI-DOGRULAMA-PLANI.md).

Özet: `/opt/dipbot` (kod), `/var/lib/dipbot` (veri), `/etc/dipbot/dipbot.env` (0600 root; `BOT_TOKEN`, `OWNER_TELEGRAM_ID`), servis kullanıcısı `dipbot`; `PYTHONPATH=/opt/dipbot/src` birim dosyasında tanımlıdır. Önce kurucunun botla **özel sohbette `/start`** göndermesi gerekir (Telegram botlar kullanıcıya kendiliğinden yazamaz).

## Bilinen sınırlar
- Kontroller aralıklıdır (yukarıya bakın); gerçek hareket zamanı bilinmez.
- Outbox teslimi "en az bir kez"dir; çok parçalı mesajda parça tekrarı olabilir.
- `/grup_tasi`: `bildirim_sinirlari` ve `yetkisiz_denemeler` taşınmaz; eski id'de gönderilmiş outbox geçmişi kalır.
- Zaten gönderilmiş bir "katıldı" bildirimi, sonradan mükerrer olduğu anlaşılırsa geri alınamaz.
- İstek sınırı bekleme penceresi bellektedir (yeniden başlatmada sıfırlanır).
- Tarama zamanlaması, outbox yeniden deneme beklemesi (ve 24 saatlik yaş sınırı), haftalık hatırlatma, `.prev` yaşı ve yetkisiz-deneme/denetim bildirimlerinin tekrar-engel pencereleri duvar saatine bağlıdır; saat atlarsa gecikebilir, öne gelebilir veya bildirim kısa süre bastırılabilir (bekleyen **onayların** güvenliğini etkilemez). Linux monotonic saati askıya almayı saymaz (VM duraklaması yanlış "saat değişti" üretebilir; güvenli yöndedir).
- `/grup_tasi`: eski grubun onayı düştükten sonra yeni grup onaylanana kadar başlayan savaşlar, yeni grubun ilk (bildirimsiz) taramasında baseline'a girer ve taşımadan sonra da bildirilmez (kısa pencere; kabul edilen sınır). İki grupta ortak savaşın kaydı yeni grubunkiyle birleşir (en erken ilk görülme korunur).
- Token `.prev` yedeği tektir: üst üste iki yenilemede (A→B→C) ilk token kaybolur (`.prev` = B). Yedek, taramalar token nedeniyle duraklatılmışken yaş nedeniyle silinmez.
- Kod sürümünü geri alırken veritabanını da güncelleme öncesi yedekten geri yükleyin (şema ileri taşınır; bkz. `deploy/README-DEPLOY.md`).
- Savaş listesi sayfalaması ve `/members` sayfalaması **canlıda doğrulanmadı**; bot "tam liste" bayrağı gelmeyen listeyi eksik sayar.

## Doğrulananlar ve doğrulanmayanlar
**Doğrulandı (otomatik test / araç ile; hepsi MOCK ya da yerel):**
- 679 test: ilk ekleme bildirimsiz, kimlik üzerinden değişiklik, bölge/vatandaşlık bağımsızlığı, tekrar bildirim yok, eksik cevap veriyi bozmaz, parti ilk kayıt/giriş/çıkış/kaldırma kuralları, grup izolasyonu, yetki matrisi (komut+buton), eski buton reddi, yeniden başlatmada veri korunumu, ilk savaş taramasında bildirim yok, outbox yeniden deneme/iptal, token doğrulama/`.prev`/maskeleme, D7 saat güvenliği, göç taşıma.
- Python 3.11, 3.12 ve 3.13'te test paketi geçti (Ubuntu 24.04 tabanlı bulut ortamında).
- `deploy/dipbot.service` için `systemd-analyze verify` (systemd 255, **statik**; yollar sahte düzende) hatasız; `backup.sh`/`restore.sh` gerçek WAL veritabanı ve **sahte** `systemctl` ile çalıştı; `onkontrol.sh` sahte dizin düzeniyle çalıştı.
- Kullanıcı DevTools'ta doğruladı (ben bağımsız doğrulamadım): `Authorization: Bearer <JWT>` + `X-Client-Platform: web` ile `GET /api/players/72400` → 200; savaş bağlantısı biçimi `…/wars/war/{numara}`; oturum token'ı JWT'dir.

**Doğrulanmadı (canlıda denenmeli — plan: `deploy/CANLI-DOGRULAMA-PLANI.md`):**
- Botun gerçek Diplomacia bağlantısı; `GET /api/players/72400`'ün **kimlik gerektirdiği** (başlıksız ve sahte Bearer ile 401) — bu yüzden token doğrulaması canlıda **kesinleşmiş sayılmaz**.
- Token ömrü; yeni girişin eski token'ı düşürüp düşürmediği; 401 dışı hata gövdeleri, 403/404/429 anlamları, `Retry-After`; `X-Client-Platform` zorunluluğu; `/members` ve `/api/wars` sayfalaması; `/api/wars/ended`; başka `status`/`war_type` değerleri; `attacker_power`/`defender_power`'ın "hasar" olup olmadığı (arayüzde "Güç" denir); `null` bölge/vatandaşlık anlamı; oyuncu UUID'sinin değişmezliği.
- Gerçek Telegram: polling, HTML görünümü, gizlilik modu, `delete_message`, `set_my_commands`, göç (`migrate`) olayları, `/grup_tasi` düğmesi, hız sınırları.
- Gerçek Ubuntu VPS: servis başlatma/yeniden başlatma, gerçek süreçte SIGTERM, otomatik yeniden başlama, zamanlayıcı/cron yedeği, uzun süreli çalışma, gerçek NTP saat atlaması.

## Proje yapısı ve testler
```
src/dipbot/            çekirdek: db, takip, yetki, yonetim, outbox, zamanlayici, kaynak, maske, zaman
  diplomacia/          API adaptörü (oyuncu/parti/üye/savaş ayrıştırıcıları, HTTP istemcisi, token doğrulayıcı)
  tg/                  Telegram katmanı (arayüz, biçimleme, gönderici, token deposu, onay işlemleri, çalıştırıcı)
tests/                 otomatik testler + tests/fixtures (gerçek yanıtlardan KISALTILMIŞ örnekler)
deploy/                systemd birimi, yedek/geri yükleme, ön kontrol, kurulum ve canlı doğrulama belgeleri
dp.txt                 asıl şartname · API-ARASTIRMA.md · findings.md · progress.md · task_plan.md · DEVAM.md · BULUT_DEVAM.md
```
Testler: `python -m pytest -q` (ağ kullanmaz). Geliştirme geçmişi, bulgular ve açık maddeler: `findings.md`, `progress.md`.
