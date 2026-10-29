# Çok hesaplı tarama: kurulum, kullanım ve güvenli geçiş planı

**Durum:** PLAN. Bu belge hazırlanırken canlı VPS'e, Telegram'a veya Diplomacia'ya dokunulmadı; hiçbir şey dağıtılmadı. Tek `dipbot.service`, tek SQLite, tek Telegram botu ve bildirim kanalı `-1003998002227` korunur; eski gruplar kapalı kalır; `takip-bot` servisine dokunulmaz.

## 1. Ne var?
- **Birden çok Diplomacia hesabı**, hepsi **Telegram'dan** yönetilir (VPS terminalinde elle token/proxy girmek gerekmez): *Account Management* menüsü (`/accounts` veya ana menü → 🔐 Account Management) — yalnız `OWNER_TELEGRAM_ID`, yalnız botun özel sohbeti. Düğmeler: **View Accounts · Add Account · Account Details · Update JWT Token · Proxy Settings · Test Connection · Assigned Players · Account Statistics**.
- **Hesap başına oyuncu ekranı:** Account → Players: yalnız o hesaba atanmış oyuncular; oyuncu ID, ad, son başarılı tarama zamanı, son taramada hata var mı, takip durumu; sayfalı (8/sayfa); ID/ad ile arama; oyuncuya tıklayınca mevcut oyuncu ayrıntısı.
- **Hesap istatistikleri:** atanmış oyuncu sayısı (`150/150`, `55/150`), başarıyla taranan, bekleyen görevler, son başarılı API isteği, son rate limit, aktif cooldown, proxy bağlantı durumu, son tarama süresi.

## 2. Görev dağıtımı (kesin kural)
- **Her hesap en fazla 150 oyuncudan sorumludur** (`config.HESAP_KAPASITESI`). **Bu sabit bir görev atama kapasitesidir; API'nin 150 oyuncuyu belirli sürede sorgulamaya izin verdiği varsayımı DEĞİLDİR** — gerçek hız/kota koruması ayrıdır (bkz. §5).
- Hesaplar **sırayla doldurulur** (kullanıcı hesap seçmez; `/addplayer` ve parti üzerinden otomatik ekleme merkezi sisteme devam eder): 100 → `100`; 205 → `150/55`; 300 → `150/150`; 301 → `150/150` + 1 oyuncu **beklemede**; üçüncü hesap eklenince `150/150/1`; 450 ve üç hesap → `150/150/150`.
- **Kapasite dolunca:** yeni oyuncu silinmez/kaybolmaz, veritabanında bekler; kurucuya yalnızca gerekli uyarı gider (**“A new Diplomacia account is required”**, 24 saatte en fazla bir kez); hesabı siz eklersiniz, bekleyenler **otomatik atanır**. **Hesaplar kendiliğinden oluşturulmaz.**
- Oyuncu sayısı azalırsa boşalan yerler sıralı doldurma kuralına göre **en az taşımayla** kapatılır; ekleme mevcut görevleri kıpırdatmaz.
- **Atama değişince oyuncu geçmişi ve bildirim karşılaştırma verisi (bölge, vatandaşlık, son gözlem, baseline) sıfırlanmaz**; veri oyuncu tablosundadır, atama yalnız “hangi hesap tarasın” bilgisidir. Atamalar SQLite'ta kalıcıdır (yeniden başlatmada korunur).

## 3. Arıza davranışı
- **Devralma yok:** bir hesap arıza verirse (token reddi, proxy erişimi yok, rate limit, 403, ağ) **yalnız o hesabın görevleri bekler**; başka hesap onun oyuncularını **taramaz**. Hesap düzelince kaldığı yerden devam eder. Durum ve bekleme süreleri kalıcıdır.
- **Proxy:** proxy yapılandırılmış hesapta proxy erişimi kesilirse **doğrudan bağlantıya otomatik geçilmez**; hesap bekler (üstel geri çekilme 60 sn → 15 dk), proxy dönünce devam eder. Doğrudan bağlantıya yalnız siz *Proxy Settings → Remove proxy* ile (onay düğmesiyle) geçebilirsiniz.
- **Token reddi (401):** yalnız o hesap duraklar, kurucuya özel sohbette **tek** uyarı gider; *Update JWT Token* ile yeni token **önce sınanır**, geçerliyse kaydedilir ve hesap devam eder.
- Parti/savaş taramaları ve komutlardaki etkileşimli istekler **birincil hesapla** (hesap kimliği en küçük olan) yapılır; birincil hesap bozuksa bunlar bekler (oyuncu taramaları diğer hesaplarda sürer).

