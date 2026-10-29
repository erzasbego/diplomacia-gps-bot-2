# Canlı doğrulama planları

Bu belge iki bölümden oluşur:
- **Bölüm A — Merkezi mimariye geçiş sonrası kontrol listesi:** çalışan sunucuda kısa duman testi. Önce bunu kullanın.
- **Bölüm B — Yeni sunucu için ilk kurulum doğrulama planı (tarihsel/ayrıntılı):** sıfırdan kurulum yapılacaksa. PR #4 dağıtımı için gerekli değildir.

---

# Bölüm A — Merkezi mimariye geçiş sonrası kontrol listesi

**Durum:** PLAN; çalıştırılmadı. Adım adım geçiş ve migration: [`KANAL-GECISI-PLANI.md`](KANAL-GECISI-PLANI.md). Bu liste geçişten sonraki duman testidir. **Gerçek takip kaydı eklemeyin/silmeyin** (`/addplayer`, `/addparty`, `/removeplayer`, `/removeparty`, `/scan <n>` gerçek veriyi değiştirir); yalnız okuma komutları ve gözlem.

| # | Kontrol | Beklenen |
|---|---|---|
| A.1 | `sudo systemctl is-active dipbot`; `sudo journalctl -u dipbot -n 80 --no-pager --utc` | `active`; "bildirim kanalı doğrulandı (id=-1003998002227)"; varsa "bekleyen bildirimler hizalandı…" tek satır; hata/uyarı yağmuru yok |
| A.2 | `ls -l /var/lib/dipbot`; `sudo /opt/dipbot/deploy/onkontrol.sh` | root sahipli `-wal/-shm` yok; `SONUC: HATA yok` |
| A.3 | Kurucu, bot **özel sohbet**: `/channel` | "✅ The channel exists and the bot can post messages there." (kanala test mesajı atılmaz) |
| A.4 | `/lists` | "Central tracking list": Players/Parties/Wars known = migrasyon analizindeki "to add" + önceki merkezi kayıtlar; "Migration: applied" (uygulandıysa) |
| A.5 | `/status` | İngilizce; `Group:` satırı **yok**; `Notification channel: … — ready`; Tracked Players/Parties, Scan Interval (merkezi), Last Successful Scan, Next Scheduled Scan, Scan in Progress, Pending Notifications; iki eski tanı satırı yok; saatler `DD.MM.YYYY HH:mm` İstanbul 24 saat |
| A.6 | `/players`, `/parties`, `/wars`, eski ad `/durum` | Merkezi liste görünür, İngilizce; grup/liste seçimi istenmez |
| A.7 | **Eski üç grupta** herhangi bir komut (`/start`, `/status`…) ve düğme | **Hiç yanıt yok**; eski gruplara hiçbir mesaj/uyarı düşmez; botu gruba yeniden ekleme/yönetici yapma hiçbir şey değiştirmez (kurucuya yalnız özel sohbette bilgi gelir) |
| A.8 | Başka bir kullanıcı bota özelden yazar | "In private chat, this bot is available to the owner only."; kurucuya yetkisiz deneme bildirimi |
| A.9 | Bir tarama aralığı bekleyin; gerçek bir değişiklik olduğunda | Bildirim **yalnız kanalda** (İngilizce, `Detected: …` İstanbul saati); eski gruplarda hiçbir şey yok; aynı olay tek kez |
| A.10 | `sqlite3` (dipbot olarak, `-readonly`): eski grup tablolarındaki satır sayıları | Migrasyon öncesi analizdeki sayılarla **aynı** (arşiv silinmedi/değişmedi) |
| A.11 | Kanaldan botu geçici çıkarma/yetki kaldırma (yalnız bilerek, test için) | Tarama duraklar, kurucuya "Notification channel is not ready"; bildirim kaybolmaz; yetki geri verilince bekleyenler tek kez teslim edilir ve "ready again" gelir |

---

