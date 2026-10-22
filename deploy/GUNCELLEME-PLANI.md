# Canlı VPS güvenli güncelleme planı (kısa komutlar, parti ilk verisi ve büyük parti ilk-100 sürümleri)

**Durum:** PLAN. Bu belge hazırlanırken canlı VPS'e dokunulmadı: hiçbir dosya değiştirilmedi, `dipbot` servisi durdurulmadı veya yeniden başlatılmadı. Aşağıdaki adımları siz sunucuda çalıştırırsınız. `guncelle.sh` bulutta yalnız sahte dizin, sahte `systemctl`/`pip` ve yerel git depolarıyla test edildi; **gerçek sunucuda çalıştırılmadı.**

## Bu sürümde ne değişiyor?
- **İngilizce arayüz ve İstanbul saati (en son sürüm):** Telegram arayüzünün tamamı İngilizce; komut menüsü İngilizce (`/addplayer`, `/players`, `/removeplayer`, `/addparty`, `/parties`, `/removeparty`, `/wars`, `/scan`, `/status`, `/help`, `/grant`, `/revoke`, `/approve_group`, `/remove_group`, `/move_group`); eski Türkçe adlar çalışmaya devam eder. Gösterilen tüm saatler Europe/Istanbul, 24 saat (`DD.MM.YYYY HH:mm`); veritabanı/zamanlayıcı UTC kalır. `/status` sadeleşti. **Şema, yetki, grup ve tarama davranışı değişmedi; veritabanı geçişi yoktur.** Güncellemeden sonra komut menüsü Telegram'da yenilenir (bot açılışta `set_my_commands` çağırır; istemci önbelleği için sohbeti yeniden açmak gerekebilir).
- Komutlar: `/oyuncuekle`, `/oyuncusil`, `/partiekle`, `/partisil` (eski alt çizgili adlar çalışmaya devam eder).
- `diplomacia.com.tr` bağlantıları: `/player/ID`, `/party/ID` ve `profile/` / `press/` bağlamlı biçimleri.
- Sade savaş listesi, tutarlı emojiler.
- `/partiekle`: üyelerin ilk bölge/vatandaşlık verisi sınırlı eşzamanlılıkla alınır; tek başlangıç özeti; ilk bilgi bildirim değildir.
- **Büyük partiler (API tek istekte 100 üye döndürür; örn. 100/329, 100/129):** artık eklenebilir; okunan üyeler değerlendirilir, liste **EKSİK okundu** diye açıkça bildirilir (tam liste sayılmaz), sayfalama parametresi uydurulmaz, görünmeme üyelik değişikliği sayılmaz (ayrıntı: README "Büyük partiler").
- **XP süzgeci — tek kriter:** parti üyelerinden yalnız `xp ≥ 1.500.000` olanlar otomatik takibe alınır (oyuncu sayısı sınırlanmaz); XP eksik/geçersiz olan eklenmez; takipten çıkarma yalnız art arda 3 geçerli eşik-altı gözlemle ve yalnız otomatik, XP'den geçmiş, başka partide nitelikli olmayan oyuncular için.

