# Merkezi takip mimarisine ve log kanalına geçiş planı

**Durum:** PLAN. Bu belge hazırlanırken canlı VPS'e, Telegram'a veya Diplomacia'ya dokunulmadı; gerçek veritabanı üzerinde HİÇBİR migration çalıştırılmadı ve servis yeniden başlatılmadı. Gerçek migration ve canlıya geçiş **kurucunun açık onayıyla** yapılır. Ayrı PM2 servisi `takip-bot`a hiçbir adımda dokunulmaz.

## 1. Yeni mimari (özet)
- **Telegram log kanalı** (`LOG_CHANNEL_ID`, varsayılan `-1003998002227`): tüm otomatik takip bildirimlerinin (oyuncu, parti, savaş) **tek** hedefi. Kanalda bot komutu çalışmaz.
- **Kurucunun özel sohbeti:** tek yönetim paneli (oyuncu/parti ekle-sil, tarama ayarı, token, durum, `/channel`, `/lists`). Grup seçimi yoktur.
- **Merkezi takip alanı:** tek oyuncu listesi, tek parti listesi, tek savaş kaydı, tek tarama yapılandırması. Veri `group_id` sütununda **`0`** (`config.MERKEZ_ID`; Telegram kimliği değil) ile tutulur; **kanal değişse de takip geçmişi korunur**.
- **Eski üç grup** (`-1004485206993`, `-1004375821471`, `-5571949121`): tamamen devre dışı; yalnız **arşiv ve migration kaynağı**. Komut/buton işlenmez, bildirim/hata mesajı/otomatik cevap gönderilmez, taranmazlar, botun gruba eklenmesi veya yönetici olması hiçbir yetki ya da takip alanı oluşturmaz. Eski kayıtlar veritabanından **silinmez**.
- Kaldırılanlar: grup onayı/yetkili kullanıcı mantığı (çalışma zamanında okunmaz), `/grant /revoke /approve_group /remove_group /move_group`, grup göçü işleme, **Select Group**, **TRACKING_GROUP_IDS**.
- Kanal erişilemezse eski gruplara **dönülmez**: tarama duraklar, kanal satırları bekler (hata/deneme sayılmaz), kurucuya özel sohbette tek uyarı gider; kanal düzelince bekleyen bildirimler tam teslim edilir.
- Açılışta bekleyen outbox satırları hizalanır (silinmez): eski grup kapsamlı satırlar **iptal** (ne gruba ne kanala gider); merkezi kapsamlı satırlar güncel kanala yönlendirilir; kurucu/kanal dışı artıklar iptal. Gönderim anında da aynı koruma vardır.

## 2. Migration aracı (`python -m dipbot.migrasyon`)
- `analiz <db> [--eski-gozlem-sessiz GUN] [--kaynak ID ...] [--json]`: **salt okunur dry-run** (veritabanı `mode=ro`, tek tutarlı anlık görüntü). Gösterir: her eski grubun sayıları, benzersiz/tekilleştirilen oyuncu-parti-savaş, **kaynaklar arası çakışmalar** (alan değerleriyle), atlananlar (zaten merkezde, merkezde engelli, uid mükerrer, taşınmayan engel), **son gözlemlerin yaşı** (uzun boşluk = ilk merkezi taramada çok sayıda gerçek değişiklik bildirimi), **önerilen merkezi tarama aralığı**, savaş baseline durumu, eski bekleyen bildirim sayısı ve **PLAN DIGEST** (plan özeti). Uygulama aynı planı üretir (dry-run = uygulama).
- `uygula <db> --yedek <yedek.db> --onayliyorum --plan <özet> [--bekleyen-kaybi-kabul] [--aralik N] [--eski-gozlem-sessiz GUN] [--kaynak ID ...]`: tek transaction; yalnız merkezi alana **ekler**; eski kayıtlara dokunmaz; merkezde var olanı ve merkezde **engellenmiş/silinmiş** olanı ezmez/geri getirmez; idempotent (ikinci çalıştırma `zaten_uygulanmis`; `--zorla` yalnız merkez geçişten sonra değişmemişse çalışır ve yine mükerrer oluşturmaz); **her tablo** için eklenen satır sayısı plana eşit değilse **geri alınır**.
  - `--plan`: `analiz`in yazdığı plan özeti; veri veya seçenekler analizden sonra değiştiyse uygulama **reddedilir** (onaylanan plan = uygulanan plan).
  - `--yedek`: dosya var, canlı veritabanının **kendisi değil**, `integrity_check=ok` ve kaynak tabloların **içerik özeti** canlıyla birebir aynı olmalı (servis durdurulmuşken alınmış TAZE yedek; bayat/yabancı yedek reddedilir). Şema sürümü beklenenden farklıysa `db.baglan` çağrılmadan reddedilir.
  - `--bekleyen-kaybi-kabul`: eski gruplarda teslim edilmemiş bildirim varsa **zorunlu**; bu bildirimler açılışta iptal edilir ve hiçbir yere gönderilmez (o değişiklikler merkezde yeniden duyurulmaz).
  - `--eski-gozlem-sessiz GUN`: son gözlemi GUN günden eski oyuncu/parti kayıtları ilk merkezi taramada **sessiz** yeniden gözlenir (bildirim seli önlemi; o aralıktaki gerçek değişiklikleri **gizler** — kurucu kararı). Verilmezse kayıtlar aynen taşınır ve ilk taramada gerçek farklar kanala bildirilir.
