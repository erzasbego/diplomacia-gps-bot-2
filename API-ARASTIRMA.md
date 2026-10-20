# Diplomacia API araştırması

Tarih: 2026-10-19. Yöntem: kullanıcının oturum açmış Chrome'unda sayfa yüklenirken ağ isteklerinin **adresleri ve durum kodları** izlendi (ana oturum, Claude in Chrome). Yanıt gövdeleri okunamadı. Bu dosyadaki "Doğrulandı" ifadesi yalnızca "adres var ve oturumla 200 döndü" demektir; alan adı veya şema doğrulaması değildir.

## 1. Genel
- Site: Expo (React Native Web) SPA. Veri, aynı kökteki JSON API'den gelir: `https://diplomacia.com.tr/api/...`
- Tüm istekler GET. Cloudflare Insights beacon'ı 503 döndü; veri API'siyle ilgisi yok.
- Kimlik doğrulama: **çerez yok gibi görünüyor (çıkarım)**. Oturum başlığı olmadan yapılan sayfa içi `fetch(..., {credentials:'include'})` denemeleri 401 döndü (`/api/players/72400`, `/api/parties/player/72400`, `/api/wars`). Sayfanın kendi istekleri 200 döndü. Bu, büyük olasılıkla `Authorization` başlığı gerektiğini gösterir, ama **başlık biçimi doğrulanmadı** (araç çerez/depolama erişimini engelledi; atlatılmadı).

## 2. Uç noktalar (adres doğrulandı, gövde şeması doğrulanmadı)
| Amaç | Adres | Kaynak sayfa |
|---|---|---|
| Oyuncu ayrıntısı | `GET /api/players/{oyuncu_id}` (örnek 72400) | /player/72400 |
| Oyuncunun partisi | `GET /api/parties/player/{oyuncu_id}` | /player/72400 |
| Oyuncunun parti geçmişi | `GET /api/parties/player/{oyuncu_id}/history` | /player/72400 |
| Oturum sahibinin profili | `GET /api/players/profile` | tüm sayfalar |
| Parti ayrıntısı | `GET /api/parties/{parti_id}` (örnek 5830) | /party/5830 |
| Parti üyeleri | `GET /api/parties/{parti_id}/members` | /party/5830 |
| Parti etkinliği | `GET /api/parties/{parti_id}/activity` | /party/5830 |
| Parti binaları | `GET /api/parties/{parti_id}/buildings` | /party/5830 |
| Eyalet ayrıntısı | `GET /api/provinces/detail?province=Varna` | /profile/region/varna |
| Eyalette partiler | `GET /api/parties/province/Varna` | /profile/region/varna |
| Aktif savaşlar | `GET /api/wars` | /wars |
| Biten savaşlar | `GET /api/wars/ended` | /wars |
| Bağımsızlık durumu | `GET /api/wars/independence/status` | /wars |
| Kendi partilerim | `GET /api/parties/my` | /party/5830 |

## 3. Sayfadan görülen veriler (arayüz metni; API alan adı değil)
- **Oyuncu sayfası** `/player/72400`: ad (parti etiketiyle, ör. `[TEKNO] Ayetullah Mutahhari`), seviye, **ülke/vatandaşlık satırı** ("Macaristan Sosyalist Cumhuriyeti."), altında bağlı bölge ("Horasan"), **BÖLGE** bölümünde "Temeşvar" ve altında o bölgeyi elinde tutan ülke ("Bosna İslâm Cumhuriyeti"), parti ("TEKNO MERKEZ PARTİSİ"), ulus (Hırvatistan), son aktiflik. Bölge ile vatandaşlık ülkesi arayüzde ayrı alanlardır; botta ayrı modellenmeli. Hangi alanın hangi JSON anahtarına karşılık geldiği doğrulanmadı.
- **Parti sayfası** `/party/5830`: parti adı, ideoloji, lider, sözcü, üye sayısı ("2 ÜYE"), merkez, kuruluş tarihi. İlk üyeler sayfada listeleniyor; "TÜMÜNÜ GÖR" ile tam liste açılıyor (muhtemelen `/members`). Parti için **iki kimlik** görüldü: sayısal (5830) ve UUID (`/api/parties/{uuid}/press`). Sabit kimlik olarak hangisinin kullanılacağı API gövdesinden doğrulanmalı.
- **Eyalet sayfası** `/profile/region/varna`: eyalet adı, sahip ülke, oyuncu sayısı, vali, tarih akışı.
- **Savaşlar** `/wars`: "AKTİF SAVAŞLAR" listesi; her kartta saldıran/savunan taraf, kalan süre, durum. Savaş ayrıntı/hasar adresi **bulunamadı** (kartlara tıklanmadı).

