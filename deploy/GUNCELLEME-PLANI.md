# Canlı VPS güvenli güncelleme planı (kısa komutlar + parti ilk verisi sürümü)

**Durum:** PLAN. Bu belge hazırlanırken canlı VPS'e dokunulmadı: hiçbir dosya değiştirilmedi, `dipbot` servisi durdurulmadı veya yeniden başlatılmadı. Aşağıdaki adımları siz sunucuda çalıştırırsınız. `guncelle.sh` bulutta yalnız sahte dizin, sahte `systemctl`/`pip` ve yerel git depolarıyla test edildi; **gerçek sunucuda çalıştırılmadı.**

## Bu sürümde ne değişiyor?
- Komutlar: `/oyuncuekle`, `/oyuncusil`, `/partiekle`, `/partisil` (eski alt çizgili adlar çalışmaya devam eder).
- `diplomacia.com.tr` bağlantıları: `/player/ID`, `/party/ID` ve `profile/` / `press/` bağlamlı biçimleri.
- Sade savaş listesi, tutarlı emojiler.
- `/partiekle`: üyelerin ilk bölge/vatandaşlık verisi sınırlı eşzamanlılıkla alınır; tek başlangıç özeti; ilk bilgi bildirim değildir.

## Veritabanı şeması DEĞİŞMİYOR
`SEMA_SURUMU` 4'te kalır (`PRAGMA user_version=4`); yeni tablo/sütun yok. Bu yüzden:
- Mevcut **grup onayları, yetkili kullanıcılar, takip edilen oyuncular/partiler, engeller, savaş kayıtları ve bekleyen bildirimler aynen korunur**; veri dönüştürme/geçiş adımı yoktur.
- **Geri dönüş** yalnız kodu eski commit'e almaktır; veritabanı eski kodla da uyumludur (aynı şema). Yine de yedek, beklenmedik durum için alınır.
- Parti ilk verisi için yeni bir kalıcı durum eklenmedi: "ilk gözlem" bilgisi zaten SQLite'ta `takip_oyuncular.baseline_tamam` ve bölge/vatandaşlık sütunlarının `NULL` olup olmamasıyla tutulur (yeniden başlatmada kaybolmaz).
- Bu PR'dan **önceki** sürüme (örn. F2b öncesi, şema < 4) dönüş bu planın konusu değildir; o durumda yedekten geri yükleme gerekir (`README-DEPLOY.md`, "Güncelleme").

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

## Geri dönüş
Sorun varsa (betik de aynı komutları yazar):
```bash
sudo git -C /opt/dipbot checkout --detach <ONCEKI_REV>     # guncelleme-*.txt içindeki onceki_surum
sudo systemctl restart dipbot
```
Şema değişmediği için veritabanını geri yüklemek **normalde gerekmez**. Yalnız veri bozulmuşsa: `sudo /usr/local/sbin/dipbot-restore /var/backups/dipbot/dipbot-<damga>.db` (yedek tarihinden sonraki veriler kaybolur; restore önce mevcut DB'nin güvenlik yedeğini alır).

## Bilinmeyenler (canlıda doğrulanmadı)
- `guncelle.sh`nin gerçek sunucuda (systemd, gerçek `dipbot` kullanıcısı, gerçek `pip`) davranışı.
- Üye ilk verisi alımının gerçek Diplomacia hız sınırlarıyla davranışı (büyük partilerde üst sınır ve süre bütçesi sabitleri `config.py`: `PARTI_EKLE_ESZAMANLI`, `PARTI_EKLE_ILK_VERI_UST`, `PARTI_EKLE_ILK_VERI_SURE_SN`; canlı gözleme göre ayarlanabilir).
- Telegram'da yeni komut listesinin (`set_my_commands`) istemcilerde görünmesi ve eski komutların kısa süre önbellekte kalması.
