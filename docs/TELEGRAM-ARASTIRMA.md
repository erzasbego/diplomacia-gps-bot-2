# Telegram araştırması

Tarih: 2026-10-19. Hazırlayan: `arastirmaci` (Haiku). Dosyaya ana oturum aktardı (araştırma ajanının yazması kancayla engellendi). **Ana oturum kaynakları ayrıca kontrol etmedi**; aşağıdaki "doğrulandı" ifadesi ajanın resmî kaynakta gördüğü anlamına gelir. Geliştirici kritik maddeleri (sürüm, sınırlar) kurulumda yeniden doğrulamalı.

Etiket: [D] resmî kaynakta doğrulandı, [K] topluluk/üçüncü taraf, [X] doğrulanamadı.
Kaynaklar: API https://core.telegram.org/bots/api · FAQ https://core.telegram.org/bots/faq · PTB-APP https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html · PTB-ERR https://docs.python-telegram-bot.org/en/stable/telegram.error.html · PTB-PYPI https://pypi.org/project/python-telegram-bot/ · AIOGRAM https://pypi.org/project/aiogram/

## 1. Kütüphane
- python-telegram-bot (PTB) son kararlı 22.8 (12 Haziran 2026), Bot API 10.0, `requires_python >=3.10` [D] PTB-PYPI. Python 3.11 kapsamda; ayrı sınıflandırıcı kontrolü yapılmadı.
- `run_polling()`: başlatma ve kapanışı yönetir. Unix'te varsayılan durdurma sinyalleri SIGINT/SIGTERM/SIGABRT; Windows ProactorEventLoop'ta sinyal ayarlanamaz. `drop_pending_updates` varsayılan False, `bootstrap_retries` varsayılan 0 [D] PTB-APP.
- `shutdown()` `post_shutdown` çağırmaz (yalnız run_polling/run_webhook çağırır) [D].
- JobQueue isteğe bağlı (`[job-queue]` extra, APScheduler) [D]. JobQueue zorunluluğu [X].
- aiogram 3.31.0 (26 Ağustos 2026), Python >=3.10,<3.15 [D]. FSM/kapanış ayrıntıları [X].
- Öneri (çıkarım): PTB 22.8 + `run_polling`; JobQueue zorunlu değil, düz asyncio görevi yeterli.

## 2. Callback
- `callback_data` 1-64 **bayt** (Türkçe karakter 2 bayt) [D].
- `answerCallbackQuery` çağrılana kadar istemcide ilerleme göstergesi sürer; `text` en fazla 200 karakter [D].
- Buton ömrü için resmî süre sınırı [X].
- Öneri: `callback_data`'da kısa anahtar, asıl veri SQLite'ta; her tıklamada sunucuda yetki ve grup doğrulaması.

## 3. Biçimlendirme ve uzunluk
- HTML: `<`, `>`, `&` için `&lt; &gt; &amp;` [D]. Desteklenen etiketler: b/strong, a href, code, pre, tg-spoiler [D].
- MarkdownV2 kaçış listesi API'de var; ters eğik çizgi ayrıntısı [X].
- Öneri: HTML + `html.escape` (href için `quote=True`). Python `html` davranışı ayrıca doğrulanmadı [X].
- Mesaj metni 1-4096 karakter (entity ayrıştırma sonrası) [D]. Öneri: ham düz metinde böl, hedef ~3500, her parçayı ayrı biçimlendir.

## 4. Hız sınırı ve hatalar
- Tek sohbet saniyede ~1 mesaj, grup dakikada 20 mesaj, toplu ~30 mesaj/sn; aşımda 429 [D] FAQ.
- `retry_after` beklenecek saniyeyi verir [D]. PTB `RetryAfter` int veya timedelta döner [D].
- `NetworkError` taban sınıf; `TimedOut` ve `BadRequest` alt sınıfı [D]. `Forbidden` [D].
- "bot was blocked / kicked", "chat not found" tam metinleri resmî kaynakta yok [X]; üçüncü taraf kaynaklar çelişiyor.
- Sınıflandırma önerisi: TimedOut/NetworkError → geri çekilme; RetryAfter → bekle; Forbidden → sohbeti pasifle; ChatMigrated → kimlik güncelle; InvalidToken → fatal; Conflict → yeniden deneme çözmez; BadRequest → içerik hatası, logla. Metin eşleşmesine değil hata sınıfına dayan.

