# VPS Kurulum ve İşletim (TASLAK)

Bu belge ve `deploy/` altındaki dosyalar taslaktır; gerçek bir sunucuda **denenmemiştir**.

## Doğrulanmamış varsayımlar (önce kontrol edin)

| Varsayım | Durum |
|---|---|
| Giriş noktası `python -m dipbot` | **Koddan doğrulandı** (`src/dipbot/__main__.py` -> `tg/calistir.py: main`). |
| Ortam değişkenleri | **Koddan doğrulandı:** `BOT_TOKEN`, `OWNER_TELEGRAM_ID` (zorunlu); `DB_PATH`, `DIPLOMACIA_TOKEN_DOSYASI`, `DIPLOMACIA_DOGRULAMA_OYUNCU` (isteğe bağlı; token doğrulamada sorgulanan oyuncu numarası, varsayılan `72400`, geçersizse bot başlamaz; doğrulama 404 verirse "doğrulama oyuncusu bulunamadı, token geçersiz demek değildir" uyarısı çıkar ve başka bir numara verilmelidir). Başka değişken okunmaz. |
| SQLite yolu | `DB_PATH` ile ayarlanır (boşsa `<proje>/data/dipbot.db`). Sunucuda `/var/lib/dipbot/dipbot.db` verilir; `backup.sh`/`restore.sh` varsayılanı aynıdır. Yedek betikleri `DB_PATH` ortam değişkenini kendi ortamlarında okur (systemd env dosyasını görmezler). |
| Debian/Ubuntu LTS, Python 3.11+ | Sürüm **varsayımdır**; `requirements.txt` ile teyit edin. |
| `systemd-analyze verify`, gerçek polling, gerçek süreçte SIGTERM | **Doğrulanmadı** (aşağıdaki "Doğrulama durumu özeti"ne bakın). |
| `backup.sh` / `restore.sh` davranışı | Yalnız `bash -n` sözdizimi denetimi yapıldı; sunucuda çalıştırılmadı. |

## Düzen

| Amaç | Yol |
|---|---|
| Kod | `/opt/dipbot` |
| Sanal ortam | `/opt/dipbot/.venv` |
| Kalıcı veri (SQLite + `-wal`/`-shm`) | `/var/lib/dipbot` (restart ve güncellemelerde korunur) |
| Diplomacia token dosyası | `/var/lib/dipbot/diplomacia.token` (0600, `dipbot` sahibi; yedeğe girmez) |
| Sırlar (BOT_TOKEN vb.) | `/etc/dipbot/dipbot.env` |
| Yedekler | `/var/backups/dipbot` |
| Servis kullanıcısı | `dipbot` (giriş kabuğu yok) |

## İki farklı token

| | `BOT_TOKEN` | Diplomacia oyun tokenı |
|---|---|---|
| Ne | Telegram botunun kimliği (BotFather) | Diplomacia oyun hesabına erişen token |
| Nerede | `/etc/dipbot/dipbot.env` (ortam değişkeni) | `DIPLOMACIA_TOKEN_DOSYASI` dosyası (varsayılan `<DB dizini>/diplomacia.token`) |
| Nasıl değişir | `dipbot.env` düzenlenir + `systemctl restart dipbot` | Kurucu, botun özel sohbet menüsünden (aşağıya bakın) |
| Yedek | Yedeğe girmez | Yedeğe girmez |

İkisi de veritabanında veya logda tutulmaz. `dipbot.env` depoya eklenmemelidir.

## Çıkış kodları

| Kod | Anlam | systemd davranışı |
|---|---|---|
| 0 | Normal kapanış (SIGTERM/SIGINT) | Yeniden başlatılmaz |
| 2 | Yapılandırma hatası (`BOT_TOKEN`/`OWNER_TELEGRAM_ID` eksik veya geçersiz); mesaj stderr'e/journal'a yazılır, değer gösterilmez | `RestartPreventExitStatus=2 3` ile yeniden başlatılmaz; unit `failed` kalır |
| 3 | `BOT_TOKEN` Telegram tarafından reddedildi (InvalidToken) **veya** Conflict (aynı token ile başka bir örnek getUpdates yapıyor) | Aynı şekilde yeniden başlatılmaz |
| diğer | Beklenmeyen hata | `Restart=on-failure`, 10 sn sonra; 10 dk'da 5 denemeyi aşarsa `failed` |

