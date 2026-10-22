#!/usr/bin/env bash
# Diplomacia GPS Botu - SQLite guvenli yedek (TASLAK, gercek sunucuda denenmedi).
# Calisan DB dosyasinin duz kopyasi KULLANILMAZ; sqlite3 ".backup" (online backup API) kullanilir.
# Token/env dosyasi (/etc/dipbot/dipbot.env) yedege KONMAZ.
#
# SAHIPLIK GUVENLIGI: canli DB'ye (ve -wal/-shm dosyalarina) YALNIZ uygulama kullanicisi (APP_USER, varsayilan dipbot)
# erisir. Betik root ile calistirilirsa sqlite3 `runuser -u APP_USER --` ile calistirilir; boylece veri dizininde
# root sahipli -wal/-shm olusmaz (servis sonradan DB'yi acamaz hale gelmezdi). Yedek, once APP_USER'a ait ozel (0700)
# gecici bir dizine yazilir ve orada dogrulanir; sonra BACKUP_DIR'e (betigi calistiranin yetkisiyle) tasinir.
# Mevcut BACKUP_DIR'in sahibi/izni DEGISTIRILMEZ; yalniz dogrulanir (sembolik baglanti degil, grup/diger yazamaz,
# sahibi root / calistiran / APP_USER). Yoksa 0700 ile olusturulur. Dosyalar 0600.
#
# Gereksinim: sqlite3 CLI (apt install sqlite3); root icin runuser (util-linux).
# Ortam degiskenleriyle degistirilebilir:
#   DB_PATH      (varsayilan /var/lib/dipbot/dipbot.db)  <- dosya adi varsayimdir, uygulamayla eslestirin
#   BACKUP_DIR   (varsayilan /var/backups/dipbot)
#   KEEP         (saklanacak son yedek sayisi, varsayilan 14)
#   APP_USER     (varsayilan dipbot)  canli DB'nin sahibi olmasi beklenen kullanici
set -euo pipefail
umask 077

DB_PATH="${DB_PATH:-/var/lib/dipbot/dipbot.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dipbot}"
KEEP="${KEEP:-14}"
APP_USER="${APP_USER:-dipbot}"

hata() { echo "HATA: $*" >&2; exit 1; }
uyari() { echo "UYARI: $*" >&2; }

command -v sqlite3 >/dev/null 2>&1 || hata "sqlite3 kurulu degil."
[[ -f "$DB_PATH" ]] || hata "veritabani bulunamadi: $DB_PATH"
[[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP >= 1 )) || hata "KEEP pozitif tamsayi olmali."

# ---- kimin yetkisiyle? -------------------------------------------------------------------------------
BEN_ID="$(id -u)"
BEN_AD="$(id -un)"
if [[ "$BEN_ID" == "0" ]]; then
  command -v runuser >/dev/null 2>&1 || hata "runuser yok (util-linux): canli DB'ye root olarak dokunulmaz."
  id -u "$APP_USER" >/dev/null 2>&1 || hata "uygulama kullanicisi yok: $APP_USER (APP_USER ile belirtin). Canli DB'ye root olarak dokunulmaz."
  APP_KOMUT=(runuser -u "$APP_USER" --)
else
  [[ "$BEN_AD" == "$APP_USER" ]] || hata "root (sudo) veya $APP_USER olarak calistirin; su an: $BEN_AD. Baska kullanici canli DB'ye dokunmamali."
  APP_KOMUT=()
fi

# ---- canli DB ve WAL/SHM sahipligi gercekten APP_USER mi? ---------------------------------------------
sahip_denetle() {
  local f="$1" s
  [[ -e "$f" ]] || return 0
  s="$(stat -c '%U' -- "$f")"
  [[ "$s" == "$APP_USER" ]] || hata "$f sahibi '$s', beklenen '$APP_USER'. Yedek alinmadi, hicbir sey degismedi. (Servisi durdurup sahipligi elle duzeltin: chown $APP_USER:$APP_USER '$f'; ya da APP_USER'i gercek servis kullanicisina ayarlayin.)"
}
sahip_denetle "$DB_PATH"
sahip_denetle "$DB_PATH-wal"
sahip_denetle "$DB_PATH-shm"

