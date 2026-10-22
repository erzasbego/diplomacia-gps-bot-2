# Canlı doğrulama planları

Bu belge iki bölümden oluşur:
- **Bölüm A — PR #4 sonrası kontrol listesi (mevcut gruplar):** çalışan sunucuda, mevcut iki grupta kısa duman testi. Önce bunu kullanın.
- **Bölüm B — Yeni sunucu için ilk kurulum doğrulama planı (tarihsel/ayrıntılı):** sıfırdan kurulum yapılacaksa. PR #4 dağıtımı için gerekli değildir.

---

# Bölüm A — PR #4 sonrası kontrol listesi (mevcut gruplar)

**Durum:** PLAN; çalıştırılmadı. Kapsam: İngilizce arayüz, İstanbul saati, sade `/status`, eski komut takma adları, mevcut veri ve yetki. **Gerçek takip kaydı eklemeyin/silmeyin** (`/addplayer`, `/addparty`, `/removeplayer`, `/removeparty`, `/approve_group`, `/remove_group`, `/grant`, `/revoke`, `/move_group`, `/scan <n>` deneme amacıyla kullanılmaz; bunlar gerçek veriyi değiştirir). Yalnız okuma komutları kullanılır.

Mevcut gruplar: `-1004375821471` ve `-1004485206993`. Komutları, o grupta zaten yetkili olan kullanıcı/kurucu hesabıyla yazın.

## A.0 Güncellemeden ÖNCE temel çizgi (salt okunur; sonuçları kaydedin)
```bash
sudo -u dipbot sqlite3 -readonly /var/lib/dipbot/dipbot.db <<'SQL'
.headers on
SELECT 'onayli' k, group_id, donem FROM onayli_gruplar ORDER BY 2;
SELECT 'yetkili' k, COUNT(*) FROM yetkili_kullanicilar;
SELECT 'oyuncu' k, group_id, COUNT(*) FROM takip_oyuncular GROUP BY 2 ORDER BY 2;
SELECT 'parti' k, group_id, COUNT(*) FROM takip_partiler GROUP BY 2 ORDER BY 2;
SELECT 'savas' k, group_id, COUNT(*) FROM savaslar GROUP BY 2 ORDER BY 2;
SELECT 'aralik' k, group_id, tarama_araligi_dk FROM grup_ayarlari ORDER BY 2;
SELECT 'outbox_bekleyen' k, COUNT(*) FROM outbox WHERE durum='bekliyor';
PRAGMA user_version;
SQL
```
Güncellemeden sonra aynı sorguyu tekrarlayın: onaylı gruplar, yetkili sayısı, oyuncu/parti/savaş sayıları ve tarama aralıkları **aynı** olmalı (oyuncu/parti/savaş sayıları tarama sonucu doğal olarak değişebilir; **azalma** şüphelidir). `user_version` yalnızca §2'de (GUNCELLEME-PLANI) açıklanan 4 → 6 durumunda değişir.

## A.1 Servis
| # | Kontrol | Beklenen |
|---|---|---|
| A.1.1 | `sudo systemctl is-active dipbot` ve `sudo journalctl -u dipbot -n 50 --no-pager --utc` | `active`; "Bot çalışıyor"; hata/uyarı yağmuru yok |
| A.1.2 | `ls -l /var/lib/dipbot` | `dipbot.db` (varsa `-wal`, `-shm`) sahibi `dipbot`; **root sahipli dosya yok** |
| A.1.3 | `sudo /opt/dipbot/deploy/onkontrol.sh` | `SONUC: HATA yok` |