Kod 2 veya 3 sonrası nedeni düzeltip `systemctl reset-failed dipbot && systemctl start dipbot` çalıştırın. Conflict için başka örneği (başka sunucu, geliştirme makinesi, ikinci process) kapatın.

## İlk kullanım: kurucunun botu özelde başlatması

Telegram botlar bir kullanıcıya kendiliğinden yazamaz. Kurucu (`OWNER_TELEGRAM_ID` hesabı) botu bulup **özel sohbette `/start`** göndermelidir; aksi halde bot kuruciya mesaj/bildirim gönderemez ve token menüsü kullanılamaz.

## Kurulum (root veya sudo ile)

```bash
# 1. Paketler (Python sürümü varsayım: 3.11+)
apt update
apt install -y python3 python3-venv python3-pip sqlite3 git

# 2. Kullanıcı ve dizinler
useradd --system --home-dir /var/lib/dipbot --shell /usr/sbin/nologin dipbot
install -d -o dipbot -g dipbot -m 0750 /var/lib/dipbot
install -d -o dipbot -g dipbot -m 0700 /var/backups/dipbot
install -d -o root   -g dipbot -m 0750 /etc/dipbot
install -d -o root   -g root   -m 0755 /opt/dipbot

# 3. Kodu yerleştirin (depo adresi/yöntem size ait), sonra venv ve bağımlılıklar
#    Kod root'a ait kalır; servis kullanıcısı kodu değiştiremez.
git clone <DEPO_ADRESI> /opt/dipbot        # veya dosyaları kopyalayın
python3 -m venv /opt/dipbot/.venv
/opt/dipbot/.venv/bin/pip install -r /opt/dipbot/requirements.txt

# 4. Ortam dosyası: .env.example'ı örnek alın, GERÇEK değerleri yalnız sunucuda yazın
install -o root -g dipbot -m 0640 /opt/dipbot/.env.example /etc/dipbot/dipbot.env
chmod 0600 /etc/dipbot/dipbot.env && chown root:root /etc/dipbot/dipbot.env
nano /etc/dipbot/dipbot.env     # BOT_TOKEN, OWNER_TELEGRAM_ID, DB_PATH=/var/lib/dipbot/dipbot.db,
                                # DIPLOMACIA_TOKEN_DOSYASI=/var/lib/dipbot/diplomacia.token
ls -l /etc/dipbot/dipbot.env    # beklenen: -rw------- root root

# 5. Betikler
install -m 0750 -o root -g root /opt/dipbot/deploy/backup.sh  /usr/local/sbin/dipbot-backup
install -m 0750 -o root -g root /opt/dipbot/deploy/restore.sh /usr/local/sbin/dipbot-restore

# 6. Servis
install -m 0644 /opt/dipbot/deploy/dipbot.service /etc/systemd/system/dipbot.service
systemd-analyze verify /etc/systemd/system/dipbot.service   # DOGRULANMADI: sunucuda calistirin
systemctl daemon-reload
systemctl enable --now dipbot
systemctl status dipbot
```

Notlar:
- `EnvironmentFile` systemd (root) tarafından okunur; uygulama `dipbot.env` dosyasını ne okur ne yazar, bu yüzden `0600 root:root` yeterlidir.
- Servis `ProtectSystem=strict` ile çalışır: yalnız `ReadWritePaths=/var/lib/dipbot` yazılabilir. Uygulama yalnız `DB_PATH` dizinine (DB, WAL, SHM) ve token dosyasına yazar. `DB_PATH` veya `DIPLOMACIA_TOKEN_DOSYASI` başka bir dizine verilirse `dipbot.service` içindeki `ReadWritePaths` de güncellenmelidir. Token yazımı aynı dizinde geçici dosya + yeniden adlandırma ile yapıldığından dizinin kendisi yazılabilir olmalıdır.
- `UMask=0077` ile oluşan dosyalar yalnız `dipbot` kullanıcısına açıktır.
- Telegram long polling kullanılırsa gelen port, alan adı veya HTTPS gerekmez; yalnız giden HTTPS erişimi yeterlidir.

