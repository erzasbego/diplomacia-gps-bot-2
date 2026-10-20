#!/usr/bin/env bash
# Diplomacia GPS Botu - yedekten geri yukleme (TASLAK, sunucuda denenmedi).
# Kullanim: sudo ./restore.sh /var/backups/dipbot/dipbot-YYYYMMDDTHHMMSSZ.db
# Ortam: DB_PATH (varsayilan /var/lib/dipbot/dipbot.db), SERVICE (varsayilan dipbot),
#        APP_USER (varsayilan dipbot), SAFETY_DIR (varsayilan /var/backups/dipbot)
set -euo pipefail
umask 077

DB_PATH="${DB_PATH:-/var/lib/dipbot/dipbot.db}"
SERVICE="${SERVICE:-dipbot}"
APP_USER="${APP_USER:-dipbot}"
SAFETY_DIR="${SAFETY_DIR:-/var/backups/dipbot}"

[[ $# -eq 1 ]] || { echo "Kullanim: $0 <yedek_dosyasi.db>" >&2; exit 2; }
BACKUP="$1"

[[ $EUID -eq 0 ]] || { echo "HATA: root olarak calistirin (sudo)." >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "HATA: sqlite3 kurulu degil." >&2; exit 1; }
[[ -f "$BACKUP" ]] || { echo "HATA: yedek dosyasi yok: $BACKUP" >&2; exit 1; }

RESULT="$(sqlite3 "$BACKUP" 'PRAGMA integrity_check;')"
[[ "$RESULT" == "ok" ]] || { echo "HATA: yedek bozuk: $RESULT" >&2; exit 1; }

echo "Geri yuklenecek yedek : $BACKUP"
echo "Hedef veritabani      : $DB_PATH"
echo "Servis '$SERVICE' durdurulacak, mevcut DB guvenlik yedegine alinacak ve ustune yazilacak."
read -r -p "Devam etmek icin 'EVET' yazin: " ANSWER
[[ "$ANSWER" == "EVET" ]] || { echo "Iptal edildi."; exit 1; }

WAS_ACTIVE=0
if systemctl is-active --quiet "$SERVICE"; then WAS_ACTIVE=1; fi

echo "Servis durduruluyor..."
systemctl stop "$SERVICE"

# Mevcut DB'yi (varsa) geri yuklemeden once guvenlik yedegine al; servis durdu, .backup guvenli.
if [[ -f "$DB_PATH" ]]; then
  mkdir -p "$SAFETY_DIR"
  chmod 700 "$SAFETY_DIR"
  SAFETY="$SAFETY_DIR/dipbot-pre-restore-$(date -u +%Y%m%dT%H%M%SZ).db"
  sqlite3 "$DB_PATH" ".backup '$SAFETY'"
  chmod 600 "$SAFETY"
  echo "Mevcut DB yedeklendi: $SAFETY"
fi

# Yedegi hedef dizinde gecici dosyaya kopyala, sonra atomik yer degistir.
TMP="$DB_PATH.restoring"
trap 'rm -f "$TMP"' EXIT
cp -- "$BACKUP" "$TMP"
chown "$APP_USER:$APP_USER" "$TMP"
chmod 600 "$TMP"
# Eski WAL/SHM artiklari yeni DB ile karisip bozulmaya yol acmasin.
rm -f -- "$DB_PATH-wal" "$DB_PATH-shm"
mv -f -- "$TMP" "$DB_PATH"
trap - EXIT

chown "$APP_USER:$APP_USER" "$DB_PATH"
chmod 600 "$DB_PATH"
echo "Geri yukleme tamamlandi."

if (( WAS_ACTIVE )); then
  echo "Servis baslatiliyor..."
  systemctl start "$SERVICE"
  systemctl --no-pager --lines=5 status "$SERVICE" || true
else
  echo "Servis onceden calismiyordu; baslatilmadi. Gerekirse: systemctl start $SERVICE"
fi
