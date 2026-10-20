# BULUT_DEVAM — Claude Code Cloud'da kaldığımız yerden devam

Hazırlanma: 2026-10-20 (yerel Windows oturumundan). Bu dosya bulut oturumunun **ilk okuyacağı** dosyadır. Ardından sırayla: `progress.md` (geçmiş), `task_plan.md` (durum), `findings.md` (bulgular ve kapanış özeti), `API-ARASTIRMA.md` (doğrulanan/doğrulanmayan API bilgisi), `DEVAM.md` (ayrıntılı tur planları, "TUR F" bölümü), `dp.txt` (asıl şartname).

> **GÜNCEL (bulut oturumu sonu):** §6 madde 2 (Tur F2b), 3 (bağımsız doğrulama), 4 (kök README) ve 6 (VPS dosya düzeyi hazırlık) TAMAMLANDI; 679 test. Kalan: §5 kullanıcı kararları, §6 madde 7 (canlı doğrulama — `deploy/CANLI-DOGRULAMA-PLANI.md`, yalnız kullanıcı onayı ve yerel/VPS token ile). Ayrıntı: progress.md 'BULUT OTURUMU', findings.md 'BULUT QA'.

## 0. Dürüst durum (tek bakışta)
- **Doğrulanmış son test sayısı: 631 passed, 0 failed, 0 skipped** (`python -m pytest -q`; ana oturum yerelde 3 kez çalıştırdı). **Hepsi MOCK**: sahte kaynak, `httpx.MockTransport`, sahte Telegram nesneleri.
- **Canlı Diplomacia API'ye HİÇ istek atılmadı. Canlı Telegram'a HİÇ bağlanılmadı. VPS'e bağlanılmadı. Hiçbir şey canlıda doğrulanmadı.**
- Çalışan ajan yok; yarım kalmış kod değişikliği yok (dosyalar tutarlı, testler yeşil). Yapılmamış işler aşağıda "Yapılacaklar".
- Yerel git deposu hazırlandı (dal `main`, ilk commit yerelde). Henüz GitHub'a push/yükleme YAPILMADI; GitHub deposu daha sonra PRIVATE olarak oluşturulacak (kullanıcı kararı).

## 1. Ne bu proje
Diplomacia (diplomacia.com.tr) oyunundaki oyuncuları, partileri ve savaşları izleyen, Telegram'dan yönetilen, VPS'te sürekli çalışacak bir bot. Python 3.11 + `python-telegram-bot==22.8` (long polling) + SQLite. Telegram arayüzü ve mesajlar Türkçe; gösterilen zamanlar Europe/Istanbul, kayıt UTC. Şartname: `dp.txt`.

## 2. Proje yapısı
```
src/dipbot/
  __main__.py            python -m dipbot giriş noktası
  config.py              sabitler (tarama aralıkları [5,10,15,20], varsayılan 10; outbox/denetim/savaş limitleri)
  db.py                  SQLite şeması, migration (SEMA_SURUMU=3), islem() transaction yardımcısı
  kaynak.py              DiplomaciaSource Protocol, normalize veri tipleri, hata sınıfları (TokenGecersiz, ErisimReddedildi, TokenYok...), SinirliKaynak, ZamanlayiciKorumali
  takip.py               oyuncu/parti/savaş takip mantığı (TakipServisi)
  yetki.py               saf yetki kararları (kurucu/yetkili/onaylı grup; KURUCU_EYLEMLERI)
  yonetim.py             yetki_ver/al, grup_onayla/kaldir(/sistem), denetim_bildir, goc_kaydet, grup_tasi
  outbox.py              kalıcı bildirim kuyruğu (claim/lease, tüm UPDATE'ler durum='bekliyor' koşullu)
  zamanlayici.py         tarama zamanlayıcısı, token duraklatma/kuşak, haftalık token hatırlatma
  maske.py               sır maskeleme (bot token, JWT, Bearer, çalışma anı sırları)
  zaman.py
  diplomacia/            ayristir.py (oyuncu/parti/üye), savas.py, istemci.py (IstemciKaynagi, OrtamKimlik), dogrulayici.py (OyuncuDogrulayici)
  tg/                    arayuz.py (komutlar+düğmeler), bicim.py (HTML), gonderici.py, token.py (TokenDeposu, .prev), islemler.py (onay işlemleri), uygulama.py, calistir.py, yapilandirma.py, gunluk.py, modeller.py, kaynak_yok.py
tests/                   ~25 test dosyası (+ tests/fixtures: gerçek yanıtlardan KISALTILMIŞ örnekler)
deploy/                  dipbot.service, backup.sh, restore.sh, README-DEPLOY.md  (TASLAK, sunucuda doğrulanmadı)
docs/TELEGRAM-ARASTIRMA.md   Telegram kütüphane/API araştırması (Haiku, kaynakları ayrıca doğrulanmadı)
(.claude/agent-memory/ yerelde var ama kullanıcı kararıyla ilk aktarıma/commit'e DAHİL DEĞİL; kalıcı kararlar progress.md/findings.md/DEVAM.md içinde de kayıtlı)
```