## Zaman

Kayıtlar (veritabanı, log) UTC tutulur (`TZ=UTC`). Kullanıcıya gösterilen zamanlar uygulama tarafından Europe/Istanbul'a çevrilir. `journalctl` çıktısı sunucu saat dilimine göre görünür; UTC için `--utc` ekleyin.

## Log görüntüleme

```bash
journalctl -u dipbot -f                        # canlı
journalctl -u dipbot --since "1 hour ago" --utc
journalctl -u dipbot -p warning -n 200
```

Loglarda token bulunmamalıdır (uygulama sorumluluğu); şüphe varsa paylaşmadan önce kontrol edin.

## Durdurma, başlatma, yeniden başlatma

```bash
systemctl stop dipbot       # SIGTERM; 30 sn içinde kapanmazsa SIGKILL (TimeoutStopSec=30)
systemctl start dipbot
systemctl restart dipbot
systemctl disable dipbot    # açılışta başlamasın
```

Koda göre (`tg/calistir.py`) SIGTERM/SIGINT alınınca polling durur, çalışan tarama görevine 10 sn tanınır (dolarsa iptal edilir), Telegram uygulaması ve SQLite bağlantısı kapatılır. `TimeoutStopSec=30` bu 10 sn'nin üstünde pay bırakır. Bu davranış birim testlerle sınanmıştır; **gerçek süreçte systemd ile doğrulanmadı**.

Çökme halinde `Restart=on-failure` 10 sn sonra yeniden başlatır (çıkış kodu 2 ve 3 hariç, bkz. "Çıkış kodları"); 10 dakikada 5'ten fazla başlatma denemesi olursa servis `failed` kalır (`StartLimit*`). Sıfırlamak için: `systemctl reset-failed dipbot && systemctl start dipbot`.

## Güncelleme

```bash
/usr/local/sbin/dipbot-backup                 # önce yedek
cd /opt/dipbot
git pull                                      # veya yeni dosyaları kopyalayın
/opt/dipbot/.venv/bin/pip install -r requirements.txt
systemctl restart dipbot
journalctl -u dipbot -n 50 --utc
```

Sorun çıkarsa önceki sürüme dönün (`git checkout <önceki_sürüm>`), gerekiyorsa aşağıdaki geri yüklemeyi uygulayın.

## Yedekleme

`backup.sh`, çalışan veritabanının düz kopyasını **almaz**; `sqlite3 .backup` kullanır, bütünlüğü doğrular, 0600 izinle zaman damgalı (`dipbot-YYYYMMDDTHHMMSSZ.db`, UTC) dosya yazar ve son `KEEP` (varsayılan 14) yedeği tutar. `dipbot.env` ve `diplomacia.token` yedeğe **konmaz** (betik yalnız SQLite dosyasını yedekler). Sırları ayrıca güvenli yerde (örn. parola yöneticisi) saklayın. **Geri yüklemede veya yeni sunucuya taşımada Diplomacia tokenı yeniden ayarlanmalıdır** (token dosyası yoksa aşağıdaki yöntemle). Yedekler token içermez ama oyuncu/grup verisi içerir; uzak sunucuya kopyalarken şifreli kanal kullanın.

El ile: `sudo /usr/local/sbin/dipbot-backup`

### Zamanlama - seçenek A: systemd timer (günlük)

`/etc/systemd/system/dipbot-backup.service`:
```ini
[Unit]
Description=Diplomacia GPS Botu SQLite yedegi

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/dipbot-backup
```

`/etc/systemd/system/dipbot-backup.timer`:
```ini
[Unit]
Description=Gunluk dipbot yedegi

[Timer]
OnCalendar=*-*-* 03:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now dipbot-backup.timer
systemctl list-timers dipbot-backup.timer
```