## 4. Doğrulanamayanlar (geliştirme bunlara dayanmamalı)
> **GÜNCELLEME 2026-10-19:** 2. (gövde şemaları: oyuncu/parti/üyeler), 3. (sayfalama: örnekte alan yok, hâlâ doğrulanmadı) ve 7. (alan eşlemesi) maddeleri §6'daki kullanıcı örnekleriyle kısmen çözüldü. 1. (kimlik doğrulama), 4. (hata/429 davranışı) ve 5. (savaş) hâlâ açık.
1. Kimlik doğrulama başlığı biçimi, token ömrü, yenileme akışı.
2. Tüm yanıt gövdeleri: alan adları, tipler, boş/eksik değer davranışı.
3. `/members` sayfalaması (parametre adı, sayfa boyutu, toplam alanı).
4. Hata gövdeleri (401 dışında), 429 davranışı, `Retry-After`.
5. Savaş ayrıntısı, taraf hasarı, savaş kimliği alanları.
6. Vatandaşlık ülkesi uç noktası (`/profile/country/{uuid}`) incelenmedi.
7. Hangi JSON alanı "mevcut bölge", hangisi "vatandaşlık ülkesi".

## 5. Gerekli kullanıcı girdisi
Aşağıdakilerden biri yeterli (token/başlık değerleri **olmadan**):
- `players/72400`, `parties/5830/members`, `parties/player/72400`, `wars` yanıt gövdelerinden birer örnek (kişisel veri varsa kısaltılmış).
- İstek başlığı **adı ve biçimi** (ör. `Authorization: Bearer <...>`), değer olmadan.

---

# 6. Doğrulanmış yanıt şemaları (kullanıcının sağladığı gerçek örnekler, 2026-10-19)

Kaynak: kullanıcı Diplomacia'dan üç gerçek yanıt sağladı (üst anahtarlar `player_response`, `party_response`, `party_members_response` yalnızca örnekleri ayırmak içindir; gerçek gövde bu anahtarların ALTINDAKİ nesnedir). Hangi örneğin hangi adresten alındığı kullanıcı tarafından belirtilmedi; eşleme adlandırma ve §2'deki adreslerden **çıkarım**dır: oyuncu → `GET /api/players/{numara}`, parti → `GET /api/parties/{numara}`, üyeler → `GET /api/parties/{numara}/members`. `GET /api/parties/player/{numara}` ve `/history` yanıtları HÂLÂ görülmedi. Örneklerde token/çerez yok. Tam örneklerden kısaltılmış kopya: `tests/fixtures/`.

