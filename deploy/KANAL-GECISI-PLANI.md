# Kanal geçişi planı: bildirimler yalnız log kanalına (`-1003998002227`)

**Durum:** PLAN. Bu belge hazırlanırken canlı VPS'e, Telegram'a veya Diplomacia'ya dokunulmadı; hiçbir şey otomatik dağıtılmadı. Adımları siz sunucuda uygularsınız. Ayrı PM2 servisi `takip-bot`a dokunulmaz.

## 1. Ne değişiyor?
- Tüm otomatik takip bildirimleri (oyuncu / parti / savaş) **yalnız** log kanalına gider. Eski gruplara bildirim gitmez.
- Gruplarda **hiçbir komut/düğme işlenmez** (sessizce yok sayılır; yanıt, denetim kaydı, bildirim yok). Yönetim yalnız **kurucunun özel sohbetindedir**.
- Oyuncu/parti/savaş **takip özellikleri ve algoritmaları aynıdır**; veritabanı şeması **değişmez**; hiçbir kayıt silinmez.
- Üç eski grubun takip listeleri **otomatik birleştirilmez**. Hangi listenin kullanılacağını kurucu `TRACKING_GROUP_IDS` ile seçer; **varsayılan boş: hiçbir liste taranmaz**. Seçilmeyen listeler olduğu gibi saklanır, taranmaz ve bildirim üretmez.
- Kanal Telegram API'siyle doğrulanır (var mı, tür `channel` mı, bot yönetici ve "Post messages" yetkili mi); hazır değilse tarama duraklar, kanal satırları bekletilir ve kurucuya özel sohbette bildirim gider.
- Kuyrukta bekleyen **eski grup hedefli** bildirimler açılışta **iptal** edilir (silinmez) — ne gruba ne kanala gider.
- Kurucuya özel sohbette giden operasyonel bildirimler (denetim, yetkisiz özel sohbet denemesi, token uyarıları, kanal durumu) değişmedi.

## 2. Önkoşullar
1. Botu log kanalına **yönetici** olarak ekleyin ve "Post messages" yetkisini verin (Telegram kanal yönetici ayarları). Kanal kimliği `-1003998002227` olmalı.
2. PR birleşmiş/inceleme tamam; CI yeşil.
3. Güvenli yedek yolu için `deploy/GUNCELLEME-PLANI.md` (§3–§4) geçerlidir: yedek her zaman `dipbot` kullanıcısıyla alınır.

## 3. Adımlar (sırayla; hiçbirinde servisi önce yeniden başlatmayın)
Değişkenler: `APP=/opt/dipbot`, `DEPLOY_USER="$(stat -c %U $APP/.git)"`.

```bash
# 0) Durum + geri dönüş noktası
sudo systemctl status dipbot --no-pager
ONCEKI_REV="$(sudo -u "$DEPLOY_USER" git -C $APP rev-parse HEAD)"; echo "ONCEKI_REV=$ONCEKI_REV"

# 1) Yedek (zorunlu; başarısızsa DURUN)
sudo /usr/local/sbin/dipbot-backup

# 2) Kodu getir ve yalnız ileri sar — servisi YENİDEN BAŞLATMAYIN (çalışan süreç eski kodla sürer)
sudo -u "$DEPLOY_USER" git -C $APP fetch origin <dal-veya-main>
sudo -u "$DEPLOY_USER" git -C $APP merge --ff-only FETCH_HEAD

# 3) Mevcut veriyi RAPORLA (salt okunur; hiçbir şeyi değiştirmez/birleştirmez/silmez)
sudo -u dipbot env PYTHONPATH=$APP/src $APP/.venv/bin/python -m dipbot.rapor /var/lib/dipbot/dipbot.db
#    ortak kimlik listeleri için: ... -m dipbot.rapor /var/lib/dipbot/dipbot.db --ayrinti
```
Rapor şunları gösterir: her takip listesi için oyuncu/parti/parti üyesi/savaş sayıları (elle eklenen oyuncu sayısı dahil), tarama aralığı, son başarılı tarama, bekleyen bildirim; listeler arası **ortak** ve **özgün** kimlik sayıları (ad yok); bekleyen kuyruğun hedeflere dağılımı (hangileri iptal edilecek). Çıktıda token/sır yoktur; oyuncu adı yoktur, paylaşabilirsiniz.