### Zamanlama - seçenek B: cron

`/etc/cron.d/dipbot-backup` (cron dosyası sunucu saat dilimini kullanır; UTC ise 03:30 UTC):
```
30 3 * * * root /usr/local/sbin/dipbot-backup >> /var/log/dipbot-backup.log 2>&1
```

## Yedekten geri yükleme

```bash
ls -1 /var/backups/dipbot/
sudo /usr/local/sbin/dipbot-restore /var/backups/dipbot/dipbot-YYYYMMDDTHHMMSSZ.db
```

Betik sırasıyla: root kontrolü, yedek bütünlük kontrolü, `EVET` onayı, servisi durdurma, mevcut DB'yi `dipbot-pre-restore-*.db` olarak yedekleme, eski `-wal/-shm` dosyalarını silme, geri yükleme, sahiplik/izin (`dipbot:dipbot`, 0600) düzeltme, servisi (önceden çalışıyorsa) başlatma. Sonra `journalctl -u dipbot -n 50` ile kontrol edin. Yedek tarihinden sonraki veriler kaybolur.

## Diplomacia token yenileme

Yöntem: kurucu, botun **Telegram özel sohbetindeki** token menüsünü kullanır. Bot, **aday tokenı mevcut dosyaya dokunmadan** `GET /api/players/72400` ile sınar (`Authorization: Bearer` + `X-Client-Platform: web`); yalnız HTTP 200 **ve** gövde oyuncu verisi olarak ayrıştırılabiliyorsa kabul eder. HTML/boş/`{}`/ayrıştırılamayan gövde, 401, 403, 5xx ve ağ hatası **reddedilir** (403 "geçersiz" değil "doğrulanamadı" sayılır). Kabulde mevcut dosya önce 0600 `<token dosyası>.prev` olarak atomik yedeklenir (`.prev` eski tokenı içerir; paylaşmayın, yedeğe koymayın), sonra yeni token atomik yazılır (geçici dosya + `os.replace`, izin 0600). Reddedilirse dosya ve `.prev` değişmez. Başarı mesajı yalnız "aday token ile oyuncu isteği başarılı; kaydedildi" der; canlı davranışı garanti etmez. Servisin yeniden başlatılması GEREKMEZ: token her Diplomacia isteğinde dosyadan okunur (mock testlerle doğrulandı, canlıda doğrulanmadı).

**Durum ve hata davranışı (mock testli, canlı DOĞRULANMADI):** token dosyası yok/boş/biçimi bozuksa istek atılmaz; durum ekranı "Diplomacia bağlantısı: token dosyası yok" der, geri çekilme ve log yağmuru olmaz. **401** → token geçersiz: taramalar duraklar, kurucuya TEK bildirim gider. **403** → "erişim reddedildi (403): kimlik biçimi veya site engeli olabilir"; geri çekilme uygulanır ama taramalar duraklatılmaz ve "token geçersiz" bildirimi gönderilmez. Çıkış kodları değişmedi (2 yapılandırma, 3 BOT_TOKEN/Conflict); Diplomacia token sorunları süreci sonlandırmaz.