## 3. Kurulum ve komutlar (bulut = Linux varsayımı; yerelde Windows'ta `.venv\Scripts\python.exe` kullanılıyordu)
```bash
python3.11 -m venv .venv && . .venv/bin/activate     # Python 3.11
pip install -r requirements.txt                      # tzdata, python-telegram-bot==22.8, pytest
python -m pytest -q                                  # BEKLENEN: 631 passed (uyarılar: PTB RetryAfter deprecation, zararsız)
python -m pytest -q -W ignore::DeprecationWarning
python -m pytest tests/test_tur_d9.py -q             # tek dosya
python -m dipbot                                      # CANLI ÇALIŞTIRMA: env yoksa çıkış kodu 2 (BOT_TOKEN/OWNER_TELEGRAM_ID); BULUTTA ÇALIŞTIRMA
```
Ortam değişkenleri (`.env.example`; uygulama `.env` DOSYASINI OKUMAZ, yalnız süreç ortamını): `BOT_TOKEN` (BotFather), `OWNER_TELEGRAM_ID` (pozitif tamsayı), `DB_PATH`, `DIPLOMACIA_TOKEN_DOSYASI` (Diplomacia JWT'si BURADA, 0600, DB'de/env'de DEĞİL), isteğe bağlı: `DIPLOMACIA_AUTH_BASLIK`, `DIPLOMACIA_AUTH_SEMA` (varsayılan Authorization/Bearer), `DIPLOMACIA_DOGRULAMA_OYUNCU` (varsayılan 72400). Her isteğe `X-Client-Platform: web` eklenir. Çıkış kodları: 2 yapılandırma hatası, 3 geçersiz bot token/Conflict.

## 4. Çalışma kuralları (yerel genel talimatlar bulutta YOK; aynen uygula)
- Türkçe iletişim. Doğrulanmamışı doğrulanmış gösterme; mock testleri canlı doğrulamadan ayrı raporla; tamamlanmamış incelemeyi tamamlanmış sayma.
- Sır/token/çerez/anahtarı koda, teste, fixture'a, loga, belgeye, hafızaya, ajan istemine YAZMA (testte yalnız tamamen uydurma değerler). Kullanıcının daha önce sohbete yapıştırdığı Diplomacia tokenı güvenli değil: KULLANMA.
- Kullanıcı onayı olmadan: `git push`, canlı dağıtım/VPS bağlantısı, gerçek Telegram mesajı, canlı Diplomacia isteği/hesap işlemi, satın alma yok. (Kullanıcı ayrıca "canlı API testini güvenli yeni token yerel ortamda sağlanınca yap" dedi; bulutta yerel token yok → canlı test bulutta YAPILMAZ.)
- Model politikası: varsayılan Sonnet; Haiku yalnız araştırma/hafıza; Opus yalnız kritik inceleme (salt okunur) veya Sonnet iki farklı denemede çözemezse; QA salt okunur; alt ajanlar ajan başlatmaz; AYNI dosyalarda tek yazıcı.
- Yöntem: her madde için ÖNCE hatayı gösteren davranış testi (assert başarısızlığı), sonra en küçük düzeltme; mevcut testleri GEVŞETME (bilinçli değişiklik gerekçelendirilir); her ajan raporundaki test sayısını ana oturum KENDİ çalıştırarak doğrular.
- **Ajan tur sınırı:** yerelde ajanlar ~35 turda rapor vermeden takıldı (en az 7 kez). İşleri küçük tut (≤ 6-8 madde), ajana "≈28. turda DUR ve rapor ver" yaz; takılırsa önce `pytest` ve değişen dosyalara bak, sonra aynı ajanı 'durum raporu + kalan' diye devam ettir (körlemesine yeniden başlatma). Opus ~18 turda sınırlı: 12. turda raporunu vermesini iste.
- Yerel ajan tanımları (arastirmaci, gelistirici, api, qa, kritik-inceleme, zor-kod, devops, hafiza) kullanıcının makinesinde idi; bulutta yoksa genel amaçlı ajanı AYNI istem/rol kurallarıyla kullan (qa/kritik-inceleme için "yalnız okuma" kısıtını istemde belirt).

## 5. Tamamlananlar (mock testli)
Araştırma (API şemaları: oyuncu, parti, üyeler, savaş, kimlik başlığı); çekirdek (SQLite, yetki/grup izolasyonu, takip, outbox, zamanlayıcı, haftalık token hatırlatma); Telegram katmanı (komutlar+butonlar, yetki, token menüsü, savaş ekranı+tıklanabilir `wars/war/{war_number}` bağlantısı, denetim bildirimi, `/grup_kaldir` yalnız kurucu, `/grup_tasi` onaylı göç taşıma); gerçek adaptör (oyuncu/parti/savaş ayrıştırıcıları + HTTP istemcisi: Bearer + `X-Client-Platform: web`); token güvenliği (aday token doğrulama, `.prev` yedek, maskeleme, kuşak sayacı); QA/Opus bulgularının Tur A-E-F1-F2a düzeltmeleri. Ayrıntı: `progress.md`, `findings.md` (kapanış özeti), `DEVAM.md`.

## 6. Yapılacaklar — SIRAYLA
1. **Başlangıç:** bu dosya + progress.md + task_plan.md + findings.md oku; kurulum yap; `pytest -q` → 631 passed bekle (farklıysa dur ve nedenini bul).
2. **Tur F2b (tek yazıcı, ≈6 madde, rapor limiti koy):** 
   - **D7 saat güvenliği** (kullanıcı kararı): süresi geçmiş/geçerliliği belirsiz onaylarla işlem yapılamasın. Bekleyen işlem/onay (tg_islemler) ve token-bekleme TTL'lerinde süreç içi monotonic saat + duvar saati tutarlılık kontrolü (duvar saati geri gider veya created_at'ten önce olursa işlem geçersiz → "saat değişti, işlemi yeniden başlatın"); yeniden başlatmada bekleyen onay/token-bekleme geçersiz; grup onayı "dönemi" (O7, outbox bayat satır) zaman damgası yerine DB'de artan tamsayı `onay_donemi` sayacı. Saat geri/ileri atlaması simülasyon testleri. (Şu an `olusturuldu >= onaylandi` metin karşılaştırması; `tests/test_tur_d.py::test_c7_*` davranışı belgeliyor — bilinçli değişecek.)
   - **`.prev` politikası** (kullanıcı kararı): süresiz tutulmasın. Önerilen: yeni token ilk başarılı gerçek yetkili tarama/istek ile teyit edilince VEYA `TOKEN_PREV_OMRU` (24 sa) dolunca otomatik güvenli silme (unlink; zamanlayıcıda periyodik kontrol); geri dönüş yolu (elle `.prev` → token dosyası) ve silme politikası `deploy/README-DEPLOY.md` + kök README'de belgeli. Testler.
   - Token bekleme süresi 5 dk KALIR (kullanıcı kararı).
