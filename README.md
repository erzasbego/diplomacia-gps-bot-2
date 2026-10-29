# Diplomacia GPS Botu

[Diplomacia](https://diplomacia.com.tr) oyunundaki **oyuncuları, partileri ve savaşları** izleyen, Telegram'dan yönetilen, tek VPS'te sürekli çalışacak bir bot. Değişiklikleri (isim, bölge, vatandaşlık ülkesi, parti girişi/çıkışı, yeni savaş) ilgili onaylı Telegram grubuna **İngilizce** bildirir.

Python 3.11+ · `python-telegram-bot` 22.8 (long polling; alan adı/HTTPS gerekmez) · SQLite · Telegram arayüzü ve mesajlar **İngilizce** (belgeler Türkçe) · gösterilen zamanlar `Europe/Istanbul` 24 saat, kayıtlar UTC.

> ## ⚠️ Doğrulama durumu — önce bunu okuyun
> Bu bot **henüz gerçek Diplomacia'ya veya gerçek Telegram'a bağlanılarak denenmedi.** Şu an elde olan: **1301 otomatik test** (sahte veri kaynağı, `httpx.MockTransport`, sahte Telegram nesneleri; Python 3.11/3.12/3.13'te geçti). Bunlar iş kurallarını kanıtlar; **canlı entegrasyonu kanıtlamaz.** Ayrıntı için [Doğrulananlar ve doğrulanmayanlar](#doğrulananlar-ve-doğrulanmayanlar).

## İçindekiler
1. [Nasıl çalışır](#nasıl-çalışır)
2. [Kurulum (geliştirme)](#kurulum-geliştirme)
3. [Yapılandırma](#yapılandırma)
4. [Yetki modeli](#yetki-modeli)
5. [Komutlar ve butonlar](#komutlar-ve-butonlar)
6. [Takip kuralları](#takip-kuralları)
7. [Diplomacia token'ı ve yenileme](#diplomacia-tokenı-ve-yenileme)
8. [Veri klasörü, yedek ve geri yükleme](#veri-klasörü-yedek-ve-geri-yükleme)
9. [VPS (Ubuntu) kurulumu](#vps-ubuntu-kurulumu)
10. [Bilinen sınırlar](#bilinen-sınırlar)
11. [Doğrulananlar ve doğrulanmayanlar](#doğrulananlar-ve-doğrulanmayanlar)
12. [Proje yapısı ve testler](#proje-yapısı-ve-testler)

## Nasıl çalışır
- Bot, kurucunun verdiği **Diplomacia oturum token'ı** ile sitenin kendi JSON API'sini (`/api/players/…`, `/api/parties/…`, `/api/wars`) okur. Başlıklar: `Authorization: Bearer <token>` ve `X-Client-Platform: web` (biçim, kullanıcı tarafından tarayıcı DevTools'unda doğrulandı; botla canlıda denenmedi).
- Her onaylı grubun **ayrı** takip listesi, ayarı ve bildirimi vardır; gruplar birbirinin verisini görmez.
- Tarama aralığı grup başınadır: **5, 10, 15 veya 20 dakika** (varsayılan 10). Aynı iş önceki bitmeden yeniden başlamaz; siteye eşzamanlı istek sayısı sınırlıdır; hata/istek sınırında geri çekilme uygulanır.
- Değişiklik kaydı ile bildirim kuyruğu (outbox) **aynı veritabanı işleminde** yazılır; Telegram hatasında bildirim kaybolmaz, kontrollü yeniden denenir.

### "Aralıklı kontrol" sınırı (önemli)
Bot olayları **dinlemez, aralıklarla yoklar.** Bu yüzden: iki kontrol arasında olup geri alınan bir değişiklik (örn. oyuncu bölge değiştirip geri döndü) **görülmez**; bildirimdeki zaman **tespit zamanıdır, hareketin kesin zamanı değildir.** Savaşlarda yalnızca **yeni savaş** bildirilir; hasar/güç değişimi mesaj üretmez.

## Kurulum (geliştirme)
```bash
python3.11 -m venv .venv && . .venv/bin/activate     # Python 3.11+ (3.12 ve 3.13 de denendi)
pip install -r requirements.txt
python -m pytest -q                                  # beklenen: 1301 passed (MOCK)
```
Paket `src/` altındadır ve pip ile **kurulmaz**; çalıştırırken `PYTHONPATH=src` verilir (testler için `pytest.ini` bunu zaten yapar):
```bash
export BOT_TOKEN=... OWNER_TELEGRAM_ID=...           # kendi değerleriniz; depoya YAZMAYIN
PYTHONPATH=src python -m dipbot
```
Uygulama `.env` dosyasını **okumaz**; yalnız süreç ortamını okur (systemd `EnvironmentFile` veya kabuk). `BOT_TOKEN`/`OWNER_TELEGRAM_ID` yoksa çıkış kodu 2 ile kapanır.

## Yapılandırma
Örnek: [`.env.example`](.env.example). Gerçek değerleri depoya eklemeyin.

| Değişken | Zorunlu | Anlamı |
|---|---|---|
| `BOT_TOKEN` | evet | Telegram botunun token'ı (BotFather). **Diplomacia token'ı değildir.** |
| `OWNER_TELEGRAM_ID` | evet | Kurucunun sayısal Telegram kullanıcı kimliği (pozitif tamsayı). |
| `DB_PATH` | hayır | SQLite dosyası. Boşsa `<proje>/data/dipbot.db`. |
| `DIPLOMACIA_TOKEN_DOSYASI` | hayır | Diplomacia token'ının tutulduğu dosya (0600). Boşsa `DB_PATH` dizinindeki `diplomacia.token`. |
| `DIPLOMACIA_AUTH_BASLIK`, `DIPLOMACIA_AUTH_SEMA` | hayır | Kimlik başlığı override'ı (varsayılan `Authorization` / `Bearer`). Genelde boş bırakılır. |
| `DIPLOMACIA_DOGRULAMA_OYUNCU` | hayır | Yeni token doğrulanırken sorgulanan herkese açık oyuncu numarası (varsayılan `72400`). |

Kod sabitleri (`src/dipbot/config.py`): tarama aralıkları `[5,10,15,20]`, denetim bildirimi sınırları, outbox/istek sınırı süreleri, `TOKEN_PREV_OMRU_SANIYE` (24 sa), `SAAT_TUTARSIZLIK_TOLERANSI_SANIYE` (30 sn).

Çıkış kodları: `0` normal, `2` yapılandırma hatası, `3` Telegram `BOT_TOKEN` reddedildi veya `Conflict` (aynı token'la ikinci örnek), `130` Ctrl-C (yalnız geliştirmede).

## Merkezi takip, bildirim kanalı ve yetki modeli

> **Mimari:** takip artık Telegram gruplarından **bağımsızdır**. Tek merkezi takip alanı (oyuncu listesi, parti listesi, savaş kaydı, tarama yapılandırması; `group_id` sütununda `config.MERKEZ_ID = 0`) vardır. Tüm otomatik takip bildirimleri **yalnız** log kanalına gider (`config.LOG_KANAL_ID` = `-1003998002227`; `LOG_CHANNEL_ID` ile değiştirilebilir; kanal değişse de takip geçmişi korunur). Yönetim **yalnız kurucunun özel sohbetindedir.** Eski gruplar (`-1004485206993`, `-1004375821471`, `-5571949121`) tamamen devre dışıdır ve yalnız arşiv/migration kaynağıdır. Ayrıntı ve geçiş adımları: [`deploy/KANAL-GECISI-PLANI.md`](deploy/KANAL-GECISI-PLANI.md).

- **Gruplar:** hiçbir komut/düğme işlenmez, hiçbir bildirim/hata/otomatik cevap gönderilmez, taranmaz; botun gruba eklenmesi veya orada yönetici olması yetki ya da takip alanı oluşturmaz (kurucuya yalnız özel sohbette bilgi gider). Grup onayı, yetkili kullanıcı listesi, grup seçimi (`Select Group`), `TRACKING_GROUP_IDS` ve grup göçü işleme **kaldırıldı** (eski kayıtlar veritabanında arşiv olarak durur).
- **Kanal doğrulaması:** açılışta (ve `/channel` ile) Telegram API'siyle kanalın var olduğu, türünün `channel` olduğu ve botun orada yönetici + "Post messages" yetkisine sahip olduğu **mesaj göndermeden** doğrulanır. Hazır değilse **tarama duraklar** (değişiklik bildirimsiz yutulmasın), kanal satırları bekletilir (hata/deneme sayılmaz), eski gruplara dönülmez ve kurucuya özel sohbette tek bildirim gider; düzelince bekleyen bildirimler tam teslim edilir.
- **Bekleyen bildirim kuyruğu:** açılışta eski grup kapsamlı satırlar **iptal** edilir (silinmez; ne gruba ne kanala gider); merkezi kapsamlı satırlar güncel kanala yönlendirilir; gönderici yalnız kurucu/kanal hedeflerini kabul eder.
- **Migration:** eski grup kayıtlarını merkezi alana taşımak için `python -m dipbot.migrasyon` (`analiz` = salt okunur dry-run, `uygula`/`geri-al` = açık onay + doğrulanmış güncel yedek gerekir). Aynı oyuncu/parti tekilleştirilir, çakışmalar raporlanır, eski kayıtlar silinmez, ikinci çalıştırma veriyi çoğaltmaz. **Gerçek migration kurucunun onayıyla ayrıca yapılır.**
- `/lists` merkezi takip listesinin özetini gösterir. Kurucuya özel sohbette giden **operasyonel** bildirimler (denetim, yetkisiz özel sohbet denemesi, token uyarıları, kanal durumu) değişmedi.

| Kavram | Ne demek |
|---|---|
| **Kurucu** | `OWNER_TELEGRAM_ID` ile tanımlanan tek yönetici. Botu yalnız **özel sohbetinde** kullanır: oyuncu/parti ekle-sil, tarama ayarı, token yenileme, durum, `/channel`, `/lists`. Kurucu dışındaki herkes reddedilir ve kurucuya (hız sınırlı) bildirilir. |

- Kurucuya **denetim bildirimi** altyapısı (kimlik+zaman, token yok) korunur; grup/yetki komutları kaldırıldığı için artık yeni denetim olayı üretmez.

## Arayüz dili ve saat (İngilizce arayüz, Europe/Istanbul)
Telegram arayüzünün **tamamı İngilizcedir** (tek dil; dil seçeneği yoktur): komut yanıtları, menüler, düğmeler, açılır uyarılar, hata/yetki mesajları, bildirimler. Oyuncu/parti adları ve oyun adları çevrilmez. Telegram komut menüsü İngilizcedir:

| İngilizce komut (menüde/yardımda) | Eski Türkçe adlar (hâlâ çalışır, İngilizce yanıt verir) |
|---|---|
| `/start`, `/help` | `/yardim` |
| `/addplayer`, `/players`, `/removeplayer` | `/oyuncuekle`, `/oyuncular`, `/oyuncusil`, `/oyuncu_ekle`, `/oyuncu_sil` |
| `/addparty`, `/parties`, `/removeparty` | `/partiekle`, `/partiler`, `/partisil`, `/parti_ekle`, `/parti_sil` |
| `/wars`, `/scan`, `/status` | `/savaslar`, `/tarama`, `/durum` |
| `/channel`, `/lists` | `/kanal`, `/listeler` |

Eski ve yeni adlar **aynı işleyiciye, aynı yetki ve onay kurallarına** gider; callback verileri (düğme kimlikleri) değişmedi, bu yüzden eski mesajlardaki düğmeler çalışır.

**Saat:** kullanıcıya gösterilen tüm tarih/saatler **Europe/Istanbul** (`zoneinfo`), 24 saat, `DD.MM.YYYY HH:mm` (ör. `22.10.2026 21:56`; AM/PM yoktur). Bu **yalnız gösterim** dönüşümüdür: veritabanı, zamanlayıcı ve API zaman damgaları UTC ve sabit biçimli ISO olarak kalır; dönüşüm yalnız metin üretilirken yapılır. Sunucunun saat dilimi değiştirilmez (gösterim ondan bağımsızdır).

## Komutlar ve butonlar
Aşağıdaki tabloda komutlar eski (Türkçe) adlarıyla yazılıdır; İngilizce menü adları yukarıdaki eşleme tablosundadır (ikisi de çalışır). **Gruplarda hiçbir komut işlenmez**; komutlar yalnız kurucunun özel sohbetindedir. Grup seçimi yoktur: komutlar doğrudan merkezi takip alanında çalışır. `/start` ana menüyü açar.

| Komut | Ne yapar |
|---|---|
| `/oyuncuekle <ID veya bağlantı>` | Oyuncuyu takibe alır (ilk ekleme değişiklik bildirimi üretmez). Örn. `/oyuncuekle 72400` veya `/oyuncuekle https://diplomacia.com.tr/player/72400` (`/profile/player/…` ve `/press/player/…` biçimleri de kabul edilir). |
| `/oyuncular`, `/oyuncusil <ID veya bağlantı>` | Listeler / takipten çıkarır (onay düğmesiyle). |
| `/partiekle <ID veya bağlantı>` | Partiyi ve üyelerinden **XP'si yeterli olanları** takibe alır. Örn. `/partiekle 1244` veya `/partiekle https://diplomacia.com.tr/party/1244` (`/profile/party/…`, `/press/party/…` da kabul). Yalnız `xp ≥ 1.500.000` üyeler takibe alınır; onların bölge/vatandaşlık ilk verisi sınırlı eşzamanlılıkla alınır (üst sınır ve süre bütçesi var), alınamayanlar ilk taramada tamamlanır. Yanıt tek bir **başlangıç özeti**dir (toplam üye, API'den incelenen, 1.500.000+ XP ile takibe alınan, XP sınırı altında, ilk bilgisi alınan, ilk taramayı bekleyen; kısmi okumada listenin EKSİK olduğu ve kaç üyenin okunamadığı); bildirim gönderilmez. Sonradan katılan üye otomatik takibe alınır. |
| `/partiler`, `/partisil <ID veya bağlantı>` | Listeler / parti takibini kaldırır (aşağıdaki "Parti kaldırma"). |
| `/savaslar` | Aktif savaşları listeler; ayrıntı ekranında `…/wars/war/{numara}` bağlantısı bulunur. |
| `/tarama <5\|10\|15\|20>` | Grubun tarama aralığı. |
| `/durum` (`/status`) | Bot durumu (İngilizce): grup, takip edilen oyuncu/parti sayısı, tarama aralığı, son başarılı tarama (oyuncular/partiler/savaşlar), sonraki planlı tarama, devam eden tarama, bekleyen bildirim. "Diplomacia connection" satırı yalnız gerçek bir sorun varsa görünür (sabit "doğrulanmadı" notları kaldırıldı). |
| `/yardim` | Komut özeti. |
| `/channel` (`/kanal`) | Log kanalını Telegram API'siyle yeniden doğrular (mesaj göndermez). |
| `/lists` (`/listeler`) | Takip listeleri ve hangisinin taranmakta olduğu. |

**Eski komut adları** (`/oyuncu_ekle`, `/oyuncu_sil`, `/parti_ekle`, `/parti_sil`) geriye dönük uyumluluk için çalışır, aynı yetki ve onay kurallarına tabidir; ancak Telegram komut listesinde, yardımda ve menülerde yalnız yeni kısa adlar gösterilir. **Bağlantı kuralı:** yalnız `diplomacia.com.tr` / `www.diplomacia.com.tr` alan adı ve `/player/ID`, `/party/ID` yolları (başına `profile/` veya `press/` gelebilir); başka alan adı, port, kullanıcı bilgisi, ek yol parçası veya yanlış tür (oyuncu komutuna parti bağlantısı) reddedilir.

**Savaş listesi** yalnız temel bilgiyi gösterir (`Saldıran – Savunan · Durum · #numara`, numara varsa tıklanabilir); ayrıntı ekranı kapsamlıdır. Emoji anlamları: ⚔️ standart savaş (tür yoksa/tanınmıyorsa da), 🔥 ayaklanma, 🏳️ bağımsızlık; ayrıntıda ⚔️ saldıran, 🛡️ savunan, ⏳ hazırlık, 🟢 aktif.

**Ana menü düğmeleri:** Oyuncular · Partiler · Savaşlar · Tarama Ayarları · Bot Durumu · Yönetim · Yardım. *Token Yenile* ana menüde değil, **Yönetim** ekranındadır (yalnız kurucu, özel sohbette). Silme/kaldırma işlemleri **onay düğmesi** ister; onay tek kullanımlık, 10 dakika geçerli ve yalnız işlemi başlatan kullanıcıya/sohbete bağlıdır.

## Takip kuralları
- **Kimlik:** takip, sabit oyuncu kimliği üzerindendir; ad, bölge ve vatandaşlık ülkesi ayrı alanlar olarak bağımsız izlenir. Aynı oyuncu farklı yollarla (bağlantı, numara, parti, baştaki sıfırlı numara) eklense bile **tek** kayıttır.
- **Eksik/hatalı cevap** (boş alan, sayfalaması tamamlanmamış parti listesi, ağ hatası) değişiklik veya üye çıkışı sayılmaz; önceki doğru veri korunur. Hiç gözlem yapılamayan tarama "başarılı" kaydedilmez.
- **Oyuncu bulunamıyor (404):** art arda 3 taramada 404 alan oyuncu durum ekranında "Bulunamıyor" işaretlenir; **otomatik silinmez.**
- **XP süzgeci (parti üyeleri) — tek kriter:** Bir partiden yalnız **`xp ≥ 1.500.000`** olan üyeler otomatik takibe alınır (`config.PARTI_XP_ESIGI`; tam 1.500.000 dahil). **Takip edilen oyuncu sayısı sınırlanmaz** (329 üyeli partide eşiği geçen hepsi, okunabildikleri sürece). **Her üyenin XP'si ayrı denetlenir** (API sıralamasına güvenilmez, erken kesme yok). XP eksik/geçersizse (alan yok, metin, negatif, kesirli, NaN, aşırı büyük) oyuncu **eklenmez** ve "bilinmiyor" sayılır (0 sayılmaz). Eşik altı oyuncular otomatik takibe alınmaz; üyelik listesinde yine kayıtlıdır. Sonradan eşiğe ulaşan oyuncu bir sonraki taramada **sessizce** (ilk bölge/vatandaşlık bildirim değildir) otomatik takibe alınır.
- **Takipten çıkarma (kademeli):** otomatik eklenmiş bir oyuncunun **geçerli** XP'si eşiğin altında **art arda 3 taramada** (`PARTI_XP_ALTI_ARDISIK_TARAMA`) gözlenirse otomatik takipten çıkar; tek geçici/hatalı yanıt silmez, eşiğin üstündeki bir gözlem sayacı sıfırlar, XP eksik/geçersiz yanıt sayacı değiştirmez. **Korunanlar:** elle eklenenler (`/oyuncuekle`) ve bu sürümden **önce** otomatik eklenmiş mevcut kayıtlar (XP süzgecinden bir kez geçene kadar XP kuralına tabi değildir). Engel konmaz. Bir oyuncu aynı anda yalnız bir partide olabileceğinden "başka partide görünüyor" korumasına dayanılmaz. Parti ayrılması, listede görünmeme veya eksik/hatalı yanıt **hiçbir silme veya bildirim üretmez** (XP kanıtı değildir).
- **Büyük partiler ve EKSİK okuma:** Diplomacia `/members` uç noktası büyük partilerde **tek istekte 100 üye** döndürür (gerçek günlükler: 100/329, 100/129). Bu bir **okuma sınırıdır, takip politikası değil**; **gerçek sayfalama yöntemi doğrulanmadı** ve kanıtlanmamış `page`/`offset`/`limit` parametresi **gönderilmez** (testli: ek istek yok). Bu durumda liste **EKSİK okunmuş** sayılır, tam liste olarak değerlendirilmez: okunan üyelerden uygun olanlar takibe alınır, okunamayan üyeler için XP bilinmez ve **başlangıç özeti bunu açıkça yazar** ("EKSİK okundu: 100/329 … 229 üye okunamadı"; okunan hepsi eşik üstüyse okunamayanlar arasında da uygun oyuncu olabileceği belirtilir). Liste kabulü sıkıdır: tam 100 kayıt + `member_count` > 100, mükerrer/sayfalama anahtarı/bozuk kayıt yok; **açıklanamayan eksiklik reddedilir** (99/329, 50/329, 101/329 …). API tüm üyeleri döndürürse (ör. 250/250) liste **tam** sayılır ve **kesilmez**. Üyelerin XP'ye göre sıralı geldiği kullanıcı beyanıdır; sıralama **varsayılmaz**, yalnız bilgi olarak denetlenip belirtilir. Kısmi okumada ayrılma/katılma çıkarılamaz → "ayrıldı/katıldı" bildirimi yok; görünmeyen kimse silinmez. Tam listede (tüm üyeler okunduğunda) gerçek katılım/ayrılma bildirilir, ancak yalnız takip ile ilgili olanlar için: katılan → XP'si uygun olan; ayrılan → o an takipte olan. Kısmi→tam geçişte listede olmayanlar doğrulanmış ayrılmadır; yeni görünenlerin katılımı doğrulanamadığından bildirilmez. Kısmi okunan partiyi kaldırırken yalnız okunan üyeler çıkarılır; okunamayanlar (elle eklenmişse) çıkarılmaz ve ekran bunu belirtir. Sonraki taramada tanılama günlüğü (gövde anahtarları, yanıt başlık adları, sayfalama ipucu başlıklar) yazılır; gerçek yöntem bu veriyle veya DevTools Network kaydıyla belirlenecektir.
- **Parti kaldırma:** önce partinin güncel üyeleri doğrulanır; doğrulanamazsa **hiçbir şey silinmez**. Doğrulanırsa, o anda partide olan herkes (elle eklenmiş olsa da) merkezi takipten çıkar; daha önce ayrılmış üyeler takipte kalır; kaldırılanlar otomatik yeniden eklenmez.
- **Savaşlar:** ilk taramada mevcut savaşlar bildirimsiz kaydedilir (bildirim yağmuru yok); tek taramada 5'ten fazla yeni savaş çıkarsa tek özet mesajı gider; tanınmayan durum/türdeki kayıt "yeni savaş" bildirimi üretmez.
- **Bildirim teslimi "en az bir kez"dir:** gönderimden hemen sonra süreç ölürse aynı mesaj bir kez daha gidebilir; çok parçalı uzun mesajda bir parça tekrarlanabilir. Telegram'da "tam bir kez" garantisi verilemez.

## Diplomacia token'ı ve yenileme
Token **veritabanında, ortam değişkeninde, logda ve Git'te tutulmaz**; yalnız `DIPLOMACIA_TOKEN_DOSYASI` dosyasında (0600) durur ve her istekte dosyadan okunur (yeniden başlatma gerekmez). Dosya yoksa/boşsa **hiç istek atılmaz** ve hata yağmuru olmaz.

**Gerçek geçerlilik süresi doğrulanmadı.** "Yaklaşık haftalık" beklentisi nedeniyle, son başarılı güncellemeden 7 gün sonra kurucuya (yalnız kurucuya) hatırlatma gider. Token daha erken geçersizleşirse (401) taramalar durur ve kurucuya **tek** mesaj gider.

**Yenileme adımları (yalnız kurucu, botla özel sohbette):**
1. Menü → **Yönetim** → **Token Yenile** → **Yeni token gönder** (bekleme bu düğmeyle başlar).
2. Yeni token'ı **tek bir mesajla** gönderin (5 dakika içinde). Bot mesajı silmeye çalışır; silemezse elle silin.
3. Bot adayı, mevcut dosyaya dokunmadan `GET /api/players/72400` ile sınar (200 **ve** oyuncu verisi olarak ayrıştırılabilen gövde şart; HTML/boş/401/403/5xx reddedilir). Başarısızsa mevcut token **değişmez**.
4. Başarılıysa eski token `…/diplomacia.token.prev` olarak yedeklenir, yenisi atomik yazılır ve taramalar kaldığı yerden devam eder.

**`.prev` politikası:** `.prev` eski token'ı içerir ve **süresiz tutulmaz.** Yeni token **ilk başarılı yetkili taramayla** teyit edilince **veya 24 saat** dolunca otomatik silinir (yalnız `unlink`). Geri dönüş yolu (yalnız `.prev` hâlâ varsa): `.prev` dosyasını token dosyasının üzerine taşıyın (`mv diplomacia.token.prev diplomacia.token`). Silindikten sonra geri dönüş yoktur. Windows'ta 0600 uygulanmaz (üretim hedefi Linux).

**Bekleyen onay ve token bekleme** süreç belleğindedir: **yeniden başlatmada geçersiz olur**, sunucu saati atlarsa (30 sn'den fazla tutarsızlık) uygulanmaz ve işlemin yeniden başlatılması istenir.

## Veri klasörü, yedek ve geri yükleme
- **Veri:** varsayılan `<proje>/data/` (sunucuda `/var/lib/dipbot`): `dipbot.db` (+ `-wal`, `-shm`) ve `diplomacia.token`. Servis yeniden başlatmalarında ve güncellemelerde korunur. Yetkili kullanıcılar, onaylı gruplar, ayarlar, takipler, son bilgiler, bilinen savaşlar, bildirim durumu ve engeller veritabanındadır; **Telegram mesaj geçmişi yerine kullanılmaz.**
- **Yedek:** `deploy/backup.sh` çalışan DB'nin düz kopyasını almaz; `sqlite3 .backup` kullanır (root ise `runuser -u dipbot` altında: canlı DB'ye root dokunmaz, root sahipli `-wal/-shm` oluşmaz), bütünlüğü doğrular (0600, zaman damgalı, son 14 tutulur) ve mevcut yedek dizininin sahip/iznini değiştirmez, yalnız doğrular. **Token dosyası, `.prev` ve `dipbot.env` yedeğe girmez**; sırları ayrıca güvenli yerde saklayın. Geri yüklemede/yeni sunucuda Diplomacia token'ı yeniden verilmelidir.
- **Geri yükleme:** `deploy/restore.sh <yedek.db>` (servisi durdurur, mevcut DB'yi `dipbot` olarak güvenlik yedeğine alır, yedeği yükler). Yedek tarihinden sonraki veriler kaybolur; **yalnız veri bozulmuşsa** gerekir. Kod sürümünü geri almak için veritabanını geri yüklemek normalde gerekmez (şema geçmişi yalnız sütun ekler; ayrıntı: `deploy/GUNCELLEME-PLANI.md` §6).
- **Güncelleme/dağıtım:** `deploy/GUNCELLEME-PLANI.md` (yedek → kod → ön kontrol → servis; git işlemleri deponun sahibiyle, SQLite işlemleri `dipbot` ile). PR #4 şemayı değiştirmez.
- Ayrıntı ve zamanlama: [`deploy/README-DEPLOY.md`](deploy/README-DEPLOY.md).

## VPS (Ubuntu) kurulumu
Hedef: **Ubuntu 24.04 LTS** (Python 3.12), tek VPS, tek bot süreci, systemd. Adım adım kurulum, servis dosyası (`deploy/dipbot.service`), ön kontrol betiği (`deploy/onkontrol.sh`), yedek/geri yükleme, log ve güncelleme: [`deploy/README-DEPLOY.md`](deploy/README-DEPLOY.md). Kurulum sonrası **canlı doğrulama planı** (hangi adımda ne beklendiği, sonuç tablosu): [`deploy/CANLI-DOGRULAMA-PLANI.md`](deploy/CANLI-DOGRULAMA-PLANI.md).

Özet: `/opt/dipbot` (kod), `/var/lib/dipbot` (veri), `/etc/dipbot/dipbot.env` (0600 root; `BOT_TOKEN`, `OWNER_TELEGRAM_ID`), servis kullanıcısı `dipbot`; `PYTHONPATH=/opt/dipbot/src` birim dosyasında tanımlıdır. Önce kurucunun botla **özel sohbette `/start`** göndermesi gerekir (Telegram botlar kullanıcıya kendiliğinden yazamaz).

## Bilinen sınırlar
- İngilizce arayüz: yalnız Telegram'a giden metinler çevrildi. Günlük (log) satırları, kod yorumları, belgeler ve konsol/başlangıç hataları (`BOT_TOKEN` eksik vb.) Türkçe kalabilir. Veritabanında önceki sürümden kalmış **eski Türkçe** `tarama_isleri.son_hata` değerleri, ilgili iş bir sonraki başarılı taramada temizlenene kadar durum ekranında olduğu gibi görünebilir. Kuyrukta bekleyen bildirimler gönderim anında biçimlendirildiği için İngilizce gider.
- Kontroller aralıklıdır (yukarıya bakın); gerçek hareket zamanı bilinmez.
- Outbox teslimi "en az bir kez"dir; çok parçalı mesajda parça tekrarı olabilir.
- Zaten gönderilmiş bir "katıldı" bildirimi, sonradan mükerrer olduğu anlaşılırsa geri alınamaz.
- İstek sınırı bekleme penceresi bellektedir (yeniden başlatmada sıfırlanır).
- Tarama zamanlaması, outbox yeniden deneme beklemesi (ve 24 saatlik yaş sınırı), haftalık hatırlatma, `.prev` yaşı ve yetkisiz-deneme/denetim bildirimlerinin tekrar-engel pencereleri duvar saatine bağlıdır; saat atlarsa gecikebilir, öne gelebilir veya bildirim kısa süre bastırılabilir (bekleyen **onayların** güvenliğini etkilemez). Linux monotonic saati askıya almayı saymaz (VM duraklaması yanlış "saat değişti" üretebilir; güvenli yöndedir).
- Token `.prev` yedeği tektir: üst üste iki yenilemede (A→B→C) ilk token kaybolur (`.prev` = B). Yedek, taramalar token nedeniyle duraklatılmışken yaş nedeniyle silinmez.
- Kod sürümünü geri almak normalde veritabanını geri yüklemeyi gerektirmez (PR #4 şemayı değiştirmez; şema geçmişi yalnız sütun ekler). Yedekten geri yükleme yalnız veri bozulmuşsa kullanılır; ayrıntı: `deploy/GUNCELLEME-PLANI.md` §6.
- Savaş listesi sayfalaması **canlıda doğrulanmadı**; bot "tam liste" bayrağı gelmeyen listeyi eksik sayar. `/members` büyük partide tek istekte **100 üye** döndürür (VPS günlüklerinden); **sayfalama yöntemi, sıralama ölçütü (XP) ve bunun dışındaki üyelere nasıl ulaşılacağı doğrulanmadı** → büyük partilerde okunamayan üyeler izlenemez (özet bunu belirtir). Çözüm, gerçek Network kaydına dayanmalıdır (bkz. `API-ARASTIRMA.md` §9c).
- XP süzgeci için API'deki `xp` alanının **toplam XP** olduğu varsayılır (örnek yanıtlarla tutarlı, canlıda doğrulanmadı). Aynı eşik-altı oyuncu birden fazla takipli partide görünürse (çift üyelik olmamalı) sayaç oyuncu başınadır; nitelikli bir gözlem sayacı sıfırlar. Gecikmiş (eski) bir parti yanıtı tek seferlik bir katılma/ayrılma çifti üretebilir (gerçek taşınmadan ayırt edilemez); aynı yanıtın tekrar taranması yeni bildirim üretmez.
- Bu sürümden önce otomatik eklenmiş oyuncular (düşük XP'liler dahil) silinmez; XP kuralına yalnız XP süzgecinden geçtikten sonra tabi olurlar. Mevcut partilerde yeni katılan düşük XP'li üyeler artık otomatik eklenmez.

## Doğrulananlar ve doğrulanmayanlar
**Doğrulandı (otomatik test / araç ile; hepsi MOCK ya da yerel):**
- 1301 test: ilk ekleme bildirimsiz, kimlik üzerinden değişiklik, bölge/vatandaşlık bağımsızlığı, tekrar bildirim yok, eksik cevap veriyi bozmaz, parti ilk kayıt/giriş/çıkış/kaldırma kuralları, grup izolasyonu, yetki matrisi (komut+buton), eski buton reddi, yeniden başlatmada veri korunumu, ilk savaş taramasında bildirim yok, outbox yeniden deneme/iptal, token doğrulama/`.prev`/maskeleme, D7 saat güvenliği, göç taşıma.
- Python 3.11, 3.12 ve 3.13'te test paketi geçti (Ubuntu 24.04 tabanlı bulut ortamında).
- `deploy/dipbot.service` için `systemd-analyze verify` (systemd 255, **statik**; yollar sahte düzende) hatasız; `backup.sh`/`restore.sh` gerçek WAL veritabanı ve **sahte** `systemctl` ile çalıştı; `onkontrol.sh` sahte dizin düzeniyle çalıştı.
- Kullanıcı DevTools'ta doğruladı (ben bağımsız doğrulamadım): `Authorization: Bearer <JWT>` + `X-Client-Platform: web` ile `GET /api/players/72400` → 200; savaş bağlantısı biçimi `…/wars/war/{numara}`; oturum token'ı JWT'dir.

**Doğrulanmadı (canlıda denenmeli — plan: `deploy/CANLI-DOGRULAMA-PLANI.md`):**
- Botun gerçek Diplomacia bağlantısı; `GET /api/players/72400`'ün **kimlik gerektirdiği** (başlıksız ve sahte Bearer ile 401) — bu yüzden token doğrulaması canlıda **kesinleşmiş sayılmaz**.
- Token ömrü; yeni girişin eski token'ı düşürüp düşürmediği; 401 dışı hata gövdeleri, 403/404/429 anlamları, `Retry-After`; `X-Client-Platform` zorunluluğu; `/members` ve `/api/wars` sayfalaması; `/api/wars/ended`; başka `status`/`war_type` değerleri; `attacker_power`/`defender_power`'ın "hasar" olup olmadığı (arayüzde "Güç" denir); `null` bölge/vatandaşlık anlamı; oyuncu UUID'sinin değişmezliği.
- Gerçek Telegram: polling, HTML görünümü, gizlilik modu, `delete_message`, `set_my_commands`, kanal doğrulaması (`getChat`/`getChatMember`) ve kanala gönderim, hız sınırları.
- Gerçek Ubuntu VPS: servis başlatma/yeniden başlatma, gerçek süreçte SIGTERM, otomatik yeniden başlama, zamanlayıcı/cron yedeği, uzun süreli çalışma, gerçek NTP saat atlaması.

## Proje yapısı ve testler
```
src/dipbot/            çekirdek: db, takip, yetki, yonetim, outbox, zamanlayici, kaynak, maske, zaman
  diplomacia/          API adaptörü (oyuncu/parti/üye/savaş ayrıştırıcıları, HTTP istemcisi, token doğrulayıcı)
  tg/                  Telegram katmanı (arayüz, biçimleme, gönderici, token deposu, onay işlemleri, çalıştırıcı)
tests/                 otomatik testler + tests/fixtures (gerçek yanıtlardan KISALTILMIŞ örnekler)
deploy/                systemd birimi, yedek/geri yükleme, ön kontrol, kurulum ve canlı doğrulama belgeleri
dp.txt                 asıl şartname · API-ARASTIRMA.md · findings.md · progress.md · task_plan.md · DEVAM.md · BULUT_DEVAM.md
```
Testler: `python -m pytest -q` (ağ kullanmaz). Geliştirme geçmişi, bulgular ve açık maddeler: `findings.md`, `progress.md`.