```bash
# 4) KARAR (sizin): hangi liste kullanılacak? Listeler OTOMATİK birleştirilmez.
#    Seçilen liste kimliğini env dosyasına yazın (tek liste önerilir; birden çok liste aynı oyuncu için çift bildirim üretebilir):
sudo nano /etc/dipbot/dipbot.env      # TRACKING_GROUP_IDS=-100xxxxxxxxxx   (isteğe bağlı: LOG_CHANNEL_ID=-1003998002227)
sudo ls -l /etc/dipbot/dipbot.env     # beklenen: -rw------- root root

# 5) Ön kontrol (TRACKING_GROUP_IDS / LOG_CHANNEL_ID biçimini de denetler)
sudo $APP/deploy/onkontrol.sh

# 6) Servis
sudo systemctl restart dipbot
sleep 5; sudo systemctl is-active dipbot
sudo journalctl -u dipbot -n 80 --no-pager --utc
```
Günlükte beklenenler: `bildirim kanalı doğrulandı (id=-1003998002227)`; varsa `kanal geçişi: eski grup hedefli N bekleyen bildirim iptal edildi`; `TRACKING_GROUP_IDS tanımlı değil` uyarısı **olmamalı** (seçtiyseniz). `bildirim kanalı HAZIR DEĞİL` görürseniz botun kanalda yönetici/"Post messages" yetkisini kontrol edin; hazır olana dek tarama yapılmaz.

## 4. Doğrulama (gerçek kayıt eklemeden/silmeden)
Kurucu hesabıyla **botun özel sohbetinde**:
- `/channel` → "✅ The channel exists and the bot can post messages there." (mesaj göndermez; kanala test mesajı atılmaz)
- `/lists` → seçtiğiniz liste "✅ ACTIVE (scanned)", diğerleri "not active"; sayılar rapordakiyle uyumlu.
- `/status` → `Notification channel: … — ready`, `Tracking list status: ACTIVE (scanned)` (listeyi Select Group ile seçince).
- Eski gruplarda `/status`, `/players` vb. yazın → **hiç yanıt gelmez**; eski gruplara hiçbir bildirim düşmez.
- Bir tarama aralığı (varsayılan 10 dk) sonra: gerçek bir değişiklik olduğunda bildirim **yalnız kanalda** görünür (İngilizce, İstanbul saati). Yapay değişiklik üretmeyin.
- Veri: önceki rapor sayıları, **tüm listelerde** aynı olmalı (seçilmeyenler dahil; azalma yok). `PRAGMA user_version` değişmez.

## 5. Dikkat edilecekler
- **Seçilmeyen listeler taranmaz:** o listelerdeki oyuncu/parti/savaşlar için bildirim gelmez. Hepsini kapsamak istiyorsanız bu **ayrı bir karardır** (listeleri birleştirme veya birden çok liste seçme); bu sürüm bunu otomatik yapmaz.
- Daha önce taranmayan bir liste sonradan seçilirse, aradaki değişiklikler ilk taramada toplu bildirilebilir.
- `TRACKING_GROUP_IDS` içindeki liste `onayli_gruplar`'da (onaylı) değilse taranmaz; onayı kaldırılmış liste için bekleyen bildirimler durur (mevcut davranış).
- Bu belgedeki komutlar `dipbot` kullanıcısıyla çalışır; canlı SQLite'a root ile dokunmayın (bkz. `GUNCELLEME-PLANI.md`).

## 6. Geri dönüş
Yalnız kod ve env: 
```bash
sudo -u "$DEPLOY_USER" git -C $APP checkout --detach <ONCEKI_REV>
sudo nano /etc/dipbot/dipbot.env       # TRACKING_GROUP_IDS / LOG_CHANNEL_ID satırlarını kaldırın veya yorum yapın
sudo systemctl restart dipbot
```
Eski kod bildirimleri yeniden **eski gruplara** gönderir. İptal edilen eski grup bildirimleri geri gelmez (satırlar `iptal` olarak kalır). Veritabanı şeması değişmediği için DB geri yüklemesi gerekmez.