# ---- yedek dizini: DOGRULA, degistirme -----------------------------------------------------------------
if [[ -L "$BACKUP_DIR" ]]; then
  hata "BACKUP_DIR sembolik baglanti: $BACKUP_DIR (guvenlik icin reddedildi)."
elif [[ -e "$BACKUP_DIR" ]]; then
  [[ -d "$BACKUP_DIR" ]] || hata "BACKUP_DIR dizin degil: $BACKUP_DIR"
  MOD="$(stat -c '%a' -- "$BACKUP_DIR")"
  SAHIP_UID="$(stat -c '%u' -- "$BACKUP_DIR")"
  if (( (8#$MOD & 8#022) != 0 )); then
    hata "$BACKUP_DIR grup/diger kullanicilar tarafindan yazilabilir (mod $MOD); yedekler baskasi tarafindan degistirilebilir. Izni/sahipligi SIZ duzeltin (ör. chmod 700); betik mevcut dizine dokunmaz."
  fi
  APP_UID="$(id -u "$APP_USER" 2>/dev/null || echo -1)"
  if [[ "$SAHIP_UID" != "0" && "$SAHIP_UID" != "$BEN_ID" && "$SAHIP_UID" != "$APP_UID" ]]; then
    hata "$BACKUP_DIR sahibi beklenmeyen (uid $SAHIP_UID; root, calistiran veya $APP_USER olmali). Betik mevcut dizine dokunmaz."
  fi
  (( (8#$MOD & 8#007) == 0 )) || uyari "$BACKUP_DIR diger kullanicilarca okunabilir (mod $MOD); yedek dosyalari yine de 0600 olur. Izne dokunulmadi."
  [[ -w "$BACKUP_DIR" ]] || hata "$BACKUP_DIR yazilabilir degil ($BEN_AD)."
else
  mkdir -p -m 700 -- "$BACKUP_DIR"
fi

# ---- gecici (APP_USER'a ait, 0700) calisma dizini ------------------------------------------------------
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/dipbot-yedek.XXXXXX")"
TMP=""
trap 'rm -rf -- "$STAGE"; [[ -z "$TMP" ]] || rm -f -- "$TMP"' EXIT
if [[ "$BEN_ID" == "0" ]]; then
  chown "$APP_USER" -- "$STAGE"  # yalniz bu yeni gecici dizin; mevcut hicbir dizinin sahibi degismez
fi
chmod 700 -- "$STAGE"
STAGED="$STAGE/yedek.db"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/dipbot-$STAMP.db"
TMP="$FINAL.partial"

# Yazan surec varken tutarli anlik goruntu; kilitlenmeye karsi 10 sn bekleme. Kaynak SALT OKUNUR acilir.
"${APP_KOMUT[@]}" sqlite3 -readonly "$DB_PATH" ".timeout 10000" ".backup '$STAGED'" \
  || hata "sqlite3 .backup basarisiz. Yedek alinmadi."

# Yedegi dogrula (immutable: -wal/-shm olusturmaz, hicbir sey yazmaz), sonra son adina tasi.
RESULT="$("${APP_KOMUT[@]}" sqlite3 "file:$STAGED?immutable=1" 'PRAGMA integrity_check;' 2>&1 | head -n1)"
if [[ "$RESULT" != "ok" ]]; then
  echo "HATA: yedek butunluk kontrolunden gecmedi: $RESULT" >&2
  exit 1
fi

cp -- "$STAGED" "$TMP"
cmp -s -- "$STAGED" "$TMP" || hata "yedek kopyasi dogrulanamadi (cmp). Yedek alinmadi."
RESULT="$(sqlite3 "file:$TMP?immutable=1" 'PRAGMA integrity_check;' 2>&1 | head -n1)"
[[ "$RESULT" == "ok" ]] || hata "yedek hedefte butunluk kontrolunden gecmedi: $RESULT"

chmod 600 -- "$TMP"
mv -- "$TMP" "$FINAL"
TMP=""
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
