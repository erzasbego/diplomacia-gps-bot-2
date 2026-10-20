# DEVAM — limit sıfırlandıktan sonra bu sırayla devam et

> **BULUT GEÇİŞİ (2026-10-20):** Bulut oturumu için ana giriş `BULUT_DEVAM.md`'dir. Tur F1 (denetim bildirimi, /grup_kaldir yalnız kurucu, E13) ve Tur F2a (D9 /grup_tasi) UYGULANDI (631 passed, MOCK); Tur F2b (D7 saat güvenliği + .prev politikası) hâlâ YAPILACAK. Aşağıdaki 'TUR F' maddelerinden 1, 2, 4 uygulandı; 3 (D7) ve 6 (.prev) bekliyor; 5 (token bekleme 5 dk) değişmedi; 7 (outbox) Tur E'de uygulandı; 8 (canlı doğrulama kesinleşmiş sayılmaz) geçerli.


> **GÜNCEL DURUM (2026-10-20):** Tur A, B ve C UYGULANDI (mock testlerle; ana oturum `pytest -q` → 558 passed). Bu dosyadaki §2 maddeleri artık TARİHSEL kayıttır. Kalanlar: §3 bağımsız doğrulama (qa + dar kapsamlı Opus), §4 kök README.md, §5 canlı doğrulama (kullanıcı güvenli token'ı yerel ortama koyunca ve izin verince), §6 doğrulanmayanlar listesi, ve kullanıcı kararı bekleyen maddeler: O-e denetim bildirimi, /grup_kaldir grupta açık/düğme kapalı tutarsızlığı, D7 (O7 saat geri atlaması), D9 (göçte veri taşıma). Ayrıntı: progress.md (2026-10-20 bölümü).


Kontrol noktası: 2026-10-19. Ayrıntılı geçmiş: `progress.md`. Bulgular: `findings.md`. API şemaları: `API-ARASTIRMA.md` (§6 oyuncu/parti, §7 savaş, §8 kimlik doğrulama). Şartname: `dp.txt`. Plan: `task_plan.md`.

## 0. Durum özeti (dürüst)
- Kod: Python 3.11, `python-telegram-bot==22.8`, SQLite. **`.venv\Scripts\python.exe -m pytest -q` → 465 passed (kontrol noktasında ana oturum çalıştırdı). Tamamı MOCK/fixture; canlı Diplomacia veya canlı Telegram ile HİÇBİR ŞEY doğrulanmadı.** Başarısız/atlanan test yok.
- **Henüz başlatılmayan/yarım düzeltme turu:** Opus ve ilk QA bulguları kodda UYGULANMADI (aşağıda §2). Kod şu an bulgulardan ÖNCEKİ halde.
- **QA tur 2 TAMAMLANDI** (rapor `findings.md` sonunda, KRİTİK yok; kod/test okuması, yeniden üretilmedi). Bulguları Tur A/B/C'ye dağıtıldı (aşağıda).
- **Canlı API bağlantısı HİÇ test edilmedi.** Bot şimdiye kadar Diplomacia'ya tek bir gerçek istek atmadı.
- Kullanıcının eski paylaştığı oyun tokenı güvenli değil sayılıyor: **kullanma, hiçbir yere yazma.** Yeni token yalnız kullanıcı tarafından yerel token dosyasına konacak.

## 1. İlk iş (ucuz)
1. `progress.md`, `findings.md` sonu ve bu dosyayı oku. `.venv\Scripts\python.exe -m pytest -q` çalıştır; beklenen 465 passed. Farklıysa dur, nedenini bul.
2. QA tur 2 raporu `findings.md` sonunda (ek QA turu gerekmez; düzeltmelerden sonra tekrar qa).

## 2. Düzeltme turları (tek yazıcı: `gelistirici`/Sonnet; ajanın tur sınırı ~35, bu yüzden bölünmüş)
Kural: her madde için ÖNCE hatayı gösteren test (başarısız çıktıyı gör), sonra en küçük düzeltme; her turdan sonra tam paket; testleri gevşetme. Aynı anda başka yazıcı ajan yok (qa/Opus salt okunur olabilir).

**Tur A — kimlik doğrulama + Opus YÜKSEK + 403/numara**
- Kullanıcı DOĞRULADI (`API-ARASTIRMA.md` §8): `Authorization: Bearer <JWT>` + `X-Client-Platform: web`. Adaptörde (`diplomacia/istemci.py` `OrtamKimlik`, `dogrulayici.py`) varsayılan şema Bearer olsun, her istekte `X-Client-Platform: web` gönderilsin; `DIPLOMACIA_AUTH_BASLIK`/`DIPLOMACIA_AUTH_SEMA` yalnız isteğe bağlı override. Token yalnız `DIPLOMACIA_TOKEN_DOSYASI` dosyasından. **Token yoksa/boşsa HİÇ istek atılmaz.** 401 → kurucuya tek bildirim + duraklat (mevcut davranış, yeniden test et). `.env.example`, `deploy/README-DEPLOY.md` buna göre güncelle.
- **Y-1:** token doğrulayıcı yalnız durum koduna bakıyor (`dogrulayici.py:60-64`, `tg/arayuz.py:1073-1086`). 200 + geçerli JSON/dict şartı; aday tokenla ikinci sınama (oturumsuz 401 verdiği doğrulanmış adres, örn. `/api/players/72400` veya `/api/wars`+`savaslari_ayristir`); yazmadan önce eski tokenı 0600 `.prev` sakla; red'de dosya değişmez. Testler: 200+HTML, 200+{}, profil 200 + diğer 401 → reddedilir.
- **Y-2:** `tg/calistir.py:84-88,107-116` yer tutucu kaynak sonradan token verilince değişmiyor (sahte "devam ediyor"). Kimlik (artık varsayılan) geçerliyse token dosyası olmasa da `IstemciKaynagi` kur; dosya yoksa istek yerine TokenGecersiz/yapılandırılmadı.
- **O-a:** 403 her yerde "token geçersiz" (`istemci.py:214-215`, `zamanlayici.py:225-226`, `dogrulayici.py:65`). Duraklatmayı yalnız 401'e bağla; 403 → ayrı sınıf/SiteErisilemez + geri çekilme.
- **O-b:** numara kanonik değil (`072400`≠`72400`) → `^[1-9][0-9]{0,11}$` + `str(int())` (`arayuz.py:55-60`, `istemci.py:54`); aynı grupta aynı `uid` başka oyuncu_id altındaysa ekleme reddi.

**Tur B — Opus ORTA/DÜŞÜK**
- O-c Telegram RetryAfter bir sohbette outbox döngüsünü/tick'i bloke ediyor (`gonderici.py:111-131`, `outbox.py:116-136`): bekleme alan hedefi o turda atla.
- O-d arayüz istekleri zamanlayıcının duraklatma/geri çekilmesini atlıyor (`calistir.py:112` ayrı SinirliKaynak): tek paylaşılan SinirliKaynak; arayüz ağdan önce `token_duraklatildi`/istek sınırı penceresine baksın; TokenGecersiz zamanlayıcıya iletilsin.
- O-e yetki/grup değişikliklerinde kurucuya outbox denetim bildirimi; `/grup_kaldir` grupta açık, düğme kapalı (tutarsız).
- D1 parti_kaldirma_onayla işlemi yapanın yetkisi transaction içinde yeniden denetlensin (`takip.py:548-549`); D2 onay kalkınca tarama güncellemesi hiç yazılmasın (`takip.py:163-165,271-291`); D3 outbox `gonderildi` güncellemesine `AND durum='bekliyor'` (`outbox.py:142-145`); D5 `parti_kaldirma_hazirla` TokenGecersiz'i yutmasın (`takip.py:514`); D6 `yetki_ver` özel Telegram kimliklerini reddetsin (1087968824, 136817688); D10 `_tam_sayi` 20 hane.
- Karar gerekenler (dp.txt'ye bak, kullanıcıya sor): O-e denetim bildirimi, D7 (O7 zaman karşılaştırması), D9 (supergroup göçünde veri taşıma — şu an taşınmıyor, kurucu elle onaylar).

**Tur C — QA tur 2 bulguları** (Y1/Y2 = Opus Y-2/Y-1, Tur A'da; kalanlar): O-c 404 hepsi→başarısız tarama (hiç gözlem yoksa `son_basari` güncellenmesin); O-d savaş yeni-bildirim sel sınırı (tarama başına üst sınır+özet) ve `durum_taninmiyor` → bildirim yerine uyarı, arayüze 'sayfalama yok varsayımı' notu; O-e `parti_ekle` N sıralı istek (baseline_tamam=0 yoluna bırak veya arka plan); O-f çok parçalı mesaj; göç sonrası eski grup onayını düşür/kurucuya sor; token bekleme durumu kalıcı/beklenmeyen token mesajını sil; `SirFiltresi`'ne Diplomacia tokenı; bayat `deploy/README-DEPLOY.md:194,196`; yanıltıcı `istemci.py:212` yorumu; 'Güç' ekranına 'hasar olduğu doğrulanmadı' notu; ölü kod `baglanti_ayristir`. Zayıf/sabitleyici testleri (test_dogrulayici 200/403, test_kaynak_olustur_token_yoksa, test_token_yenileme_*, test_grup_goc_kaydi) düzelt, eksik senaryoları ekle (findings.md QA tur 2 listesi).

## 3. Bağımsız doğrulama
1. Tam paket (`pytest -q`), sonucu `progress.md`'ye yaz.
2. `qa` (Sonnet, salt okunur): Telegram yetki/grup izolasyonu + düzeltmelerin yeterliliği.
3. `kritik-inceleme` (Opus, salt okunur, tur sınırı ~18 → kapsamı DAR tut): Y-1, Y-2, O-a, O-d, yetki yolları, token yazma/yedek. Opus ciddi hata bulursa Sonnet'e düzelttir; iki başarısız denemede `zor-kod`.

## 4. Eksik teslimatlar (dp.txt bölüm 15)
- **Kök `README.md` (Türkçe) YOK:** kurulum, komut/buton kullanımı, token yenileme adımları, "aralıklı kontrol" sınırı (kontroller arasında olup geri alınan değişiklik görülmez; tespit zamanı kesin hareket zamanı değildir), yetki modeli, veri klasörü/yedek, doğrulananlar-doğrulanmayanlar listesi. (`deploy/README-DEPLOY.md` var ama kök README yerine geçmez.)
- Bot Durumu/komut listesi `set_my_commands` canlıda denenmedi.
- `.claude/agent-memory/` dosyaları gelistirici/api/devops/kritik-inceleme tarafından yazıldı; doğrulanmış kararları `hafiza` ile derle (token/sır yazma).

## 5. Canlı doğrulama planı (SADECE kullanıcı yerel yapılandırmayı hazırlayınca; öncesinde yapma)
Ön koşullar (kullanıcı yapar): yeni güvenli Diplomacia tokenı → yerel token dosyası (0600, `.gitignore` kapsamında); ayrı `BOT_TOKEN` (BotFather) ve `OWNER_TELEGRAM_ID` env; **kullanıcıdan canlı test için açık izin al.**
1. Salt okunur: `GET /api/players/72400` (Bearer + `X-Client-Platform: web`) → 200 ve ayrıştırma; `/api/parties/5830`, `/members`, `/api/wars`. Başlıksız/geçersiz token → 401 davranışı. 404 (olmayan oyuncu), 403, 429/`Retry-After`, `X-Client-Platform` olmadan davranış, `/members` sayfalama (büyük parti), `/api/wars/ended`, tokenın `exp` değeri/ömrü.
2. Telegram: kurucu özel sohbet `/start`; onaylı test grubu; onaysız gruba ekleme; yetkisiz deneme; `/oyuncu_ekle`, `/parti_ekle`, `/savaslar`; butonlar; HTML görünümü; gizlilik modu; `set_my_commands`; migrate; SIGTERM; systemd (`systemd-analyze verify`).
3. Sonuçları ayrı "canlı" başlığında raporla; mock testlerle karıştırma.

## 6. DOĞRULANMAMIŞ listesi (hiçbirini doğrulanmış sayma)
Canlı Diplomacia bağlantısı; token ömrü; 401 dışındaki hata gövdeleri/403/404/429; `X-Client-Platform` zorunluluğu; `/members` ve `/api/wars` sayfalaması; `/api/wars/ended`; başka `status`/`war_type` değerleri; `attacker_power/defender_power` = "hasar" olup olmadığı (arayüzde "Güç" denir); `province_name`/`citizenship_*` `null`'ın anlamı; UUID'nin değişmezliği (iki `/api/wars` görüntüsünde `id` sabit kaldı, oyuncu UUID'si için tek örnek); `/api/players/profile` durum kodu güvenilirliği; canlı Telegram (polling, HTML, gizlilik modu, migrate, `set_my_commands`); systemd servisi/yedek-geri yükleme; SIGTERM gerçek süreçte.
Kullanıcının DOĞRULADIĞI (ben bağımsız doğrulamadım): kimlik başlığı biçimi (§8); savaş bağlantısı `https://diplomacia.com.tr/wars/war/{war_number}`; JWT oturum tokenı.

## 7. Yapma
- Token/çerez/anahtarı koda, teste, fixture'a, loga, belgeye, hafızaya yazma; eski paylaşılan tokenı kullanma.
- `git push`, canlı dağıtım/VPS bağlantısı, gerçek Telegram mesajı, canlı hesaba işlem: kullanıcı onayı olmadan yok.
- Doğrulanmamış alan/uç nokta uydurma; mock sonucunu canlı doğrulama gibi sunma.
- Aynı dosyada iki yazıcı ajan çalıştırma; gereksiz ajan açma (önce tek ajan).
- `dp.txt`, `API-ARASTIRMA.md`, fixture'lar ve mevcut testleri silme/ezme.

## 8. Ajan/model kullanım kuralı (kullanıcı tercihi)
Varsayılan Sonnet; Haiku yalnız araştırma/hafıza; Opus yalnız kritik inceleme veya Sonnet iki farklı denemede çözemezse. QA salt okunur. Alt ajanlar ajan başlatmaz. Her ajanın raporundaki test sayılarını ana oturum KENDİ çalıştırarak doğrular.

---

# TUR F — kullanıcı kararları (2026-10-20), Tur E bittikten sonra, tek yazıcı gelistirici; sonra qa+Opus (dar), sonra kök README.md ve canlı doğrulama planı

Öncelik: güvenlik, veri bütünlüğü, Telegram grup izolasyonu. Gereksiz yeni özellik YOK.
1. **O-e denetim bildirimi:** yetki ve grup değişikliklerinde (yetki_ver/yetki_al, grup_onayla/grup_kaldir, göç, grup taşıma) KURUCUYA özel mesaj (outbox, yalnız kurucu özel sohbeti). İçerik: eylem türü, eylemi yapan kullanıcı id, hedef kullanıcı/grup id, zaman (Europe/Istanbul). TOKEN veya başka hassas veri YOK. Tekrarlı bildirim engeli (aynı eylem+hedef+aktör için pencere/idempotency; seli özetle). Öneri: kurucunun KENDİ yaptığı eylemler için bildirim yok (komut yanıtı zaten gösterir); bunu config bayrağıyla belgele.
2. **/grup_kaldir:** komut ve düğme AYNI politikayı uygulasın; grup kaldırma YALNIZCA kurucu tarafından (yetkili değil). Servis katmanında da (yonetim.grup_kaldir) aktör=kurucu kontrolü (derinlemesine savunma); sistem kaynaklı kaldırma (göçte otomatik düşürme) açıkça ayrı yol. Testler: yetkili kullanıcı komutla da düğmeyle de kaldıramaz; kurucu kaldırır.
3. **D7 saat güvenliği:** süresi geçmiş veya geçerliliği belirsiz onaylarla işlem yapılamasın. Zaman damgası karşılaştırması yerine (a) bekleyen işlem/onay TTL'lerinde süreç içi monotonic saat + duvar saati tutarlılık kontrolü (duvar saati geriye giderse veya created_at'ten önceyse işlem geçersiz → 'saat değişti, işlemi yeniden başlatın'), yeniden başlatmada bekleyen onay/token-bekleme geçersiz; (b) grup onayı 'dönemi' (O7) için zaman damgası yerine DB'de artan tamsayı 'onay_donemi' sayacı (outbox satırı oluşturulurken dönemi saklar; gönderimde eşleşmeyen iptal). Gerekirse yeniden onay iste. Testler: saat geri/ileri atlaması simülasyonu.
4. **D9 göç:** yeni supergroup id'ye OTOMATİK yetki YOK; eski takip verisi SİLİNMEZ. Göç olayı (eski→yeni) kayıt altına alınsın (bekleyen göç tablosu). Kurucu özel sohbette onayla başlatılan, yalnız KAYITLI çiftler için `/grup_tasi <eski> <yeni>` (butonla onay): yeni grup kurucu tarafından önceden onaylı olmalı, eski onaysız (düşürülmüş), yeni grupta mevcut takip verisi varsa REDDET (karışma yok) ve tek transaction'da takip_oyuncular, partiler, parti_uyeleri, engeller, grup ayarları, savaş baseline'ı, bildirim durumu eski→yeni taşınsın; eski id'deki bekleyen outbox iptal; taşıma sonrası denetim bildirimi (madde 1). Başarısızlıkta hiçbir şey değişmesin. Testler (izolasyon dahil).
5. **Token bekleme süresi 5 dk KALIR** (değişiklik yok).
6. **.prev politikası:** süresiz tutulmasın. Önerilen: yeni token ilk başarılı GERÇEK istekle (OyuncuDogrulayici dışı; zamanlayıcıdaki ilk başarılı yetkili tarama) teyit edilince VEYA TOKEN_PREV_OMRU (varsayılan 24 saat) dolunca otomatik güvenli silme (0600, üzerine yazma yok sadece unlink; Windows notu), zamanlayıcıda periyodik kontrol; geri dönüş yolu (elle `.prev` → token dosyası) ve silme politikası deploy/README-DEPLOY.md + kök README'de belgelensin. Testler.
7. **Outbox:** iptal edilmiş kayıtların hiçbir yoldan yeniden etkinleşmemesi (Tur E'de E5 kapsamına eklendi; Tur F'de yalnız doğrulama/regresyon).
8. **Canlı token doğrulaması kesin SAYILMAYACAK:** `GET /api/players/72400`'ün kimlik doğrulaması GEREKTİRDİĞİ ayrıca doğrulanmadı (başlıksız ve sahte/süresi dolmuş Bearer ile 401 aldığı kullanıcı tarafından/canlı testle doğrulanmalı). Doğrulayıcı mesajları ve belgeler bunu 'canlı doğrulanmadı' olarak taşır.
Tur F sonrası rapor: QA/Opus bulgularından hangileri KAPANDI, hangileri AÇIK, test sonuçları; sonra kök README.md (Türkçe) ve canlı doğrulama planı.