- `geri-al <db> --yedek <yedek.db> --onayliyorum`: **işaret şart**; yalnız bu migrasyonun **eklediği** merkezi kayıtları (işarette saklanan anahtarlar) ve işareti kaldırır. Merkezde önceden var olan veya geçişten sonra eklenen kayıtlara, eski grup kayıtlarına ve bekleyen bildirimlere dokunmaz. Tercih sırası: kod geri alma → yedekten geri yükleme → `geri-al`.

**Birleştirme kuralları:** aynı oyuncu/parti birden çok grupta varsa tek kayıt: ilk gözlemi tamam olan + **en güncel gözlemli/doğrulanmış** kayıt seçilir (eşitlikte kaynak sırası); `elle_eklendi` ve `xp_onayli` en yüksek değer; `eklendi` en erken; eksik (NULL) alanlar başka kayıttan **doldurulmaz** (bayat değer sahte değişiklik bildirimi üretmesin); parti üyeleri seçilen parti kaydından; savaşlar **birleşim** (tarihsel kayıt kaybolmaz, en erken ilk görülme); engeller birleşim (merkezde takipli kimliğin engeli taşınmaz); 404 sayaçları en büyüğü; savaş baseline'ı herhangi bir kaynakta tamamsa tamam (bilinen savaşlar için bildirim seli olmaz). Aynı `uid` farklı numarayla tekrar ederse tek kayıt aktarılır (raporlanır).

## 3. Adımlar
Değişkenler: `APP=/opt/dipbot`, `DB=/var/lib/dipbot/dipbot.db`, `DEPLOY_USER="$(stat -c %U $APP/.git)"`. Tüm SQLite işlemleri `dipbot` kullanıcısıyla yapılır (root sahipli `-wal/-shm` oluşmasın; bkz. `GUNCELLEME-PLANI.md`).

```bash
# 0) Önkoşul: botu log kanalına yönetici yapın ("Post messages" yetkisi). Durum + geri dönüş noktası:
sudo systemctl status dipbot --no-pager
ONCEKI_REV="$(sudo -u "$DEPLOY_USER" git -C $APP rev-parse HEAD)"; echo "ONCEKI_REV=$ONCEKI_REV"

# 1) İlk yedek (zorunlu)
sudo /usr/local/sbin/dipbot-backup

# 2) Kodu getir, yalnız ileri sar — servisi YENİDEN BAŞLATMAYIN (çalışan süreç eski kodla sürer)
sudo -u "$DEPLOY_USER" git -C $APP fetch origin <dal-veya-main>
sudo -u "$DEPLOY_USER" git -C $APP merge --ff-only FETCH_HEAD

# 3) DRY-RUN analiz (salt okunur; hiçbir şeyi değiştirmez). Çıktıyı inceleyin ve çakışmalara/aralık önerisine karar verin.
sudo -u dipbot env PYTHONPATH=$APP/src $APP/.venv/bin/python -m dipbot.migrasyon analiz $DB [--eski-gozlem-sessiz GUN] | tee /tmp/migrasyon-analiz.txt
```
**KARAR NOKTASI (sizin onayınız):** analiz çıktısını (özellikle *CONFLICTS*, *SKIPPED*, *AGE OF LAST OBSERVATIONS*, *RECOMMENDED CENTRAL SETTINGS*, *OLD PENDING NOTIFICATIONS* ve **PLAN DIGEST**) inceleyin; `--eski-gozlem-sessiz` gerekip gerekmediğine karar verin. Onaylamadan 4. adıma geçmeyin.

