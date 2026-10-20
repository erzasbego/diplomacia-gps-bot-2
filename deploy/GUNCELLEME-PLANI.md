# Canlı VPS güvenli güncelleme planı (kısa komutlar, parti ilk verisi ve büyük parti ilk-100 sürümleri)

**Durum:** PLAN. Bu belge hazırlanırken canlı VPS'e dokunulmadı: hiçbir dosya değiştirilmedi, `dipbot` servisi durdurulmadı veya yeniden başlatılmadı. Aşağıdaki adımları siz sunucuda çalıştırırsınız. `guncelle.sh` bulutta yalnız sahte dizin, sahte `systemctl`/`pip` ve yerel git depolarıyla test edildi; **gerçek sunucuda çalıştırılmadı.**

## Bu sürümde ne değişiyor?
- Komutlar: `/oyuncuekle`, `/oyuncusil`, `/partiekle`, `/partisil` (eski alt çizgili adlar çalışmaya devam eder).
- `diplomacia.com.tr` bağlantıları: `/player/ID`, `/party/ID` ve `profile/` / `press/` bağlamlı biçimleri.
- Sade savaş listesi, tutarlı emojiler.
- `/partiekle`: üyelerin ilk bölge/vatandaşlık verisi sınırlı eşzamanlılıkla alınır; tek başlangıç özeti; ilk bilgi bildirim değildir.
- **Büyük partiler (API ilk 100 üyeyi döndürür; örn. 100/329, 100/129):** artık eklenebilir; yalnız ilk 100 oyuncu takip edilir, sıralama değişimi parti üyeliği değişikliği sayılmaz (ayrıntı: README "Büyük partiler").

## Veritabanı şeması DEĞİŞİYOR: v4 → v5 (yalnız EKLEME, mevcut kayıtlar korunur)
`takip_partiler` tablosuna üç sütun eklenir: `sinirli INTEGER NOT NULL DEFAULT 0`, `toplam_uye INTEGER`, `xp_sirali INTEGER` (`PRAGMA user_version` 4 → 5). Geçiş servis **ilk açılışında** otomatik yapılır (`ALTER TABLE ... ADD COLUMN`, idempotent); satır silinmez/yeniden yazılmaz. Bu yüzden:
- Mevcut **grup onayları, yetkili kullanıcılar, takip edilen oyuncular/partiler, engeller, savaş kayıtları, bekleyen bildirimler aynen korunur**. Eski partiler `sinirli=0, toplam_uye=NULL` ile "tam liste" olarak devam eder (bir sonraki taramada gerçek değerler yazılır).
- **Yedek bu yüzden daha da önemlidir:** `guncelle.sh` yedeği ZORUNLU kılar; yedek `user_version=4` durumundadır.
- **Geri dönüş:** v4 kodu v5 veritabanını sorunsuz açar (ek sütunların varsayılanı vardır; eski `INSERT`ler çalışır — testli), yani çoğu durumda yalnız kodu eski commit'e almak yeterlidir. v4 kodu büyük partilerde (ilk 100) yine "üye listesi tamamlanmadı" hatası verir; bu veri bozulması değildir. Beklenmedik durumda yedekten geri yükleyin.
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
   - Büyük parti (ör. 1319 veya 5170): `/partiekle` artık "100 != 329" hatası vermez; özet "Toplam üye: 329 / Takip edilen oyuncu: 100" gösterir; `journalctl`'da "API ilk 100 üyeyi döndürdü … XP sırası doğrulandı/doğrulanamadı" satırı vardır.
   - `sqlite3 -readonly /var/lib/dipbot/dipbot.db 'PRAGMA user_version;'` → `5`.

## Geri dönüş
Sorun varsa (betik de aynı komutları yazar):
```bash
sudo git -C /opt/dipbot checkout --detach <ONCEKI_REV>     # guncelleme-*.txt içindeki onceki_surum
sudo systemctl restart dipbot
```
Şema değişmediği için veritabanını geri yüklemek **normalde gerekmez**. Yalnız veri bozulmuşsa: `sudo /usr/local/sbin/dipbot-restore /var/backups/dipbot/dipbot-<damga>.db` (yedek tarihinden sonraki veriler kaybolur; restore önce mevcut DB'nin güvenlik yedeğini alır).

## Bilinmeyenler (canlıda doğrulanmadı)
- `guncelle.sh`nin gerçek sunucuda (systemd, gerçek `dipbot` kullanıcısı, gerçek `pip`) davranışı.
- Büyük partide API'nin gerçekten **yalnız** ilk 100 üyeyi döndürdüğü, bunların **XP'ye göre azalan** sırada olduğu ve `member_count`'un toplam üye sayısı olduğu (VPS günlükleri "100 != 329" ve "100 != 129" gösterdi; sıralama ölçütü yalnız kullanıcı beyanı + 2 üyelik örnek yanıt). Bot her yanıtta `xp` alanlarını denetler ve sonucu günlüğe/özete yazar ama sıralamayı VARSAYMAZ.
- Üye ilk verisi alımının gerçek Diplomacia hız sınırlarıyla davranışı (büyük partilerde üst sınır ve süre bütçesi sabitleri `config.py`: `PARTI_EKLE_ESZAMANLI`, `PARTI_EKLE_ILK_VERI_UST`, `PARTI_EKLE_ILK_VERI_SURE_SN`; canlı gözleme göre ayarlanabilir).
- Telegram'da yeni komut listesinin (`set_my_commands`) istemcilerde görünmesi ve eski komutların kısa süre önbellekte kalması.
