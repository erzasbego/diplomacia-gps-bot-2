"""Telegram arayüz katmanı (python-telegram-bot, long polling).

Mimari: `arayuz.Arayuz` Telegram'dan BAĞIMSIZ mantıktır (Olay -> Yanit); `uygulama` PTB
Update nesnelerini Olay'a çevirir; `gonderici` mesaj gönderimi, bölme, hız sınırı ve hata
sınıflandırması yapar. Bu paket testlerde sahte Bot/Update ile denenir; canlı Telegram
doğrulaması DEĞİLDİR.
"""