## 4. Gizlilik ve güvenlik
- **Yalnız kurucu**, **yalnız özel sohbet**. Gruplar ve kanal komut/düğme işlemez.
- JWT ve proxy (adres, kullanıcı adı, parola) **yalnız VPS'te** `<veri dizini>/hesaplar/<id>/{token,token.prev,proxy}` dosyalarında tutulur: **dosyalar 0600, dizinler 0700**, atomik yazım (umask'tan bağımsız zorlanır). **SQLite'ta yalnız** hesap kimliği/etiketi, atamalar, bağlantı durumu ve gizli olmayan bilgiler vardır (proxy için yalnız “var” bayrağı ve şema `http`/`https`; adres/kimlik asla). Yedekler (`dipbot-backup`) yalnız SQLite'tır — sırlar yedeğe girmez.
- **Hiçbir yanıtta, hata mesajında, günlükte, GitHub'da ve Telegram kanalında** token/proxy bilgisi görünmez (Account Details yalnız “stored / configured (http)” gösterir).
- Token ve proxy bilgisi yalnız ilgili “bekleme” adımında (Add Account / Update JWT Token / Proxy Settings) ve **5 dakika** içinde kabul edilir; gönderdiğiniz mesaj **hemen silinmeye çalışılır**. **Silinemezse bot bunu açıkça söyler** ve elle silmenizi ister. **Telegram Bot API sohbetleri uçtan uca şifreli DEĞİLDİR**: mesaj Telegram sunucularından geçer ve silinene kadar sohbeti görebilen herkesçe okunabilir; şüphe varsa token'ı yenileyin. Bekleme yokken gelen JWT biçimli mesaj da silinir ve işlenmez.
- Güncellemede **önce sınanır**: aday token, hesabın proxy'si üzerinden bir oyuncu isteğiyle; aday proxy, hesabın mevcut token'ıyla. Başarısızsa (401, 403, zaman aşımı, proxy erişilemez) **önceki geçerli yapılandırma korunur**. Yeni hesap yalnız doğrulama başarılıysa oluşturulur.
- `deploy/onkontrol.sh` hesap dizin/dosya izinlerini (0700/0600), sahibini (`dipbot`) ve veritabanında JWT benzeri değer olmadığını denetler.

## 5. Hız ve kota koruması (aşma sistemi YOKTUR)
- Genel eşzamanlılık sınırı **tüm hesaplar için ortaktır** (`config.GENEL_ESZAMANLI`): hesap eklemek genel kotayı artırmaz. Hesap başına eşzamanlılık `HESAP_ESZAMANLI`, istekler arası bekleme `HESAP_ISTEK_ARALIGI_SN` (hesap) ve `GENEL_ISTEK_ARALIGI_SN` (genel). Bu sabitler canlı gözleme göre ayarlanmalıdır (**gerçek API sınırları bilinmiyor/doğrulanmadı**).
- **429:** hesap için bekleme penceresi (Retry-After veya üstel varsayılan); **aynı çıkış noktasını paylaşan hesaplar** (ikisi de doğrudan bağlantı veya aynı proxy host:port) da bekler. **403:** kısa geri çekilme. Rate limit/erişim kontrollerini aşmaya yönelik otomatik hesap/IP değiştirme **yoktur**.
- Bir hesap ne kadar uzun sürede tarayabildiği gerçek gecikmeye bağlıdır; 150 görev tek turda bitmek zorunda değildir (tarama sırayla, hesap başına tek istek). Başlangıçta aralığı kısa tutmayın; `/status` ve hesap istatistiklerinden “son tarama süresi”ni izleyin.

