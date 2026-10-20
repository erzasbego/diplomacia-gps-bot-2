#!/usr/bin/env bash
# Diplomacia GPS Botu - SQLite guvenli yedek (TASLAK, sunucuda denenmedi).
# Calisan DB dosyasinin duz kopyasi KULLANILMAZ; sqlite3 ".backup" (online backup API) kullanilir.
# Token/env dosyasi (/etc/dipbot/dipbot.env) yedege KONMAZ.
#
# Gereksinim: sqlite3 CLI (apt install sqlite3).
# Ortam degiskenleriyle degistirilebilir:
#   DB_PATH      (varsayilan /var/lib/dipbot/dipbot.db)  <- dosya adi varsayimdir, uygulamayla eslestirin
#   BACKUP_DIR   (varsayilan /var/backups/dipbot)
#   KEEP         (saklanacak son yedek sayisi, varsayilan 14)
set -euo pipefail
umask 077

DB_PATH="${DB_PATH:-/var/lib/dipbot/dipbot.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dipbot}"
KEEP="${KEEP:-14}"

command -v sqlite3 >/dev/null 2>&1 || { echo "HATA: sqlite3 kurulu degil." >&2; exit 1; }
[[ -f "$DB_PATH" ]] || { echo "HATA: veritabani bulunamadi: $DB_PATH" >&2; exit 1; }
[[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP >= 1 )) || { echo "HATA: KEEP pozitif tamsayi olmali." >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/dipbot-$STAMP.db"
TMP="$FINAL.partial"
trap 'rm -f "$TMP"' EXIT

# Yazan surec varken tutarli anlik goruntu; kilitlenmeye karsi 10 sn bekleme.
sqlite3 "$DB_PATH" ".timeout 10000" ".backup '$TMP'"

# Yedegi dogrula, sonra son adina tasi (yarim yedek hic gorunmesin).
RESULT="$(sqlite3 "$TMP" 'PRAGMA integrity_check;')"
if [[ "$RESULT" != "ok" ]]; then
  echo "HATA: yedek butunluk kontrolunden gecmedi: $RESULT" >&2
  exit 1
fi

chmod 600 "$TMP"
mv "$TMP" "$FINAL"
trap - EXIT
echo "Yedek alindi: $FINAL"

# Eski yedek temizligi: yalniz bu desene uyan dosyalardan son KEEP adedi kalir.
# (Zaman damgasi ada gomulu oldugundan ada gore siralama = zamana gore siralama.)
mapfile -t ALL < <(ls -1 "$BACKUP_DIR"/dipbot-*.db 2>/dev/null | sort)
COUNT="${#ALL[@]}"
if (( COUNT > KEEP )); then
  for f in "${ALL[@]:0:COUNT-KEEP}"; do
    rm -f -- "$f"
    echo "Eski yedek silindi: $f"
  done
fi
