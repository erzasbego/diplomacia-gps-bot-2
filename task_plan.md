# Görev planı (kalıcı kaynak) — BULUT GEÇİŞ KONTROL NOKTASI 2026-10-20

Şartname: dp.txt. Bulgular: findings.md, API-ARASTIRMA.md. Devam sırası: DEVAM.md ve BULUT_DEVAM.md (bulut için ana giriş). Geçmiş: progress.md.

- [x] 1. Site/API araştırması — oyuncu/parti/üyeler/savaş şemaları ve kimlik başlığı KULLANICI örnekleriyle işlendi (API-ARASTIRMA §6-§8). Canlı doğrulama YOK.
- [x] 2. Çekirdek (SQLite, yetki, takip, outbox, zamanlayıcı) — mock testli
- [x] 3. Telegram arayüzü (komut+buton, token menüsü, savaş ekranı+bağlantı, denetim bildirimi, /grup_kaldir yalnız kurucu, /grup_tasi) — mock testli, canlı Telegram YOK
- [x] 4. Gerçek API adaptörü — Bearer+X-Client-Platform varsayılan (kullanıcı DevTools'ta doğruladı), oyuncu/parti/savaş; mock testli. CANLI BAĞLANTI TEST EDİLMEDİ.
- [x] 5. Zamanlayıcı/hata sınıfları/geri çekilme/haftalık token hatırlatması — mock testli
- [x] 6a. QA tur 1+2+doğrulama ve Opus (2 inceleme) bulgularının düzeltmeleri: Tur A, B, C, E, F1, F2a UYGULANDI
- [~] 6b. Dağıtım (systemd, backup/restore, .env.example, deploy README) — taslak; sunucuda DOĞRULANMADI
- [ ] 7. Tur F2b: D7 saat güvenliği (monotonic TTL, onay_donemi sayacı) + .prev silme politikası
- [ ] 8. Bağımsız doğrulama: qa + dar kapsamlı Opus — F1, F2a (D9), F2b için (henüz hiçbir bağımsız inceleme bu üç turu görmedi)
- [ ] 9. Kök README.md (Türkçe) — YOK
- [ ] 10. Canlı doğrulama (kullanıcı güvenli token'ı yerel ortamda sağlayınca ve izin verince)

Test durumu (ana oturum, 2026-10-20): 631 passed, 0 failed (MOCK).
Kural: canlı entegrasyon doğrulanmadan "çalışıyor" denmez; mock testler ayrı raporlanır.