# Bölüm B — Yeni sunucu için ilk kurulum doğrulama planı (tarihsel/ayrıntılı)

> **TARİHSEL — merkezi mimaride GEÇERSİZ olan adımlar:** 3.8, 3.9, 3.13 ve 3.14 (grup onayı, grup yetkisi, grup göçü/`/move_group`, grup içi komutlar) artık yoktur; gruplar komut işlemez. 3.10'daki komutlar kurucunun özel sohbetinde çalışır. Diğer satırlar geçerlidir.
>
> Aşağıdaki bölüm sıfırdan kurulum içindir; eski Türkçe-arayüz dönemine ait ekran metinleri artık **İngilizce**dir (ör. `/grup_onayla` = `/approve_group`, `Bot Status`). Eski komut adları hâlâ çalışır.

**Durum:** PLAN. Bu planın hiçbir adımı henüz çalıştırılmadı: gerçek Diplomacia isteği, gerçek Telegram bağlantısı ve VPS erişimi yoktur. Bulutta yalnız mock testler, `bash -n`, sahte dizin düzeninde `onkontrol.sh`, sahte `systemctl` ile yedek/geri yükleme ve `systemd-analyze verify` (Ubuntu 24.04 / systemd 255, statik) denendi. Aşağıdaki tabloların "Sonuç" sütunları bilerek boştur; **dolduruncaya kadar ilgili davranış doğrulanmış sayılmaz.**

## 0. Kural ve önkoşullar (sizin sağlayacağınız)

- **Yeni, güvenli** bir Diplomacia oturum tokenı. (Daha önce sohbete yapıştırılan token güvenli değildir; kullanmayın.) Token sohbete, komut satırı argümanına, ortam değişkenine, loga veya depoya konmaz; yalnız botun özel sohbet menüsünden ya da `0600` token dosyasına elle yazılır.
- Ayrı bir `BOT_TOKEN` (BotFather) ve sayısal `OWNER_TELEGRAM_ID`; bir **test grubu** (yalnız siz ve test kullanıcısı).
- Ubuntu 24.04 LTS VPS, `sudo` erişimi, çıkış HTTPS açık, gelen port gerekmez.
- Diplomacia'ya karşı **yük/zorlama testi yapılmaz**: 429 bilerek tetiklenmez; istekler tek tek, elle atılır.
- Her adımda sonucu ve tarihi "Sonuç" sütununa yazın; beklenenden farklıysa durun, bulguyu kaydedin.

## 1. Sunucu hazırlığı (Telegram/Diplomacia'ya bağlanmadan)

| # | Adım | Beklenen | Sonuç |
|---|---|---|---|
| 1.1 | `deploy/README-DEPLOY.md` "Kurulum" adımlarını uygulayın (servisi henüz **başlatmayın**) | Hatasız kurulum; `python3 --version` ≥ 3.11 | |
| 1.2 | `sudo /opt/dipbot/deploy/onkontrol.sh` | `SONUC: HATA yok`; yalnız beklenen UYARI'lar (DB/token henüz yok) | |
| 1.3 | `systemd-analyze verify /etc/systemd/system/dipbot.service` | Çıktı boş / uyarısız | |
| 1.4 | `timedatectl` | `System clock synchronized: yes`, `NTP service: active` | |
| 1.5 | `sudo -u dipbot env -i PYTHONPATH=/opt/dipbot/src /opt/dipbot/.venv/bin/python -m dipbot; echo $?` | Yapılandırma hatası mesajı, çıkış kodu **2** (ağ yok) | |
| 1.6 | `cd /opt/dipbot && PYTHONPATH=src .venv/bin/python -m pytest -q` | Bulutta: `1301 passed`. Sunucuda aynı sayı beklenir | |

## 2. Diplomacia — salt okunur API doğrulaması

Token yalnız bir başlık dosyasından okunur (komut satırında görünmez). Gerçek token ile:

```bash
umask 077
printf 'Authorization: Bearer %s\n' "$(sudo cat /var/lib/dipbot/diplomacia.token)" > /tmp/dip-hdr   # işlem sonunda: shred -u /tmp/dip-hdr
curl -sS -o /dev/null -w '%{http_code}\n' -H @/tmp/dip-hdr -H 'X-Client-Platform: web' https://diplomacia.com.tr/api/players/72400
```

| # | İstek | Beklenen (doğrulanacak varsayım) | Sonuç |
|---|---|---|---|
| 2.1 | `GET /api/players/72400`, geçerli Bearer + `X-Client-Platform: web` | 200, oyuncu JSON'u (bot ayrıştırıcısıyla uyumlu) | |
| 2.2 | Aynı istek **başlıksız** | 401 (kimlik gerektirdiğinin kanıtı; token doğrulaması buna dayanıyor) | |
| 2.2b | `GET /api/wars` **başlıksız** ve uydurma Bearer ile | Kimlik gerekiyor mu (401) yoksa herkese açık mı (200)? Açıksa `.prev` teyidi yalnız oyuncu/parti isteğine dayanır (kodda öyle); kaydedin | |
| 2.3 | Aynı istek **uydurma** Bearer (`Bearer SAHTE.SAHTE.SAHTE`) ile | 401 (sahte tokenın kabul edilmediğinin kanıtı) | |
| 2.4 | Geçerli Bearer, `X-Client-Platform` **olmadan** | Davranışı kaydedin (zorunlu mu?) | |
| 2.5 | `GET /api/players/<olmayan büyük numara>` | 404 (bot bunu "bulunamadı" sayar) | |
| 2.6 | `GET /api/parties/5830`, `/api/parties/5830/members`; ayrıca ≥101 üyeli bir parti (örn. 1319, 5170) | 200. Üye listesi sayfalıysa **sayfalama alanlarını** kaydedin. **Büyük partide:** `members` uzunluğu (beklenen 100), `party.member_count` (beklenen toplam), üyelerdeki `xp` değerlerinin **azalan sırada** olup olmadığı, **gerçek sayfalama yöntemi** (tarayıcı DevTools Network'te parti sayfasının kaç `/members` isteği attığı, parametreleri, `Link`/`X-Total-Count` başlıkları) — kaydedin; bot ek sayfa İSTEMEZ, parametre uydurmaz; `journalctl -u dipbot | grep "KISMİ okundu"` tanılama satırını da ekleyin | |
| 2.6b | ≥101 üyeli partinin `members` yanıtında `xp` alanı | Her üyede tamsayı `xp`; kaç üyenin ≥ 1.500.000 olduğu (özetteki sayıyla karşılaştırın); eksik/farklı tür `xp` var mı (bot bunları 'bilinmiyor' sayar ve eklemez) | |
| 2.7 | `GET /api/wars` ve `/api/wars/ended` | 200; sayfalama var mı, `status`/`war_type` değerleri, `attacker_power` anlamı ("hasar" mı?) | |
| 2.8 | Token JWT'sinin `exp` alanı (yalnızca süreyi okuyun, değeri paylaşmayın) | Gerçek ömrü kaydedin (haftalık hatırlatma varsayımını doğrular/değiştirir) | |
| 2.9 | Oyun içinde yeniden giriş yapınca eski token düşüyor mu? | Kaydedin (token yenileme akışının riski) | |
| 2.10 | Yanıt başlıklarında `Retry-After` / hız sınırı başlıkları | Kaydedin; 429 tetiklemeyin | |

İşlem sonunda `shred -u /tmp/dip-hdr`.

## 3. Telegram — bot ve token akışı

Önce `/etc/dipbot/dipbot.env` içine `BOT_TOKEN` ve `OWNER_TELEGRAM_ID` yazın, **token dosyası olmadan** `systemctl start dipbot`.

| # | Adım | Beklenen | Sonuç |
|---|---|---|---|
| 3.1 | `journalctl -u dipbot -n 30 --utc` | "Bot çalışıyor … Diplomacia bağlantısı: yapılandırıldı"; token yok → istek yok; log'da gizli değer yok | |
| 3.2 | Kurucu hesabıyla özel sohbette `/start` | Ana menü; komut listesi (`set_my_commands`) görünüyor | |
| 3.3 | Başka bir hesap özelde `/start` | Reddedilir; kurucuya yetkisiz deneme bildirimi (tek) | |
| 3.4 | Yönetim → Token Yenile → Yeni token gönder → **geçerli** token'ı tek mesajla gönder | Mesaj silinir; "oyuncu isteği başarılı; kaydedildi"; `ls -l` 0600; `journalctl`'da token yok | |
| 3.5 | Yönetim → Token Yenile → Yeni token gönder → **uydurma** JWT biçimli değer gönder | Reddedilir (401), dosya değişmez | |
| 3.6 | **Yeni token gönder**'e bas (bekleme başlar), 5 dk bekle, sonra mesaj gönder | "süresi doldu"; token işlenmez | |
| 3.7 | **Yeni token gönder**'e bas, `systemctl restart dipbot`, sonra mesaj gönder | Bekleme geçersiz ("Önce menüden …") — yeniden başlatma bekleyen işlemi iptal eder | |
| 3.8 | Botu **onaysız** gruba ekle | Bot cevap vermez/işlem yapmaz; kurucuya tek bildirim | |
| 3.9 | Kurucu özelden `/approve_group <test grubu id>` (eski ad `/grup_onayla` da çalışır), sonra grupta yetkisiz kullanıcı komut dener | Yetkisiz reddedilir; kurucuya bildirim; yetkili kullanıcıya `/grant` (eski ad `/yetki_ver`) sonrası çalışır | |
| 3.10 | Grupta `/addplayer 72400` ve `/addplayer <profile/press bağlantısı>`, `/addparty 1244` (üye ilk verisi alınıyor mu, tek özet mi), eski `/oyuncu_ekle` takma adı, `/wars` (sade liste + emoji), düğmeler | Metinler **İngilizce**, HTML bozulmaz; ilk eklemede değişiklik bildirimi yok; savaş bağlantısı tıklanır (`/wars/war/{numara}`) | |
| 3.11 | Bir sonraki taramada (10 dk) değişiklik yoksa | Bildirim yok; `/status` son tarama zamanını (İstanbul saati, 24 saat) gösterir | |
| 3.12 | Gizlilik modu (BotFather `/setprivacy`) | Komutlar grupta çalışıyor; kararı kaydedin | |
| 3.13 | Test grubunu süper gruba yükseltin (göç) | Kurucuya göç bildirimi; eski id düşer; `/approve_group <yeni>` + `/move_group <eski> <yeni>` (eski adlar `/grup_onayla`, `/grup_tasi`) onayla taşır (veri korunur) | |
| 3.14 | Sahte onay butonu: eski mesajdaki düğmeye yetkisi alınmış kullanıcıyla bas | Reddedilir | |

## 4. Dayanıklılık ve işletim

| # | Adım | Beklenen | Sonuç |
|---|---|---|---|
| 4.1 | `systemctl stop dipbot` (taramanın ortasında da deneyin) | ≤ 10 sn'de kapanır, çıkış 0, DB bütünlüğü `ok` | |
| 4.2 | `sudo kill -9 $(systemctl show -p MainPID --value dipbot)` | 10 sn sonra otomatik yeniden başlar; takipler korunur; bildirimler "en az bir kez" teslim edilir (çökme anında bir bildirim **bir kez tekrarlanabilir**; gelen tekrar sayısını kaydedin, çoğalma/sel olmamalı) | |
| 4.3 | Sunucu yeniden başlatma (`reboot`) | Servis kendiliğinden kalkar | |
| 4.4 | Yanlış `BOT_TOKEN` ile başlat | Çıkış 3, otomatik yeniden başlamaz | |
| 4.5 | Aynı `BOT_TOKEN` ile ikinci örnek | `Conflict` alan örnek çıkış 3 ile kapanır. **Hangisinin Conflict aldığını kaydedin** (Telegram çoğunlukla eskisine verir; üretim örneği çıkabilir — bu durumda ikinci örneği ASLA aynı token'la başlatmayın) | |
| 4.6 | Ağı kes (`iptables`/ağ kapat, test VPS'inde) 2 dk, sonra aç | Hata yağmuru yok; taramalar devam; bildirimler kuyruktan gider (en az bir kez) | |
| 4.7 | `sudo /usr/local/sbin/dipbot-backup` (servis çalışırken) | `.db` 0600, `integrity_check ok`, eski yedekler `KEEP`'e göre silinir; sqlite3 `dipbot` olarak çalışır: `ls -l /var/lib/dipbot` sonrası **root sahipli `-wal/-shm` yok**; mevcut `/var/backups/dipbot` sahip/izni değişmedi | |
| 4.8 | `sudo dipbot-restore <yedek>` | Servis durur/kalkar; veri yedekteki hâle döner; güvenlik yedeği oluşur | |
| 4.9 | `.prev` yaşam döngüsü: token yenile → `ls -l diplomacia.token.prev` | `.prev` oluşur (0600); ilk başarılı taramadan sonra (`journalctl`: "…ilk başarılı yetkili taramayla teyit edildi") **silinir**; silinmezse 24 sa sonra silinir | |
| 4.10 | Saat güvenliği — **yalnız özel test VPS'inde**: onay isteyen bir işlemi (örn. `/oyuncusil 72400`) başlat; `timedatectl set-ntp false && date -s '+1 hour'`; onay düğmesine bas; sonra `timedatectl set-ntp true` | "Sunucu saati değişti … yeniden başlatın"; işlem uygulanmaz | |
| 4.11 | Token'ı sunucuda elle sil (`rm`) | Bot istek atmaz, "token dosyası yok" der, hata yağmuru yok; menüden token verince devam eder | |
| 4.12 | Geçersiz (süresi dolmuş) token ile tarama | Kurucuya **tek** "token geçersiz" mesajı; taramalar durur; yeni token sonrası devam | |
| 4.13 | 24 saat gözlem: `journalctl -p warning`, RAM/CPU (`systemctl status`), bildirim sayıları | Bellek sızıntısı/log yağmuru yok | |

## 5. Sonuç raporu şablonu

Ayrı iki başlık kullanın, karıştırmayın:

- **Canlıda doğrulandı:** (tablo satır numaraları, tarih, kısa kanıt — token/kimlik içermeden)
- **Canlıda doğrulanamadı / sapma:** (beklenen, gözlenen, kod etkisi)

Sapma bulunursa: ilgili `API-ARASTIRMA.md` / `findings.md` girdisini güncelleyin ve düzeltmeyi önce hata gösteren testle yapın.

## 6. Acil durdurma ve geri alma

- `sudo systemctl stop dipbot` her zaman güvenlidir (zarif kapanış).
- Token sızdığından şüphelenirseniz: Diplomacia hesabında oturumu kapatıp yeniden girin (eski token düşer), token dosyasını silin (`sudo shred -u /var/lib/dipbot/diplomacia.token*`), botun menüsünden yeni token verin.
- `BOT_TOKEN` sızarsa BotFather'da `/revoke`, `dipbot.env`'i güncelleyip servisi yeniden başlatın.
- Kötü güncelleme: depo sahibi olarak `git checkout --detach <önceki sürüm>` + `systemctl restart dipbot` (ayrıntı: GUNCELLEME-PLANI.md §6); yalnız veri bozulduysa `dipbot-restore`.