## Veritabanı şeması DEĞİŞİYOR: v4 → v6 (yalnız EKLEME, mevcut kayıtlar korunur)
Eklenen sütunlar (hepsi güvenli varsayılanlı): `takip_partiler.kismi/toplam_uye/xp_sirali` (v5, kısmi okuma) ve `parti_uyeleri.xp`, `takip_oyuncular.xp_alti_sayac/xp_onayli` (v6, XP süzgeci); `PRAGMA user_version` 4 → 6. Geçiş servis **ilk açılışında** otomatik yapılır (`ALTER TABLE ... ADD COLUMN`, idempotent); satır silinmez/yeniden yazılmaz. Bu yüzden:
- Mevcut **grup onayları, yetkili kullanıcılar, takip edilen oyuncular/partiler, engeller, savaş kayıtları, bekleyen bildirimler aynen korunur**. Eski partiler `sinirli=0, toplam_uye=NULL` ile "tam liste" olarak devam eder (ilk taramada gerçek değerler yazılır).
- **XP süzgeci ve mevcut oyuncular:** bu sürümden önce otomatik eklenmiş oyuncular `xp_onayli=0` ile gelir ve **XP kuralıyla SİLİNMEZ**; XP'si ≥ 1.500.000 olarak ilk kez gözlendiklerinde `xp_onayli=1` olur ve o andan itibaren kademeli çıkarma kuralına (art arda 3 eşik-altı gözlem) tabidir. Elle eklenenler hiçbir zaman XP kuralına tabi değildir. **Davranış değişikliği:** mevcut partilere sonradan katılan XP'si düşük/bilinmeyen üyeler artık otomatik takibe alınmaz.
- **Yedek bu yüzden önemlidir:** `guncelle.sh` yedeği ZORUNLU kılar; yedek `user_version=4` durumundadır.
- **Geri dönüş:** v4 kodu v6 veritabanını sorunsuz açar (ek sütunların varsayılanı vardır; eski `INSERT`ler çalışır — testli), yani çoğu durumda yalnız kodu eski commit'e almak yeterlidir. v4 kodu büyük partilerde (ilk 100) yine "üye listesi tamamlanmadı" hatası verir ve XP süzgecini bilmez (tüm üyeleri otomatik ekler); bu veri bozulması değildir. Beklenmedik durumda yedekten geri yükleyin.
- Bu PR'dan **önceki** (F2b öncesi, şema < 4) sürüme dönüş bu planın konusu değildir; o durumda yedekten geri yükleme gerekir.

## Önkoşullar
- PR birleştirilmiş, CI yeşil, güncellenecek ref (`main` veya bir commit/etiket) belli.
- `/opt/dipbot` temiz git deposu (yerel değişiklik yok), `sqlite3` kurulu, `deploy/` betikleri güncel.
- `OWNER_TELEGRAM_ID` hesabıyla Telegram'a erişiminiz var (güncelleme sonrası duman testi için).

## Adımlar
1. **Önce salt okunur durum (hiçbir şeyi değiştirmez):**
   ```bash
   sudo systemctl status dipbot --no-pager
   sudo /opt/dipbot/deploy/onkontrol.sh
   git -C /opt/dipbot rev-parse HEAD            # mevcut sürümü not alın
   ```
2. **Tek komutla güvenli güncelleme** (sıra zorunlu: yedek → kod → ön kontrol → servis):
   ```bash
   sudo /opt/dipbot/deploy/guncelle.sh main     # veya bir commit/etiket
   ```
   Betik: (a) DB'nin `sqlite3 .backup` yedeğini alıp `integrity_check` ile doğrular — **alamazsa veya doğrulayamazsa kodu, veritabanını ve servisi değiştirmeden durur**; (b) önceki sürüm + yedek yolu + şema sürümünü `guncelleme-*.txt` kaydına yazar; (c) yalnız ileri sarma (`ff-only`) ile kodu günceller; (d) `pip install -r requirements.txt` ve `onkontrol.sh` çalıştırır (hata verirse **servisi yeniden başlatmaz**, geri dönüş planını yazar); (e) `systemctl restart dipbot` ve `is-active` denetimi yapar.
3. **Elle yapmak isterseniz** (aynı sıra):
   ```bash
   sudo /usr/local/sbin/dipbot-backup                       # 1) yedek; çıktıdaki dosyayı not alın
   sqlite3 -readonly /var/backups/dipbot/dipbot-<damga>.db 'PRAGMA integrity_check;'   # 'ok' bekleyin
   git -C /opt/dipbot fetch origin main && git -C /opt/dipbot merge --ff-only FETCH_HEAD
   /opt/dipbot/.venv/bin/python -m pip install -r /opt/dipbot/requirements.txt
   sudo /opt/dipbot/deploy/onkontrol.sh
   sudo systemctl restart dipbot && sudo systemctl is-active dipbot
   ```