## 6.1 Oyuncu — gövde `{"player": {...}}`
| Alan | Tür | Not |
|---|---|---|
| `id` | UUID string | Kalıcı oyuncu kimliği (ad değişse de sabit olması beklenir; sabitliği doğrulanmadı) |
| `username` | string | **Saf oyuncu adı** ("Ayetullah Mutahhari") |
| `display_name` | string | Parti etiketli ad ("[TEKNO] Ayetullah Mutahhari"). Parti değişince değişir → **ad takibinde KULLANILMAMALI** (yanlış "ad değişti" bildirimi üretir) |
| `province_name` | string | **Mevcut bölge** adayı ("Horasan"). Oyuncu sayfası başlığında vatandaşlık ülkesinin yanında gösterilen değerle aynı; yine de "mevcut konum" anlamı API belgesinden değil sayfa eşleşmesinden çıkarıldı |
| `citizenship_country_id` / `citizenship_country_name` / `citizenship_country_flag` | UUID / string / URL | **Vatandaşlık ülkesi** |
| `citizenship_province`, `citizenship_is_independent` | null (örnekte) | Anlamı bilinmiyor; kullanma |
| `country_id`, `country_name` | UUID / string | Örnekte vatandaşlık alanlarıyla aynı; ikisi ayrışırsa hangisinin ne olduğu bilinmiyor → vatandaşlık için yalnız `citizenship_*` |
| `cabinet_*`, `mp_*`, `governor_*` | string/null | Kabine/vekil/vali; kapsam dışı. `null` meşru değer (ör. `mp_country_id: null`) |
| `level`, `xp`, `reputation`, `world_rank`, `battles_won/lost`, `total_wars` | sayı | Takip kapsamı dışı |
| `created_at`, `last_active` | ISO-8601 UTC (`...Z`, ms'li) | |
| `nation_code`, `nation{code,label,flag}` | string/nesne | Ulus (ör. HRV) — vatandaşlık DEĞİL |
| `badges[]`, `skills{}`, `passive_skills{}`, `equipped_items[]`, `levelProgress{}` | dizi/nesne | Kapsam dışı; yok sayılmalı |

**Önemli:** Oyuncu yanıtında **oyuncu numarası (72400) YOKTUR**. Numara yalnızca URL'de ve parti üye listesinde (`player_number`) bulunur. Bu yüzden numarayı `/oyuncu_ekle` bağlantısından alıp saklamak, API'den dönen UUID ile birlikte tutmak gerekir (UUID uyuşmazlığı = veri hatası, değişiklik değil).

## 6.2 Parti — gövde `{"party": {...}}`
| Alan | Tür | Not |
|---|---|---|
| `id` | UUID | Kalıcı parti kimliği |
| `party_number` | int | URL numarası (5830) |
| `name`, `abbreviation` | string | Ad değişimi takibi için `name` |
| `member_count` | int | Üye sayısı — **üye listesi tamlık çapraz kontrolü** için kullanılabilir (örnekte 2 == listedeki 2) |
| `leader_id` (UUID), `leader_name`, `leader_player_number` | | |
| `country_id`, `country_name`, `headquarters`, `ideology`, `is_open`, `parliament_seats`, `history_24h`, `created_at`, `updated_at`, `*_banned_until` | | Kapsam dışı |

## 6.3 Parti üyeleri — gövde `{"members":[...], "role_limits":{}, "loyal":{}}`
Üye alanları: `player_id` (UUID), `player_number` (int, URL numarası), `username` (saf ad), `display_name` (etiketli), `party_role` (`leader`/`spokesperson`/… ), `level`, `xp`, `country_name`, `country_flag`, `is_loyal`, `avatar_url`.
- Üye listesinde hem UUID hem numara var → oyuncu eşlemesi için yeterli.
- **Sayfalama alanı örnekte YOK** (`next`, `page`, `total`, `has_more` yok). Büyük partilerde sayfalamanın olup olmadığı, parametre adı ve sayfa boyutu **doğrulanmadı**. Tamlık denetimi: `len(members) == party.member_count` (çapraz kontrol); eşit değilse liste eksik sayılmalı, üye çıkışı çıkarılmamalı.
- `role_limits`, `loyal` (maaş/sadakat) takip dışı.

## 6.4 Doğrulanan / doğrulanmayan özeti
- DOĞRULANDI (örnek gövdeden): yukarıdaki alan adları ve türleri; zaman biçimi; `null`'ın meşru değer olarak göründüğü alanlar; oyuncuda numara olmaması; `username` ile `display_name` farkı.
- DOĞRULANMADI: kimlik doğrulama başlığı/biçimi; 401 dışındaki hata gövdeleri; 404/429 davranışı ve `Retry-After`; `/members` sayfalaması; `province_name` ve `citizenship_country_*` alanlarının **boş (null)** geldiği durumun anlamı (kayıt yok mu, kaldırıldı mı); UUID'nin gerçekten değişmez olduğu; `/api/parties/player/{numara}` gövdesi; **savaş** uç noktaları (`/api/wars`, `/api/wars/ended`, ayrıntı/hasar) gövdeleri — savaş verisi hiç görülmedi.
- Alan eşleme kararı (bot): ad=`username`; mevcut bölge=`province_name`; vatandaşlık=`citizenship_country_id`+`citizenship_country_name`.

---

# 7. Savaşlar — `/api/wars` gerçek yanıt (kullanıcı örneği, 2026-10-19)

Gövde: `{"wars": [ ... ]}`. Örnekte 19 savaş: 16 `active`, 3 `preparing`. Üst düzeyde sayfalama/toplam alanı YOK (yalnız `wars`); tamlık çapraz kontrolü için toplam sayı yok. Kısaltılmış kopya: `tests/fixtures/savaslar_ornek.json` (5 temsili savaş). Bu yanıt oturum açık sayfanın kendi isteğinden alındı (kimlik doğrulama biçimi hâlâ doğrulanmadı).

| Alan | Tür | Not |
|---|---|---|
| `id` | UUID | **Kalıcı savaş kimliği** (yeni savaş tespiti bununla) |
| `war_number` | int | Savaş numarası (ör. 5336); **ayrı alan**, kullanıcıya göre profil bağlantısı numarası. Savaş sayfasının URL biçimi DOĞRULANMADI (yalnız `/wars` listesi doğrulandı) |
| `status` | string | Görülen: `active`, `preparing` (örnekte 16/3). Başka değerler (ör. biten) görülmedi; `/api/wars/ended` gövdesi görülmedi |
| `war_type` | string | Görülen: `standard`, `rebellion`, `independence` |
| `attacker_id` | UUID \| **null** | `independence` savaşında `null` (saldıran "Bağımsız Bölge") |
| `defender_id` | UUID | `rebellion`'da `attacker_id` ile AYNI |
| `attacker_name`, `defender_name` | string | Ülke adları (Arapça/özel karakter içerebilir → HTML kaçışı şart) |
| `attacker_flag`, `defender_flag` | URL \| null | `independence`'ta saldıran bayrağı `null` |
| `attacker_province`, `defender_province` | string | Bölge adları |
| `starts_at`, `ends_at`, `created_at` | ISO-8601 UTC | `preparing` savaşta `starts_at` gelecekte (örnek: oluşturulduktan 30 dk sonra); `preparing → active` geçişi yeni savaş DEĞİL |
| `attacker_power`, `defender_power` | int | Gerçekten `0` olabilir (savaş başında). Anlamının "hasar" olduğu DOĞRULANMADI (alan adı `power`) |
| `war_name` | string \| null | Çoğunlukla `null` (örnekte yalnız 2 savaşta dolu) |
| `war_goals` | string[] | `["fetih"]` veya `[]` |
| `is_conquest`, `ended_by_ceasefire`, `is_transferred` | bool | |
| `winner_id`, `winner_name`, `winner_flag`, `end_reason`, `real_ended_at`, `ceasefire_*`, `event_*`, `name_banned_until`, `name_set_by`, `declared_by`, `declared_by_username` | çoğu null | Örnekte aktif/hazırlık savaşlarda null; takip kapsamı dışı |

Sonuçlar:
- Ayrı bir "savaş ayrıntısı" adresi görülmedi; ayrıntı alanları liste öğesinde zaten var. Ayrıntı ekranı listeyi yeniden çekerek aynı `id`yi bulabilir (bulunamazsa "aktif listede yok" denir, "silindi" DENMEZ).
- Eksik alan sıfır sayılmamalı (`null`/anahtar yok → "veri yok"); `0` meşru değerdir.
- Doğrulanmayanlar: `ended` yanıtı, diğer `status`/`war_type` değerleri, savaş sayfası URL biçimi, `power` = hasar olup olmadığı, sayfalama olasılığı, listenin tüm savaşları döndürüp döndürmediği.

`GET /api/name-styles/active` (sayfa açılışında görülen `{v, styles{UUID: stil}}`) takip kapsamı dışı; kullanılmıyor.

## 7.1 Savaş bağlantısı ve ikinci görüntü (kullanıcı doğrulaması, 2026-10-19)
- **Savaş ayrıntı bağlantısı** (kullanıcı tarayıcıda DOĞRULADI; ben bağımsız doğrulamadım): `https://diplomacia.com.tr/wars/war/{war_number}` (örnek: `/wars/war/5333`). Savaşlar veritabanında `id` (UUID) ile takip edilir; bağlantı `war_number`'dan üretilir. Kullanıcı eşlemeyi gösterdi: `war_number=5333` ↔ `id=a49da765-099f-44b8-bd51-c0f12a7ee1ac` (gerçek yanıtta eşleşiyor). `war_number` yalnız pozitif tamsayı ise bağlantı üretilir; yoksa metin gösterilir, bağlantı uydurulmaz.
- **İkinci `/api/wars` görüntüsü** (kısaltılmış kopya: `tests/fixtures/savaslar_ornek_2.json`): aynı 19 savaş. İki görüntü karşılaştırması (dakikalar arayla, kullanıcı örnekleri):
  - `id` ve `war_number` sabit kaldı (DOĞRULANDI iki anlık görüntü için).
  - 5334: ilk görüntüde `preparing`, ikincide `active`, `id` aynı → durum geçişi yeni savaş değil.
  - Güç değerleri arttı: 5333 saldıran 28552→42694, savunan 46567→265782; 5324 savunan 3581944→3687942. Alan savaş sırasında biriken bir değer gibi davranıyor; "hasar" olduğu hâlâ DOĞRULANMADI.
  - Aynı sayıda savaş (19): bu iki görüntüde yeni savaş yok.
- Hâlâ doğrulanmadı: kimlik doğrulama başlığı biçimi, `ended` yanıtı, sayfalama, diğer `status`/`war_type` değerleri.

---

# 8. Kimlik doğrulama (kullanıcı DevTools ile DOĞRULADI, 2026-10-19)

Kaynak: kullanıcı Chrome DevTools'ta başarılı bir `GET /api/players/72400` isteğini inceledi ve şunu bildirdi (ben bağımsız doğrulamadım; token değeri hiçbir yere kaydedilmedi):
- İstek başlığı: `Authorization: Bearer <JWT>` (oturum tokenı JWT biçimli).
- İstek başlığı: `X-Client-Platform: web`.
- Yanıt: HTTP 200.

Karar (bot): `Authorization` başlığını `Bearer` şemasıyla oluştur ve `X-Client-Platform: web` başlığını da gönder. Token yalnızca güvenli yerel yapılandırmadan (token dosyası) okunur; kaynağa, testlere, loglara, Git deposuna veya hafızaya yazılmaz. Kimlik bilgisi yoksa gerçek API isteği ATILMAZ. 401 → kurucuya bildirim ve gereksiz tekrar yok.

Hâlâ DOĞRULANMADI: `X-Client-Platform` başlığı olmadan isteğin başarısız olup olmadığı; 401 gövdesi; token ömrü (kullanıcı tokenın JWT olduğunu doğruladı, `exp` değeri kullanılabilir olabilir ama tek örnekten genelleme yapılmadı); 403/404/429 davranışı; `/api/wars` sayfalaması; `ended` yanıtı. Canlı bağlantı testi, kullanıcı yeni ve güvenli bir tokenı yerel ortamda sağladıktan sonra yapılacak (şu ana dek hiçbir gerçek API isteği botla atılmadı; tüm testler mock).


## 9. Büyük parti üye listesi (gerçek VPS günlükleri, 2026-10-20)
- Gerçek günlük satırları: parti 1319 → `üye sayısı uyuşmuyor (100 != 329)`, parti 5170 → `(100 != 129)`. **Kanıtlanan:** `/api/parties/{n}/members` bu partilerde tek istekte 100 kayıt döndürdü, `party.member_count` ise 329 / 129. Yanıt gövdesinde bilinen sayfalama anahtarlarından (`next`, `page`, `total`, `has_more`, `cursor`, `limit`, `offset` …) hiçbiri yoktu (aksi halde hata nedeni "sayfalama anahtarı var" olurdu; bu kontrol uyuşmazlık kontrolünden ÖNCE yapılır).
- **Kullanıcı beyanı (bağımsız doğrulanmadı):** üyeler XP'ye göre sıralı. Tutarlı tek kanıt: örnek yanıttaki iki üye azalan `xp` (2214953 ≥ 1765874). 100 üyelik gerçek bir yanıtın `xp` dizisi bu depoda YOKTUR. Bot sıralamayı varsaymaz; her üyenin XP'sini ayrı denetler.

## 9b. Üye `xp` alanı ve otomatik takip eşiği (kullanıcı kararı)
- Üye kayıtlarında `xp` (tamsayı) vardır; örnek yanıtta 2214953 ve 1765874. **Doğrulanmadı:** bunun oyuncunun toplam XP'si olduğu, 100 üyelik gerçek bir yanıttaki dağılımı, alanın bazen eksik/null/metin gelip gelmediği ve XP'nin azalıp azalmadığı.
- **Tek kriter:** `xp >= 1.500.000` (config.PARTI_XP_ESIGI; kesin karar: 1.500.000, önceki 2.000.000 değeri yanlıştı; tek sabitle değişir). Oyuncu SAYISI sınırlanmaz. Her üyenin XP'si AYRI denetlenir; eksik/geçersiz XP (alan yok, bool, metin, negatif, kesirli, NaN/inf, > 10^12) = bilinmiyor → eklenmez. Tam sayı değerli kayan nokta (1500000.0) tamsayı kabul edilir.

## 9c. Gerçek sayfalama yöntemi — ARAŞTIRMA DURUMU (kanıt bekleniyor)
- **Depoda ve günlüklerde sayfalama parametresine dair HİÇBİR kanıt yok.** `page`, `offset`, `limit`, `cursor`, `Link` başlığı gibi adaylar ÖNERİLEBİLİR ama kanıtlanmadığı için **kullanılmaz** (bot ek istek atmaz). Canlı siteye/API'ye bağlanılmadı.
- Bot artık kısmi okumada (tanılama günlüğü, `dipbot.diplomacia.istemci`, INFO) şunları yazar: gövde üst düzey anahtarları, yanıt başlık ADLARI (cookie/authorization hariç) ve sayfalama ipucu taşıyan başlıkların (`link`, `x-total-count`, `*page*`, `*range*`, `*cursor*`, `*offset*`, `*limit*`…) değerleri. Bir sonraki büyük parti taramasında `journalctl -u dipbot | grep "KISMİ okundu"` bu ipuçlarını verir.
- **Kullanıcıdan istenen gerçek Network verisi** (token/çerez PAYLAŞMADAN): tarayıcıda parti sayfasını (ör. `/party/1319`) açın → DevTools → Network → Fetch/XHR → üye listesini aşağı kaydırın / "daha fazla" düğmesine basın. (1) `/members` isteklerinin TAM URL'leri (sorgu parametreleriyle; `Authorization` başlığını SİLİN), (2) ilk ve ikinci isteğin yanıt başlıklarından `Link`, `X-Total-Count` vb. (3) yanıt gövdesinin üst düzey anahtarları (üyelerin içeriği olmadan) ve `members` uzunluğu, (4) sayfa kaç üye gösteriyor (329 mu 100 mü). Parti arayüzü üyeleri nasıl sıralıyor (XP?).
