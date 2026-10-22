# Canlı VPS güvenli güncelleme planı — PR #4 (İngilizce arayüz, İstanbul saati, sade `/status`)

**Durum:** PLAN. Bu belge hazırlanırken canlı VPS'e dokunulmadı: hiçbir dosya değiştirilmedi, `dipbot` servisi durdurulmadı veya yeniden başlatılmadı; ayrı PM2 servisi `takip-bot`a hiçbir koşulda dokunulmaz. Aşağıdaki adımları siz sunucuda çalıştırırsınız. Betikler bulutta yalnız sahte dizin, sahte `id`/`runuser`/`systemctl`/`pip` ve yerel git depolarıyla test edildi; **gerçek sunucuda çalıştırılmadı** (özellikle gerçek `chown`/sahiplik davranışı, gerçek `runuser`, gerçek SSH deploy anahtarı).

## 1. PR #4 ne değiştirir? (ve ne değiştirmez?)

PR #4'ün kendisi:
- Telegram arayüzünün tamamı **İngilizce** (tek dil; dil seçeneği yok). Komut menüsü: `/start /help /addplayer /players /removeplayer /addparty /parties /removeparty /wars /scan /status /grant /revoke /approve_group /remove_group /move_group`.
- **Eski Türkçe komutlar çalışmaya devam eder** (`/durum`, `/oyuncular`, `/yetki_ver`, `/grup_onayla`, `/oyuncu_ekle` …): aynı işleyici, aynı yetki/onay yolu, yanıtlar İngilizce. Menüde/yardımda listelenmezler.
- Kullanıcıya gösterilen tüm saatler **Europe/Istanbul, 24 saat** (`DD.MM.YYYY HH:mm`, AM/PM yok). Veritabanı, zamanlayıcı ve API zaman damgaları **UTC** kalır; dönüşüm yalnız metin üretilirken yapılır. Sunucunun saat dilimi değiştirilmez.
- `/status` sadeleşti: iki eski tanı satırı (sayfalama notu ve "yapılandırıldı … canlı bağlantı test edilmedi") kaldırıldı; "Diplomacia connection" satırı yalnız gerçek bir sorun varsa görünür.
- Bu PR'da ayrıca dağıtım güvenliği düzeltmeleri vardır (bkz. §3): yedek ve git işlemleri artık root olarak canlı SQLite'a / depoya dokunmaz.

PR #4 **değiştirmez:** SQLite şemasını ve verisini, grup onayı/yetki/kurucu mantığını, takip algoritmalarını (oyuncu/parti/savaş), tarama aralığını ve zamanlayıcıyı, bildirim teslimini, API kimlik doğrulamasını. **PR #4 hiçbir veritabanı geçişi içermez.**

## 2. Veritabanı: hangi sürümde olduğunuzu ÖNCE kontrol edin