4. **Güncelleme sonrası duman testi** (kurucu hesabıyla, test grubunda):
   - `journalctl -u dipbot -n 50 --utc`: "Bot çalışıyor"; hata/uyarı yağmuru yok.
   - `/yardim`: yalnız `/oyuncuekle /oyuncusil /partiekle /partisil` görünür; `/oyuncular` ve `/partiler` mevcut kayıtları listeler (veri korunmuş).
   - Eski takma ad: `/oyuncu_ekle 72400` çalışır (yetki/onay kuralları aynı).
   - `/oyuncuekle https://diplomacia.com.tr/profile/player/192` kabul edilir; `/oyuncuekle https://diplomacia.com.tr/party/1244` açık hatayla reddedilir.
   - Küçük bir test partisiyle `/partiekle <ID>`: tek başlangıç özeti gelir, grupta üye başına bildirim **gelmez**; sonraki taramada yalnız gerçek değişiklikler bildirilir.
   - `/savaslar`: sade liste (`Saldıran – Savunan · Durum · #numara`), düğmeden ayrıntı kapsamlı.
   - Grup onayları (`/durum`), takip listeleri ve tarama zamanları önceki hâliyle uyumlu.
   - Büyük parti (ör. 1319 veya 5170): `/partiekle` artık "100 != 329" hatası vermez; özet "Toplam üye / API'den incelenen / 1.500.000+ XP ile takibe alınan / XP sınırı altında / İlk bilgisi alınan / İlk taramayı bekleyen" satırlarını ve kısmi okumada "EKSİK okundu / N üye okunamadı" uyarısını gösterir; `journalctl`'da "üye listesi KISMİ okundu (okunan=100 toplam=329 …); gövde anahtarları=…; yanıt başlık adları=…; sayfalama ipucu başlıklar=…" tanılama satırı vardır (sayfalama kanıtı için bu satırı ve DevTools Network kaydını paylaşın; token/çerez OLMADAN).
   - `sqlite3 -readonly /var/lib/dipbot/dipbot.db 'PRAGMA user_version;'` → `6`.

## Geri dönüş
Sorun varsa (betik de aynı komutları yazar):
```bash
sudo git -C /opt/dipbot checkout --detach <ONCEKI_REV>     # guncelleme-*.txt içindeki onceki_surum
sudo systemctl restart dipbot
```
Şema değişmediği için veritabanını geri yüklemek **normalde gerekmez**. Yalnız veri bozulmuşsa: `sudo /usr/local/sbin/dipbot-restore /var/backups/dipbot/dipbot-<damga>.db` (yedek tarihinden sonraki veriler kaybolur; restore önce mevcut DB'nin güvenlik yedeğini alır).

## Bilinmeyenler (canlıda doğrulanmadı)
- `guncelle.sh`nin gerçek sunucuda (systemd, gerçek `dipbot` kullanıcısı, gerçek `pip`) davranışı.
- `xp` alanının oyuncunun **toplam XP**'si olduğu ve 1.500.000 eşiğinin bu alana uygulandığı (örnek yanıtlarda 2.214.953 ve 1.765.874; gerçek dağılım görülmedi); XP'nin zaman içinde azalıp azalmadığı.
- Büyük partide API'nin tek istekte **yalnız** 100 üye döndürdüğü, **gerçek sayfalama yöntemi** (bilinmiyor; bot parametre uydurmaz) ve üyelerin **XP'ye göre azalan** sırada olduğu ve `member_count`'un toplam üye sayısı olduğu (VPS günlükleri "100 != 329" ve "100 != 129" gösterdi; sıralama ölçütü yalnız kullanıcı beyanı + 2 üyelik örnek yanıt). Bot her yanıtta `xp` alanlarını denetler ve sonucu günlüğe/özete yazar ama sıralamayı VARSAYMAZ.
- Üye ilk verisi alımının gerçek Diplomacia hız sınırlarıyla davranışı (büyük partilerde üst sınır ve süre bütçesi sabitleri `config.py`: `PARTI_EKLE_ESZAMANLI`, `PARTI_EKLE_ILK_VERI_UST`, `PARTI_EKLE_ILK_VERI_SURE_SN`; canlı gözleme göre ayarlanabilir).
- Telegram'da yeni komut listesinin (`set_my_commands`) istemcilerde görünmesi ve eski komutların kısa süre önbellekte kalması.