Elle yöntem (gerekirse, sunucuda root ile; token'ı komut geçmişine/loga düşürmemeye dikkat edin):

```bash
install -m 0600 -o dipbot -g dipbot /dev/null /var/lib/dipbot/diplomacia.token
nano /var/lib/dipbot/diplomacia.token     # tek satır, boşluksuz; içeriği paylaşmayın
chown dipbot:dipbot /var/lib/dipbot/diplomacia.token && chmod 0600 /var/lib/dipbot/diplomacia.token
ls -l /var/lib/dipbot/diplomacia.token    # beklenen: -rw------- dipbot dipbot
```

Dosya, uygulama çalışırken de değiştirilebilir: kaynak kodu token'ı her istekte okur (yeniden başlatma gerekmez). Elle yazarken önceki dosyayı kendiniz yedekleyin. Token'ı sohbet, log, env dosyası veya yedek içine koymayın.

Windows (geliştirme) notu: Windows'ta POSIX dosya izinleri yoktur; `os.chmod` yalnız salt-okunur bayrağını etkiler. Orada koruma NTFS dizin ACL'lerine bağlıdır. Üretim hedefi Linux'tur.

## Doğrulama durumu özeti

Doğrulandı:
- `bash -n` ile `backup.sh` ve `restore.sh` sözdizimi (Git Bash).
- Koddan: giriş noktası, ortam değişkenleri, çıkış kodları 2/3, yazılan yollar, token dosyası davranışı, 10 sn tarama kapanış beklemesi.

**Doğrulanmadı:**
- `systemd-analyze verify dipbot.service` (ve `RestartPreventExitStatus` / sertleştirme direktiflerinin hedef systemd sürümünde kabulü).
- Gerçek Telegram polling (ağ, BotFather tokenı, Conflict/InvalidToken davranışı canlıda).
- Gerçek süreçte SIGTERM ile zarif kapanış ve süre uyumu (10 sn / `TimeoutStopSec=30`).
- Servis başlatma, timer, yedek/geri yükleme akışı.
- Gerçek Diplomacia bağlantısı (Bearer + X-Client-Platform kullanıcı tarafından DevTools'ta doğrulandı; kod mock testli, canlı bağlantı ve token doğrulama akışı sunucuda/canlıda TEST EDİLMEDİ).

## Denetim bildirimleri (Tur F)

Yetki/grup değişikliklerinde (yetki_ver, yetki_al, grup_onayla, grup_kaldir, göçte otomatik düşürme) kurucunun özel sohbetine yalnız kimlik ve zaman içeren bildirim gider (token/ad yok). Kurucunun kendi eylemleri bildirilmez (`config.AUDIT_NOTIFY_OWNER_ACTIONS=False`); aynı eylem 10 dk içinde (`AUDIT_DEDUP_WINDOW_SECONDS`) tek bildirim, saatte en fazla 5 (`AUDIT_NOTIFY_MAX_PER_HOUR`), fazlası tek özet. `/grup_kaldir` yalnız kurucu içindir. MOCK testli; canlı Telegram doğrulanmadı.

## Grup göçü ve veri taşıma

Telegram bir grubu süper gruba yükseltince grup kimliği değişir. Yeni kimliğe OTOMATİK yetki verilmez ve eski veri silinmez; taşıma yalnız kurucu onayıyla yapılır:

1. Telegram göç olayı gelir: eski kimliğin onayı düşer (tarama/gönderim durur), çift `grup_gocleri` tablosuna `bekliyor` olarak kaydedilir, kurucuya bildirim gider.
2. Kurucu özel sohbette `/grup_onayla <yeni_kimlik>` yazar (yeni grup onaylanmadan taşıma yapılmaz).
3. Kurucu özel sohbette `/grup_tasi <eski_kimlik> <yeni_kimlik>` yazar; ön koşullar denetlenir ve taşınacak sayılar gösterilir.
4. Kurucu "Onayla" düğmesine basar (tek kullanımlık, süreli); koşullar o anda yeniden doğrulanır ve tek transaction'da taşınır (hata olursa hiçbir şey değişmez).

Ön koşullar: çift kayıtlı ve `bekliyor`; yeni grup onaylı, eski onaysız; yeni grupta takip verisi ve başlamış savaş baseline'ı yok; eski grupta taşınacak veri var. Taşınanlar: oyuncular, partiler, parti üyeleri, engeller, grup ayarları, savaşlar + baseline bayrağı (bildirim seli olmaz), 404 sayaçları, tarama durumu. Eski kimlikteki bekleyen bildirimler/onaylar iptal edilir; gönderilmiş outbox geçmişi ve yetkisiz deneme günlüğü olduğu gibi kalır. `/grup_tasi` grupta ve yetkili kullanıcıda reddedilir. Taşıma kurucuya denetim bildirimi üretir (kurucunun kendi eylemi için `AUDIT_NOTIFY_OWNER_ACTIONS` bayrağı geçerli). MOCK testli; canlı Telegram göç olayı doğrulanmadı.