## A.2 Telegram — her iki grupta (yalnız okuma komutları)
| # | Komut / eylem | Beklenen |
|---|---|---|
| A.2.1 | `/status` | Tümü İngilizce: `Bot Status`, `Group`, `Tracked Players`, `Tracked Parties`, `Scan Interval`, `Last Successful Scan` (Players / Parties / Wars), `Next Scheduled Scan`, `Scan in Progress`, `Pending Notifications`. Sayılar A.0 ile uyumlu. |
| A.2.2 | `/status` içinde şu iki metin **hiçbir zaman** olmamalı: "Liste sayfalamasız varsayılmıştır" ve "yapılandırıldı (… canlı bağlantı test edilmedi …)". Ayrıca "Diplomacia connection:" satırı **yalnız** gerçek bir sorun varsa çıkar (sağlıklı durumda yok). Yenile (🔄 Refresh) düğmesini de deneyin. |
| A.2.3 | `/help` | `/addplayer /players /removeplayer /addparty /parties /removeparty /wars /scan /status /grant /revoke /approve_group /remove_group /move_group` İngilizce açıklamalarla listelenir; Türkçe komut adı listelenmez. |
| A.2.4 | `/players`, `/parties`, `/wars` | Mevcut yetkilere göre çalışır; mevcut kayıtlar listelenir (A.0 sayılarıyla uyumlu), metinler İngilizce, sayfalama/düğmeler çalışır. `/wars` listesinde "Note: the list is assumed to be unpaginated (unverified)." notu beklenir (yalnız `/status`ten kaldırıldı). |
| A.2.5 | Eski takma adlar: `/durum`, `/oyuncular`, `/partiler`, `/savaslar`, `/yardim` | Aynı ekranlar, **İngilizce** yanıt. |
| A.2.6 | Tüm gösterilen zamanlar (`Last Successful Scan`, `Next Scheduled Scan`, savaş/ayrıntı ekranları, bildirimlerdeki `Detected:`/`Time:`) | Biçim `DD.MM.YYYY HH:mm`, **24 saat, AM/PM yok**, **İstanbul saati**. Kontrol: sunucuda `date -u` çıktısı + 3 saat = ekrandaki saat. |
| A.2.7 | Yetkisiz bir hesap (gruptaki sıradan üye) `/players` veya `/status` yazar | Reddedilir ("You are not authorized to perform this action."); kurucuya (hız sınırlı) yetkisiz deneme bildirimi gider (komut adları İngilizce görünür, ör. `/players`). Sıradan üyeye hiçbir yeni yetki verilmemiştir. |
| A.2.8 | Onaysız bir grupta bot komutu (varsa test grubu; **yeni grup onaylamayın**) | Bot sessiz kalır; kurucuya tek bildirim. Yoksa bu satırı atlayın. |
| A.2.9 | Kurucu özel sohbette `/start` → ana menü; ilgili düğmeler (Players/Parties/Wars/Scan Settings/Bot Status/Administration/Help) | İngilizce; menü açılır. (Scan Settings ekranını **açabilirsiniz, aralığı değiştirmeyin**.) |

## A.3 Otomatik tarama ve bildirim
| # | Kontrol | Beklenen |
|---|---|---|
| A.3.1 | `/status` → `Next Scheduled Scan` ve `Scan Interval`; en az bir aralık (10 dk varsayılan) bekleyip tekrar bakın | `Last Successful Scan` zamanları ilerler; `Next Scheduled Scan` mevcut aralıkla (A.0'daki `aralik`) ileri kayar; sunucu saatinden **ileride/anormal** bir tarih yok (eski saat atlaması sorunu yaşanmadı). |
| A.3.2 | `journalctl -u dipbot --since "-30 min" -p warning` | Uyarı/hata yağmuru yok |
| A.3.3 | Bildirimler: bir sonraki gerçek değişiklikte (oyuncu bölge/vatandaşlık/ad, parti üyeliği, yeni savaş) gelen mesaj | İngilizce; `Detected: DD.MM.YYYY HH:mm` İstanbul saati; Telegram'a teslim edilir. Beklerken `Pending Notifications` kuyruğu birikmiyor (0'a iniyor). Yapay değişiklik **üretmeyin**. |
| A.3.4 | Kurucunun özel sohbetinde (denetim/yetkisiz deneme/token hatırlatma) bildirimi geldiyse | İngilizce, saatler İstanbul. |

## A.4 Veri değişmedi mi?
A.0 sorgularını tekrarlayın: onaylı gruplar, yetkili sayısı, tarama aralıkları **aynı**; oyuncu/parti sayıları azalmamış; `outbox_bekleyen` makul. İki grubun listeleri birbirine karışmamış (A.2.4'te her grupta yalnız kendi kayıtları görünür).

## A.5 Sorun halinde
Önce GUNCELLEME-PLANI.md §6 (yalnız kodu önceki commit'e alma). Veritabanını yedekten geri yüklemek yalnız veri bozulmuşsa gerekir.

---

# Bölüm B — Yeni sunucu için ilk kurulum doğrulama planı (tarihsel/ayrıntılı)

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
| 1.6 | `cd /opt/dipbot && PYTHONPATH=src .venv/bin/python -m pytest -q` | Bulutta: `1217 passed`. Sunucuda aynı sayı beklenir | |

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