3. **Bağımsız doğrulama (salt okunur):** `qa` + dar kapsamlı `kritik-inceleme` (Opus) — **F1 (denetim bildirimi, /grup_kaldir politikası, E13), F2a (D9 /grup_tasi: veri bütünlüğü, grup izolasyonu, yetki, transaction geri alma, şema taraması yeterliliği) ve F2b** için; bu üç tur henüz HİÇBİR bağımsız inceleme görmedi. Bulguları `findings.md`'ye yaz; ciddi olanı tek yazıcı Sonnet'e düzelttir; iki başarısız denemede `zor-kod`/Opus.
4. **Kök `README.md` (Türkçe)** — YOK, dp.txt bölüm 15 teslimatı: kurulum, komut ve buton kullanımı, yetki modeli (kurucu/yetkili/onaylı grup), token yenileme adımları, "aralıklı kontrol" sınırı (kontroller arasında olup geri alınan değişiklik görülmez; tespit zamanı kesin hareket zamanı değildir), veri klasörü/yedek/geri yükleme, `.prev` politikası, outbox "en az bir kez" teslim sınırı, doğrulananlar/doğrulanmayanlar listesi.
5. **Kullanıcı kararı bekleyenler (sor, uygulama):** (a) kurucu grupta `gk:` düğmesine basınca şu an ESKI_DUGME (grup listesi yalnız özelde) — grupta da çalışsın mı? (b) göçte kurucu iki mesaj alıyor (grup_goc + denetim) — birleştirilsin mi? (c) tekrarlı parti üye listesi hata olarak kalsın mı yoksa ayıklansın mı? (d) denetim bildirimi pencere 10 dk / saatte 5 / sonrası özet uygun mu? (e) dp.txt ile doğrulanacak kararlar: 404 eşiği 3 ve "hepsi 404 = tarama başarısız"; yeni savaş bildirimi üst sınırı 5; tanınmayan durum = yeni savaş sayılmaz; `parti_ekle` ayrıntıyı ilk taramaya bırakır; göçte eski grubun otomatik düşürülmesi; eski onaysız ama verili grup için göç kaydı; kurucu bile grup içinden `/grup_tasi` başlatamaz.
6. **VPS/dağıtım hazırlığı (dosya düzeyinde):** `deploy/dipbot.service` ve betikler taslak; `systemd-analyze verify`, gerçek SIGTERM kapanışı, yedek/geri yükleme sunucuda DOĞRULANMADI. Bulutta bunlar çalıştırılmaz; yalnız belge/betik tutarlılığı.
7. **Canlı doğrulama planı** (yalnız kullanıcı yerel makinede güvenli YENİ token'ı token dosyasına koyunca ve açık izin verince; bulutta YAPILMAZ): 
   - Salt okunur: Bearer + `X-Client-Platform: web` ile `GET /api/players/72400` → 200 + ayrıştırma; `/api/parties/5830`, `/members`, `/api/wars`. **`GET /api/players/72400`'ün kimlik doğrulaması GEREKTİRDİĞİ (başlıksız VE sahte/süresi dolmuş Bearer ile 401) ayrıca doğrulanmadan token doğrulaması canlıda kesinleşmiş sayılmaz.** 404 (olmayan oyuncu), 403, 429/`Retry-After`, `X-Client-Platform` olmadan davranış, `/members` sayfalama (büyük parti), `/api/wars/ended`, token `exp`/ömrü ve yeni girişin eski tokenı düşürüp düşürmediği.
   - Telegram: kurucu özel sohbet `/start`; onaylı test grubu; onaysız gruba ekleme; yetkisiz deneme; `/oyuncu_ekle`, `/parti_ekle`, `/savaslar`; butonlar; HTML görünümü; gizlilik modu; `set_my_commands`; `delete_message`; migrate ve `/grup_tasi` (önce DB yedeği); SIGTERM; systemd.
   - Sonuçları "canlı" başlığında raporla, mock testlerle karıştırma.

## 7. DOĞRULANMAMIŞ (hiçbirini doğrulanmış sayma)
Canlı Diplomacia bağlantısı; token ömrü; 401 dışındaki hata gövdeleri; 403/404/429 anlamları; `X-Client-Platform` zorunluluğu; `/members` ve `/api/wars` sayfalaması; `/api/wars/ended`; başka `status`/`war_type` değerleri; `attacker_power/defender_power` = "hasar" (arayüzde "Güç" etiketi + not); `province_name`/`citizenship_*` `null` anlamı; oyuncu UUID'sinin değişmezliği; `/api/players/72400` doğrulamasının kimlik gerektirmesi; Telegram canlı davranışı (polling, HTML, gizlilik modu, migrate, delete_message, set_my_commands); systemd/yedek/SIGTERM. Kullanıcının DOĞRULADIĞI (ben bağımsız doğrulamadım): `Authorization: Bearer <JWT>` + `X-Client-Platform: web` (DevTools'ta başarılı `GET /api/players/72400`); savaş bağlantısı `https://diplomacia.com.tr/wars/war/{war_number}`; oturum tokenının JWT olduğu.