`main` (PR #4 birleşince) önceki PR'ları da içerir. **PR #3** (XP süzgeci / büyük parti kısmi okuma) şemayı v4 → v6'ya **yalnız sütun ekleyerek** yükseltir; bu PR #4'ün değil PR #3'ün geçişidir. Canlı sunucuda hangisi geçerliyse onu görün (salt okunur, servis çalışırken güvenli; `dipbot` kullanıcısı olarak):

```bash
sudo -u dipbot sqlite3 -readonly /var/lib/dipbot/dipbot.db 'PRAGMA user_version;'
```

| Çıktı | Anlamı | Güncellemede ne olur |
|---|---|---|
| `6` | PR #3 zaten dağıtılmış | **Hiçbir şema geçişi olmaz.** Yalnız kod değişir. |
| `4` | Sunucu PR #2 düzeyinde (büyük parti hataları `100 != 329` bu durumdadır) | `main`'i çalıştırmak, servisin **ilk açılışında** PR #3'ün `ALTER TABLE … ADD COLUMN` geçişini (v4 → v6) otomatik yapar. Satır silinmez/yeniden yazılmaz; grup onayları, yetkililer, takip edilen oyuncu/partiler, engeller, savaşlar ve bekleyen bildirimler korunur. Ayrıntı için §7 (miras bölüm). |
| `< 4` veya başka | Beklenmeyen | **Durun**; güncellemeyi yapmayın, bulguyu kaydedin. |

Her durumda güncellemeden önce **yedek zorunludur** (§4).

## 3. Yedek ve git güvenliği (bu sürümde düzeltilen sorunlar)

- **Sorun 1 — root sahipli SQLite:** önceki `backup.sh`, `guncelle.sh` (root) tarafından çağrılınca canlı DB'yi **root olarak** açıyordu. WAL modunda root, `dipbot.db-wal/-shm` dosyalarını yoksa **root sahipli** oluşturabilir; servis (`dipbot`) bunları sonradan açamaz ve başlamayabilir. **Düzeltme:** `backup.sh` root ise sqlite3'ü `runuser -u dipbot --` ile çalıştırır (canlı DB'ye hiç root dokunmaz); kaynak `-readonly` açılır; yedek önce `dipbot`'a ait özel (0700) geçici dizine yazılıp `PRAGMA integrity_check` (immutable, `-wal/-shm` üretmez) ile doğrulanır, `cmp` ile kopyası denetlenir, sonra yedek dizinine 0600 taşınır. DB veya `-wal/-shm` sahibi `dipbot` değilse **yedek alınmaz** ve hiçbir şey değişmez.
- **Sorun 2 — yedek dizini körlemesine `chmod 700`:** mevcut `/var/backups/dipbot` artık değiştirilmez (sahip/izin dokunulmaz). Betik yalnız doğrular: sembolik bağlantı değil, grup/diğer yazamaz, sahibi root/çalıştıran/`dipbot`, yazılabilir; aksi hâlde **durur** ve ne yapılacağını söyler. Dizin yoksa 0700 ile oluşturulur. Yedek dosyaları 0600. Saklama (`KEEP`, varsayılan 14) davranışı aynıdır.
- **Sorun 3 — git'i root çalıştırmak:** `guncelle.sh` git'i artık **deponun sahibi kullanıcı** olarak (`runuser -u <depo sahibi> -- env HOME=<onun evi> git …`) çalıştırır: root'un SSH deploy anahtarı veya `safe.directory` ayarı varsayılmaz, `.git` içinde root sahipli dosya oluşmaz. `GIT_TERMINAL_PROMPT=0` ile parola sorusunda takılmaz. Depo sahibi `root` ise git doğrudan çalışır.
- `onkontrol.sh` artık `dipbot.db-wal/-shm` sahibi `dipbot` değilse **HATA** verir (root sahipli kalıntıyı yakalar).
- `restore.sh`: mevcut DB'nin güvenlik yedeği de aynı şekilde `dipbot` olarak alınır; `SAFETY_DIR` körlemesine `chmod`lanmaz.

> **Önemli — ilk dağıtım için:** sunucudaki `guncelle.sh`/`backup.sh` **eski** sürümdür ve `guncelle.sh` yedek betiğini güncellemeden ÖNCE mevcut (eski) checkout'tan çalıştırır. Bu yüzden **bu sürümü ilk kez** aşağıdaki §4 elle yoluyla, **yeni** `backup.sh`'yi getirilen revizyondan alarak dağıtın. Sonraki güncellemelerde `sudo /opt/dipbot/deploy/guncelle.sh main` kullanabilirsiniz.

## 4. Önerilen yol (ilk dağıtım): elle, yeni betiklerle

Varsayımlar: kod `/opt/dipbot`, veri `/var/lib/dipbot`, yedekler `/var/backups/dipbot`, servis kullanıcısı `dipbot`. Farklıysa değiştirin. Depo sahibi (deploy kullanıcısı) `stat` ile bulunur; sudo yerine o kullanıcıyla da çalıştırabilirsiniz.

```bash
APP=/opt/dipbot
DEPLOY_USER="$(stat -c %U $APP/.git)"; echo "depo sahibi: $DEPLOY_USER"

# 0) Salt okunur durum (hiçbir şeyi değiştirmez)
sudo systemctl status dipbot --no-pager
sudo -u dipbot sqlite3 -readonly /var/lib/dipbot/dipbot.db 'PRAGMA user_version;'      # §2: 6 veya 4
ONCEKI_REV="$(sudo -u "$DEPLOY_USER" git -C $APP rev-parse HEAD)"; echo "ONCEKI_REV=$ONCEKI_REV"   # geri dönüş için NOT ALIN
sudo -u "$DEPLOY_USER" git -C $APP status --short                                       # boş olmalı
ls -ld /var/backups/dipbot /var/lib/dipbot; ls -l /var/lib/dipbot                       # sahip/izinleri gözle kontrol edin

# 1) Yalnız getir (birleştirme YOK); deponun sahibi olarak
sudo -u "$DEPLOY_USER" git -C $APP fetch origin main

# 2) YENİ yedek betiğini getirilen revizyondan kur (eski kopyayı kullanmayın)
sudo -u "$DEPLOY_USER" git -C $APP show FETCH_HEAD:deploy/backup.sh  | sudo tee /usr/local/sbin/dipbot-backup  >/dev/null
sudo -u "$DEPLOY_USER" git -C $APP show FETCH_HEAD:deploy/restore.sh | sudo tee /usr/local/sbin/dipbot-restore >/dev/null
sudo chown root:root /usr/local/sbin/dipbot-backup /usr/local/sbin/dipbot-restore
sudo chmod 0750 /usr/local/sbin/dipbot-backup /usr/local/sbin/dipbot-restore

# 3) YEDEK (zorunlu). Başarısızsa DURUN: kod/veri/servis henüz değişmedi.
sudo /usr/local/sbin/dipbot-backup           # "Yedek alindi: /var/backups/dipbot/dipbot-<damga>.db" yazmalı
sudo ls -l /var/backups/dipbot | tail -3     # yeni dosya 0600
sudo ls -l /var/lib/dipbot                   # dipbot.db(-wal/-shm) sahibi dipbot; root sahipli dosya OLMAMALI

# 4) Yalnız ileri sarma ile kodu güncelle (deponun sahibi olarak)
sudo -u "$DEPLOY_USER" git -C $APP merge --ff-only FETCH_HEAD
sudo -u "$DEPLOY_USER" git -C $APP diff --stat "$ONCEKI_REV"..HEAD -- requirements.txt   # boşsa pip gerekmez
#    requirements.txt değiştiyse: sudo $APP/.venv/bin/python -m pip install -r $APP/requirements.txt

# 5) Ön kontrol (ağ/gizli değer yok). HATA varsa servisi yeniden BAŞLATMAYIN.
sudo $APP/deploy/onkontrol.sh

# 6) Servis
sudo systemctl restart dipbot
sleep 5; sudo systemctl is-active dipbot
sudo journalctl -u dipbot -n 50 --no-pager --utc     # "Bot çalışıyor"; hata/uyarı yağmuru yok
```

Sonra yedek betiklerinin kurulu kopyalarını depodakiyle eşitleyin (zaten adım 2'de yapıldı; sonraki sürümlerde tekrar edin):
```bash
sudo install -m 0750 -o root -g root $APP/deploy/backup.sh  /usr/local/sbin/dipbot-backup
sudo install -m 0750 -o root -g root $APP/deploy/restore.sh /usr/local/sbin/dipbot-restore
```
Zamanlanmış yedek (systemd timer/cron) `/usr/local/sbin/dipbot-backup`'ı çağırıyorsa bu kopya güncel olunca otomatik olarak yeni davranışı kullanır.

**Bir adım başarısız olursa:** adım 3'te (yedek) → hiçbir şey değişmemiştir, nedeni giderin ve tekrarlayın. Adım 4'te `--ff-only` reddederse → kod değişmemiştir (yerel commit/sapma var; durun ve inceleyin). Adım 5'te HATA → servisi yeniden başlatmayın; çıktıdaki düzeltmeyi yapın veya §6 geri dönüş.

## 5. Sonraki güncellemeler: `guncelle.sh`

Bu sürüm dağıtıldıktan sonra (sunucudaki betikler yeni olduğunda):
```bash
sudo /opt/dipbot/deploy/guncelle.sh main
```
Sıra zorunludur: **(1)** yedek (canlı DB'ye `dipbot` olarak dokunur, doğrular; alınamazsa **kod/veri/servis değişmeden durur**), **(2)** git fetch + `merge --ff-only` (deponun sahibi olarak), **(3)** `pip install` + `onkontrol.sh` (hata verirse servisi yeniden **başlatmaz**, geri dönüş planını yazar), **(4)** `systemctl restart` ve `is-active`.
`guncelle.sh` fetch'te takılırsa (depo sahibinin SSH anahtarı/ağ) §4 elle yolunu kullanın; elle yolda da aynı güvenlik sırası geçerlidir. `guncelle.sh` `pip`'i root olarak çalıştırır (önceki davranış); `requirements.txt` değişmediyse bu adım etkisizdir.

## 6. Geri dönüş (yalnız kod; veritabanını gereksiz yere geri yüklemeyin)

PR #4 şemayı değiştirmediği için **normalde yalnız kodu önceki commit'e almak yeterlidir; yedekten DB geri yüklemeyin.**

```bash
APP=/opt/dipbot
DEPLOY_USER="$(stat -c %U $APP/.git)"
# ONCEKI_REV: §4 adım 0'da not ettiğiniz commit (veya /var/backups/dipbot/guncelleme-*.txt içindeki onceki_surum)
sudo -u "$DEPLOY_USER" git -C $APP checkout --detach <ONCEKI_REV>
sudo $APP/deploy/onkontrol.sh                 # isteğe bağlı
sudo systemctl restart dipbot
sleep 5; sudo systemctl is-active dipbot
sudo journalctl -u dipbot -n 50 --no-pager --utc
```
Not: kodu geri alınca Telegram arayüzü eski dile (Türkçe) döner; Telegram komut menüsü bir sonraki açılışta eski adlarla yeniden kaydedilir.

- **§2'de `user_version` zaten `6` idiyse:** bu geri dönüş eksiksizdir; şema her iki sürümde aynıdır.
- **§2'de `4` idiyse ve servis v6'ya geçtikten sonra eski (v4) koda dönüyorsanız:** eski kod `user_version=6` bir DB'yi açar (geçiş yalnız `user_version < SEMA_SURUMU` iken çalışır; eklenen sütunların varsayılanı vardır ve eski `INSERT`ler çalışır — mock testle doğrulandı, **gerçek veritabanıyla denenmedi**). Eski kod büyük partilerde yine "üye listesi tamamlanmadı" hatası verir ve XP süzgecini bilmez; bu veri bozulması değildir.
- **DB yedeğinden geri yükleme yalnız veri bozulmuşsa** (`integrity_check` ≠ `ok`, açılmayan DB): `sudo /usr/local/sbin/dipbot-restore /var/backups/dipbot/dipbot-<damga>.db` (servisi durdurur, mevcut DB'nin güvenlik yedeğini alır, yedeği yükler; **yedek tarihinden sonraki veriler kaybolur**). Bunu yalnız gerçekten gerekliyse yapın.
- Servis `dipbot.db` açamıyorsa (`unable to open database file` / `readonly database`) önce `ls -l /var/lib/dipbot` bakın: root sahipli `-wal/-shm` varsa **servisi durdurup** `sudo chown dipbot:dipbot /var/lib/dipbot/dipbot.db-wal /var/lib/dipbot/dipbot.db-shm` ile düzeltip servisi başlatın.

## 7. Miras bölüm (TARİHSEL — PR #4'te geçerli DEĞİLDİR, yalnız §2'deki `user_version=4` durumunda bilgi amaçlı)

Aşağıdakiler önceki sürümlerin (PR #2 ve PR #3) notlarıdır; **PR #4'ün parçası değildir.**

- **PR #2 (kısa komutlar):** `/oyuncuekle` vb. kısa komutlar, `diplomacia.com.tr` bağlantı ayrıştırma, sade savaş listesi, parti ilk verisi alımı, tek başlangıç özeti. Şema v4.
- **PR #3 (XP süzgeci, büyük parti):** tek kriter `xp ≥ 1.500.000` (oyuncu sayısı sınırı yok); API tek istekte 100 üye döndürürse liste **EKSİK okundu** olarak işaretlenir (tam liste sayılmaz, katılma/ayrılma bildirimi ve çıkarma üretmez). Şema v4 → **v6, yalnız ekleme:** `takip_partiler.kismi/toplam_uye/xp_sirali`, `parti_uyeleri.xp`, `takip_oyuncular.xp_alti_sayac/xp_onayli`; `PRAGMA user_version` 4 → 6; geçiş servis ilk açılışında `ALTER TABLE … ADD COLUMN` ile idempotent yapılır.
  - Mevcut otomatik eklenmiş oyuncular `xp_onayli=0` ile gelir ve XP kuralıyla **silinmez**; XP'si ≥ 1.500.000 görülünce `xp_onayli=1` olur ve art arda 3 eşik-altı gözlemden sonra kademeli çıkarma kuralına girer. Elle eklenenler hiçbir zaman XP kuralına tabi değildir. Davranış değişikliği: mevcut partilere sonradan katılan XP'si düşük/bilinmeyen üyeler artık otomatik takibe alınmaz.
  - Doğrulanmayanlar: `xp` alanının toplam XP olduğu; büyük partide gerçek sayfalama yöntemi (bot parametre uydurmaz; `journalctl`'da "üye listesi KISMİ okundu …" tanılama satırı vardır); üye ilk verisi alımının gerçek hız sınırlarıyla davranışı.
- **F2b öncesi (şema < 4)** bir sürüme dönüş bu planın konusu değildir; o durumda yedekten geri yükleme gerekir.

## 8. Bilinmeyenler (canlıda doğrulanmadı)
- Betiklerin gerçek sunucuda (systemd, gerçek `dipbot` kullanıcısı, gerçek `runuser`/`chown`/sahiplik, depo sahibinin SSH deploy anahtarı) davranışı.
- Telegram'da yeni komut menüsünün (`set_my_commands`) istemcilerde görünmesi; eski menü kısa süre önbellekte kalabilir (sohbeti kapatıp açın).
- Yeni İngilizce metinlerin gerçek Telegram'daki görünümü.
- Doğrulama listesi (mevcut gruplar için): [`CANLI-DOGRULAMA-PLANI.md`](CANLI-DOGRULAMA-PLANI.md) "PR #4 sonrası kontrol listesi".