```bash
# 4) Servisi DURDURUN, TAZE yedek alın (yedek, canlı veriyle aynı satır sayısında olmalı), migrasyonu uygulayın
sudo systemctl stop dipbot
sudo /usr/local/sbin/dipbot-backup           # çıktıdaki dosyayı not alın: YEDEK=/var/backups/dipbot/dipbot-<damga>.db
sudo -u dipbot env PYTHONPATH=$APP/src $APP/.venv/bin/python -m dipbot.migrasyon uygula $DB --yedek "$YEDEK" --onayliyorum --plan <PLAN-DIGEST> [--bekleyen-kaybi-kabul] [--aralik 5] [--eski-gozlem-sessiz GUN]
#    (analizde seçtiğiniz --kaynak/--eski-gozlem-sessiz seçeneklerini AYNEN tekrarlayın; farklıysa plan özeti uyuşmaz ve uygulama reddedilir)

# 5) Ön kontrol + başlat
sudo $APP/deploy/onkontrol.sh
sudo systemctl start dipbot
sleep 5; sudo systemctl is-active dipbot; sudo journalctl -u dipbot -n 80 --no-pager --utc
```
Günlükte beklenenler: `bildirim kanalı doğrulandı (id=-1003998002227)`; varsa `bekleyen bildirimler hizalandı: eski grup kapsamlı N iptal …`. `bildirim kanalı HAZIR DEĞİL` görürseniz botun kanaldaki yönetici/"Post messages" yetkisini kontrol edin (tarama bu sürece kadar duraklar).

## 4. Doğrulama (gerçek kayıt eklemeden/silmeden)
Kurucu hesabıyla botun **özel sohbetinde**: `/channel` (✅ kanal var, bot gönderebiliyor), `/lists` (merkezi liste sayıları = analizdeki "to add" + önceki merkezi kayıtlar; "Migration: applied"), `/status` (kanal "ready", Group satırı yok), `/players`, `/parties`, `/wars`. Eski gruplarda herhangi bir komut yazın → **yanıt yok**. Bir tarama aralığı sonra gerçek bir değişiklik olduğunda bildirim **yalnız kanalda** görünür; eski gruplara hiçbir şey düşmez. Analizdeki eski grup sayıları `sqlite3` ile aynen durur (kaynak kayıtlar silinmemiş/değişmemiş).

## 5. Bilinen sınırlar / dikkat
- **Geçişte iptal edilen eski bekleyen bildirimler** (analizde sayısı yazar) hiçbir yere gönderilmez: bunlar eski gruplara üretilmiş ama henüz gönderilmemiş değişikliklerdir (kayıttaki gözlem zaten güncel olduğundan kanalda yeniden duyurulmazlar). İstenirse tutarsızlıklar analizdeki çakışma/bekleyen sayılarından elle incelenebilir.
- Tarama aralığı kaynaklarda farklıysa merkezi aralık için **en kısa** önerilir; `--aralik` ile değiştirilebilir (5/10/15/20).
- Aynı oyuncunun gözlemleri gruplarda farklıysa en güncel gözlem esas alınır; ilk merkezi taramada kayıtla gerçek durum farklıysa o oyuncu için **tek** bildirim kanala gider.
- Gerçek veritabanı bu geliştirmede görülmedi: dry-run çıktısı sunucuda üretilmelidir.
- Gönderim: çok parçalı (≈3500+ karakter) bir bildirimin ikinci parçası başarısız olursa yeniden denemede ilk parça tekrar gidebilir (en-az-bir-kez teslim; mevcut belgelenmiş sınır).
- Kanal erişimi/yetkisi gönderim sırasında kaybolursa satır başarısız olmaz: kanal anında "hazır değil" işaretlenir, tarama duraklar, kurucuya bildirilir, kanal düzelince bekleyenler gider.

## 6. Geri dönüş
1. **Yalnız kod (önerilen, veri kaybı yok):** servis durdurulur, `sudo -u "$DEPLOY_USER" git -C $APP checkout --detach $ONCEKI_REV`, servis başlatılır. Eski kod yalnız onaylı eski grupları tarar ve `group_id=0` kayıtlarını yok sayar; eski grup kayıtları migrasyonla değişmediği için eski davranış geri gelir (bildirimler yeniden eski gruplara gider).
2. **Merkezi kayıtları kaldırma:** `... -m dipbot.migrasyon geri-al $DB --yedek <taze yedek> --onayliyorum` (yalnız merkezi alan kayıtları + migrasyon işareti; geçişten sonra merkezde yapılan ekleme/silmeler de gider).
3. **Tam geri yükleme (yalnız veri bozulduysa):** `sudo systemctl stop dipbot; sudo /usr/local/sbin/dipbot-restore <migrasyon öncesi yedek>`; servisi başlatın.