## 8. Bilinen sınırlar (belgelenmiş)
Outbox "en az bir kez" teslim (çökme/çok parçalı mesajda parça tekrarı); kontroller aralıklıdır (geri alınan değişiklik görünmeyebilir); göçte `bildirim_sinirlari`/`yetkisiz_denemeler` taşınmaz, eski id'de gönderilmiş outbox geçmişi kalır; zaten gönderilmiş "katıldı" bildirimi mükerrer çözümünde geri alınamaz; Windows'ta 0600 izni uygulanmaz (Linux'ta uygula); istek sınırı bekleme penceresi bellekte (yeniden başlatmada sıfırlanır).

## 9. Buluta TAŞINACAK dosyalar (ilk commit: 79 dosya; `.venv`, `.pytest_cache`, `__pycache__`, `.claude/` hariç)
Kod: `src/dipbot/**` (29 dosya); testler: `tests/**` (fixtures dahil); belgeler: `dp.txt`, `API-ARASTIRMA.md`, `DEVAM.md`, `BULUT_DEVAM.md`, `progress.md`, `task_plan.md`, `findings.md`, `docs/TELEGRAM-ARASTIRMA.md`, `deploy/**`; yapılandırma: `requirements.txt`, `pytest.ini`, `.env.example`, `.gitignore`; `.claude/agent-memory/**` KULLANICI KARARIYLA DAHİL DEĞİL (`.claude/` klasörünün tamamı `.gitignore`'da).
**TAŞINMAYACAK / yok:** `.venv/`, `.pytest_cache/`, `__pycache__/`, `.env`, gerçek token dosyaları (`*.token*`, `.token-*`), `*.prev`, `*.db`/`-wal`/`-shm`/`*.sqlite*`, `data/`, `backups/`, `secrets/`, `.claude/` (tamamı), `.claude/settings.local.json`, `.claude/projects/` (bunların hiçbiri şu an projede yok; `.gitignore` hepsini kapsar).
Yerel genel talimat dosyaları (`~/.claude/CLAUDE.md`, ajan/skill tanımları) bu projede DEĞİL; 4. bölümdeki kurallar onların özetidir.