## 6. Dağıtım adımları (siz uygularsınız; otomatik dağıtım YOKTUR)
```bash
APP=/opt/dipbot; DEPLOY_USER="$(stat -c %U $APP/.git)"
ONCEKI_REV="$(sudo -u "$DEPLOY_USER" git -C $APP rev-parse HEAD)"; echo "ONCEKI_REV=$ONCEKI_REV"
sudo /usr/local/sbin/dipbot-backup                                   # 1) yedek (şema v7: yalnız yeni tablolar eklenir)
sudo -u "$DEPLOY_USER" git -C $APP fetch origin <dal-veya-main>      # 2) yalnız ileri sar
sudo -u "$DEPLOY_USER" git -C $APP merge --ff-only FETCH_HEAD
sudo $APP/deploy/onkontrol.sh                                        # 3) HATA yok olmalı (hesap dizini henüz yoksa UYARI normaldir)
sudo systemctl restart dipbot && sleep 5 && sudo systemctl is-active dipbot
sudo journalctl -u dipbot -n 60 --no-pager --utc
```
Sonra **Telegram'da (özel sohbet)**:
1. `/accounts` → hesap yoksa: **📥 Import existing token as Account 1** (mevcut tek-token dosyasını doğrulayıp kopyalar; eski dosya yerinde kalır) **veya** ➕ Add Account.
2. İkinci hesap için ➕ Add Account → ad → (isteğe bağlı) proxy → JWT. Mesajların silindiğini doğrulayın; silinemediyse bot uyarır.
3. `/accounts` → *View Accounts* sayıları (`150/150`, `55/150`), *Account Statistics*, *Assigned Players*, *Test Connection* ile doğrulayın.
> Yeniden başlatmadan sonra, hesap içe aktarılana/eklenene kadar **hiçbir oyuncu taranmaz** ve kurucuya “new account required” uyarısı gelir (oyuncular silinmez).

## 7. Doğrulama listesi (gerçek kayıt eklemeden/silmeden)
`/status` → `Diplomacia Accounts: N (M ready)`; her hesapta *Account Details* durumu `ready`; *Assigned Players* yalnız o hesabın oyuncularını gösterir; sayılar toplamda `Tracked Players` ile tutarlı; `sudo ls -lR /var/lib/dipbot/hesaplar` → dizinler `drwx------`, dosyalar `-rw-------`, sahibi `dipbot`; günlükte token/proxy yok; bir tarama aralığı sonra `Last Successful Scan` ilerler; bildirimler yalnız `-1003998002227` kanalına gelir.

## 8. Geri dönüş
Yalnız kod: servis durdurulur, `sudo -u "$DEPLOY_USER" git -C $APP checkout --detach $ONCEKI_REV`, servis başlatılır. Şema yalnız **yeni tablolar** ekledi; eski kod bunları yok sayar. Eski tek-token dosyası (`diplomacia.token`) içe aktarmada **kopyalandığı** için yerinde kalır ve eski kod onu kullanmaya devam eder. Hesap dizinindeki sırlar kalır (silmek için elle `shred -u`).

## 9. Bilinenler / canlıda doğrulanmadı
- Gerçek Diplomacia hız/kota sınırları, 429/403 anlamları ve çoklu hesapla davranış.
- Proxy türleri: **http/https** desteklenir (SOCKS desteklenmez); gerçek proxy ile bağlantı canlıda denenmedi (testler yerel kapalı port ve sahte aktarıcıyla doğruladı: proxy kapalıyken site sunucusuna doğrudan istek gitmez).
- Telegram'ın kurucunun mesajını silme izinleri (bazı durumlarda silinemeyebilir; bot bunu bildirir).
- Büyük partilerde/çok oyunculu durumlarda gerçek tarama süresi.