## 5. Grup / özel sohbet
- `Chat.type`: private, group, supergroup, channel [D].
- `my_chat_member`: botun kendi üyelik değişimi [D]. Varsayılan `allowed_updates` bunu içerir; açıkça verilirse eklenmeli. `ChatMember.status`: creator, administrator, member, restricted, left, kicked [D].
- Onaysız ekleme tespiti (öneri): eski durum left/kicked, yeni durum member/administrator; ekleyeni `from` ile denetle.
- Grup kimliği negatif; supergroup `-100` öneki resmî sayfada açık değil [K]. 64-bit tam sayı sakla.
- Migration: `migrate_to_chat_id` / `ChatMigrated(new_chat_id)` [D]. Onaylı kayıt eski kimlikte kalırsa yeni kimlik onaysız görünür; kaydı yeni kimliğe taşı ve yetkiyi yeniden doğrula.

## 6. Komut ve gizlilik modu
- `BotCommand.command`: 1-32 karakter, yalnız küçük İngilizce harf, rakam, alt çizgi [D] → **Türkçe karakter ve büyük harf geçersiz**. Açıklama 1-256 karakter; en fazla 100 komut [D].
- Gruplarda `/komut@botadi` biçimi [D].
- Gizlilik modu açıkken bot yalnız kendine yönelik komutları ve ona yanıtları alır; yönetici botlar tüm mesajları alır [D]. `getMe.can_read_all_group_messages` [D].
- Gizlilik değişiminde grubu yeniden ekleme gerekliliği [X]. Varsayılan durum FAQ'ta açık değil [X].

## 7. Conflict
- Aynı token ile ikinci `getUpdates` çakışır (HTTP 409, "terminated by other getUpdates request") [K]. PTB `Conflict` hatası [D]. Webhook açıkken `getUpdates` çalışmaz [D].
- Güncellemeler 24 saatten uzun tutulmaz [D].
- Öneri: tek systemd birimi/süreç kilidi; Conflict'te açık hatayla dur.

## Doğrulanamayanlar (varsayım yapma)
Kullanıcı başlatmadan özel mesaj; "blocked/kicked/chat not found" metinleri; gizlilik değişimi sonrası yeniden ekleme; BotFather adımları; supergroup `-100` resmî kuralı; JobQueue zorunluluğu; `html.escape`/MarkdownV2 ayrıntısı; aiogram FSM/kapanış; `leaveChat`.

## Geliştiriciye öneriler
1. PTB 22.8 + `run_polling()`.
2. JobQueue zorunlu değil; periyodik tarama için asyncio görevi.
3. Kapanışta kendi görevlerini iptal et; Windows'ta Ctrl+C ile test et.
4. `callback_data` kısa anahtar (<64 bayt), asıl veri DB'de, her tıklamada yetki doğrula, her callback'te `answerCallbackQuery`.
5. HTML + `html.escape`; ham metinde böl, ~3500.
6. Hız: tek sohbet 1 msg/sn, grup 20 msg/dk; `retry_after` kadar bekle; ağ hatalarında üstel geri çekilme.
7. Hata sınıfına göre karar ver.
8. `my_chat_member` ile onaysız ekleme tespiti; 64-bit kimlik; migration'da yeniden yetkilendir.
9. Komutlar `[a-z0-9_]{1,32}`; Türkçe açıklama `description`'da; `/komut@botadi` destekle.
10. Gizlilik modu ve yeniden ekleme davranışını gerçek grupta canlı test et; açılışta `can_read_all_group_messages` kontrol et; tek instance ve webhook kapalılığını denetle.
