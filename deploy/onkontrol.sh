#!/usr/bin/env bash
# Diplomacia GPS Botu - sunucu on kontrolu (ag istegi YOK; gizli deger YAZDIRMAZ; dosyalari DEGISTIRMEZ).
# Servisi baslatmadan once ve her guncellemeden sonra calistirin:  sudo /opt/dipbot/deploy/onkontrol.sh
# Cikis kodu: 0 = HATA yok (UYARI olabilir), 1 = en az bir HATA.
#
# Yollari ortam degiskenleriyle degistirilebilir (test/ozel duzen icin):
#   APP_DIR (/opt/dipbot)  DATA_DIR (/var/lib/dipbot)  ENV_FILE (/etc/dipbot/dipbot.env)
#   UNIT_FILE (/etc/systemd/system/dipbot.service)  APP_USER (dipbot)  PY (<APP_DIR>/.venv/bin/python)
#   TOKEN_FILE (<DATA_DIR>/diplomacia.token)  DB_FILE (<DATA_DIR>/dipbot.db)
# Durum: bulutta yalnizca sahte dizin duzeniyle denendi; gercek Ubuntu sunucuda calistirilmadi.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/dipbot}"
DATA_DIR="${DATA_DIR:-/var/lib/dipbot}"
ENV_FILE="${ENV_FILE:-/etc/dipbot/dipbot.env}"
UNIT_FILE="${UNIT_FILE:-/etc/systemd/system/dipbot.service}"
APP_USER="${APP_USER:-dipbot}"
PY="${PY:-$APP_DIR/.venv/bin/python}"
TOKEN_FILE="${TOKEN_FILE:-$DATA_DIR/diplomacia.token}"
DB_FILE="${DB_FILE:-$DATA_DIR/dipbot.db}"
PREV_OMRU_SN=$((24 * 3600))

HATA=0
tamam()  { echo "TAMAM : $*"; }
uyari()  { echo "UYARI : $*"; }
hata()   { echo "HATA  : $*"; HATA=1; }

# stat cikti: "izin sahip:grup"
bilgi() { stat -c '%a %U:%G' -- "$1" 2>/dev/null; }

echo "== Python ve bagimliliklar"
if [[ -x "$PY" ]]; then
  if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    tamam "Python $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') (>= 3.11)"
  else
    hata "Python 3.11 veya ustu gerekli: $("$PY" --version 2>&1)"
  fi
  SURUM="$("$PY" -c 'import telegram; print(telegram.__version__)' 2>/dev/null || true)"
  if [[ "$SURUM" == "22.8" ]]; then tamam "python-telegram-bot $SURUM"
  elif [[ -n "$SURUM" ]]; then uyari "python-telegram-bot $SURUM (requirements.txt: 22.8)"
  else hata "python-telegram-bot yuklu degil (pip install -r requirements.txt)"; fi
  # Paket src/ altinda ve pip ile kurulmaz: PYTHONPATH sart. Ortam bos (env -i) -> BOT_TOKEN yok -> cikis 2 beklenir.
  env -i PYTHONPATH="$APP_DIR/src" PYTHONDONTWRITEBYTECODE=1 "$PY" -m dipbot >/dev/null 2>&1
  KOD=$?
  if [[ $KOD -eq 2 ]]; then tamam "python -m dipbot yuklenebiliyor (bos ortamda yapilandirma hatasi = cikis 2, beklenen)"
  else hata "python -m dipbot beklenmeyen cikis kodu $KOD (2 bekleniyordu; PYTHONPATH=$APP_DIR/src?)"; fi
else
  hata "venv Python bulunamadi: $PY"
fi

echo "== Ortam dosyasi (degerler yazdirilmaz)"
if [[ -f "$ENV_FILE" ]]; then
  B="$(bilgi "$ENV_FILE")"
  [[ "$B" == "600 root:root" ]] && tamam "$ENV_FILE izin/sahip: $B" || hata "$ENV_FILE izin/sahip $B (beklenen: 600 root:root)"
  anahtar_dolu() { grep -Eq "^[[:space:]]*$1=[^[:space:]]+" "$ENV_FILE"; }
  anahtar_dolu BOT_TOKEN && tamam "BOT_TOKEN tanimli" || hata "BOT_TOKEN bos/yok"
  if grep -Eq '^[[:space:]]*OWNER_TELEGRAM_ID=[1-9][0-9]*[[:space:]]*$' "$ENV_FILE"; then tamam "OWNER_TELEGRAM_ID tanimli (pozitif sayi)"
  else hata "OWNER_TELEGRAM_ID bos/yok/sayi degil"; fi
  if grep -Eq '^[[:space:]]*DB_PATH=[^[:space:]]+' "$ENV_FILE"; then
    grep -Eq "^[[:space:]]*DB_PATH=$DB_FILE[[:space:]]*$" "$ENV_FILE" \
      || uyari "DB_PATH $DB_FILE degil: yedek betikleri ve ReadWritePaths ile eslestirin"
  else
    # Varsayilan <proje>/data/dipbot.db, ProtectSystem=strict altinda salt okunurdur: servis baslamaz.
    hata "DB_PATH bos/yok: $ENV_FILE icine DB_PATH=$DB_FILE yazin (varsayilan yol servis icinde yazilamaz)"
  fi
  # .env.example'dan kalma BOS `KEY=` override satirlari (ozellikle DIPLOMACIA_AUTH_SEMA=) Bearer onekini dusurur.
  if grep -Eq '^[[:space:]]*DIPLOMACIA_AUTH_(SEMA|BASLIK)=[[:space:]]*$' "$ENV_FILE"; then
    hata "DIPLOMACIA_AUTH_SEMA=/DIPLOMACIA_AUTH_BASLIK= BOS tanimli: satirlari silin veya yorum yapin (bos sema 'Bearer' onekini dusurur -> 401)"
  fi
  if grep -Ei '^[[:space:]]*(DIPLOMACIA_TOKEN|JWT)[A-Z_]*=[^[:space:]]' "$ENV_FILE" | grep -Eiv '^[[:space:]]*DIPLOMACIA_TOKEN_DOSYASI=' | grep -q .; then
    hata "env dosyasinda Diplomacia tokeni gibi bir deger var: token yalniz token dosyasinda tutulmali"
  fi
else
  hata "ortam dosyasi yok: $ENV_FILE"
fi

echo "== Veri dizini ve dosyalar"
if [[ -d "$DATA_DIR" ]]; then
  B="$(bilgi "$DATA_DIR")"
  case "$B" in
    "750 $APP_USER:$APP_USER"|"700 $APP_USER:$APP_USER") tamam "$DATA_DIR izin/sahip: $B" ;;
    *) hata "$DATA_DIR izin/sahip $B (beklenen: 750 veya 700 $APP_USER:$APP_USER)" ;;
  esac
else
  hata "veri dizini yok: $DATA_DIR"
fi
if [[ -f "$DB_FILE" ]]; then
  B="$(bilgi "$DB_FILE")"
  [[ "$B" == "600 $APP_USER:$APP_USER" ]] && tamam "DB izin/sahip: $B" || uyari "DB izin/sahip $B (beklenen: 600 $APP_USER:$APP_USER)"
  # WAL modunda okuma bile -shm/-wal dosyasi olusturabilir: root olarak acmak onlari root sahipli birakir ve
  # servis (dipbot) DB'yi acamaz. Bu yuzden kontrol HER ZAMAN servis kullanicisi olarak yapilir.
  if ! command -v sqlite3 >/dev/null 2>&1; then
    uyari "sqlite3 yok: DB butunluk kontrolu atlandi"
  elif [[ $EUID -eq 0 ]] && { ! command -v runuser >/dev/null 2>&1 || ! id -u "$APP_USER" >/dev/null 2>&1; }; then
    uyari "root olarak calisiyorsunuz ve runuser/$APP_USER yok: DB butunluk kontrolu atlandi (root sahipli -wal/-shm riski)"
  else
    if [[ $EUID -eq 0 ]]; then S="$(runuser -u "$APP_USER" -- sqlite3 -readonly "$DB_FILE" 'PRAGMA integrity_check;' 2>&1 | head -n1)"
    else S="$(sqlite3 -readonly "$DB_FILE" 'PRAGMA integrity_check;' 2>&1 | head -n1)"; fi
    [[ "$S" == "ok" ]] && tamam "DB butunluk kontrolu: ok" || hata "DB butunluk kontrolu: $S"
  fi
  # root sahipli -wal/-shm (ör. DB'ye root olarak sqlite3 ile dokunulduysa) servisin DB'yi acmasini engeller.
  for ek in -wal -shm; do
    if [[ -e "$DB_FILE$ek" ]]; then
      B="$(bilgi "$DB_FILE$ek")"
      case "$B" in
        *" $APP_USER:$APP_USER") tamam "$DB_FILE$ek sahibi uygun: $B" ;;
        *) hata "$DB_FILE$ek sahibi/izni $B (beklenen sahip $APP_USER:$APP_USER). Servisi durdurup duzeltin: chown $APP_USER:$APP_USER '$DB_FILE$ek'. Canli DB'ye root olarak sqlite3 ile dokunmayin." ;;
      esac
    fi
  done
else
  uyari "DB henuz yok ($DB_FILE): ilk calistirmada olusur"
fi
if [[ -f "$TOKEN_FILE" ]]; then
  B="$(bilgi "$TOKEN_FILE")"
  [[ "$B" == "600 $APP_USER:$APP_USER" ]] && tamam "token dosyasi izin/sahip: $B" || hata "token dosyasi izin/sahip $B (beklenen: 600 $APP_USER:$APP_USER)"
else
  uyari "token dosyasi yok ($TOKEN_FILE): kurucu menuden verene kadar Diplomacia istegi atilmaz"
fi
PREV="$TOKEN_FILE.prev"
if [[ -f "$PREV" ]]; then
  B="$(bilgi "$PREV")"
  [[ "$B" == "600 $APP_USER:$APP_USER" ]] || hata ".prev izin/sahip $B (beklenen: 600 $APP_USER:$APP_USER)"
  YAS=$(( $(date +%s) - $(stat -c '%Y' -- "$PREV") ))
  if (( YAS > PREV_OMRU_SN )); then uyari ".prev yasi ${YAS} sn > 24 sa: bot calisiyorsa kendiliginden silinir; calismiyorsa elle silin"
  else tamam ".prev mevcut (yas ${YAS} sn); otomatik silinir (ilk basarili tarama veya 24 sa)"; fi
fi
if [[ -e "$DATA_DIR" ]] && compgen -G "$DATA_DIR/.token-*" >/dev/null; then
  uyari "yarim kalmis gecici token dosyasi var ($DATA_DIR/.token-*): bot acilista temizler"
fi

echo "== systemd ve saat"
if [[ -f "$UNIT_FILE" ]]; then
  grep -q "^Environment=PYTHONPATH=$APP_DIR/src" "$UNIT_FILE" && tamam "unit PYTHONPATH=$APP_DIR/src" || hata "unit dosyasinda Environment=PYTHONPATH=$APP_DIR/src yok"
  grep -q "^ReadWritePaths=$DATA_DIR" "$UNIT_FILE" && tamam "unit ReadWritePaths=$DATA_DIR" || hata "unit ReadWritePaths=$DATA_DIR icermiyor"
  if command -v systemd-analyze >/dev/null 2>&1; then
    if systemd-analyze verify "$UNIT_FILE" >/dev/null 2>&1; then tamam "systemd-analyze verify temiz"
    else uyari "systemd-analyze verify uyari/hata verdi: elle calistirip cikti inceleyin"; fi
  else
    uyari "systemd-analyze yok: unit dogrulamasi atlandi"
  fi
else
  hata "unit dosyasi yok: $UNIT_FILE"
fi
if command -v timedatectl >/dev/null 2>&1; then
  N="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
  [[ "$N" == "yes" ]] && tamam "saat NTP ile senkron" || uyari "saat NTP senkronu dogrulanamadi ($N): bekleyen onay guvenligi saat atlamalarina duyarlidir"
fi

echo
if (( HATA )); then echo "SONUC: HATA var; duzeltmeden servisi baslatmayin."; exit 1; fi
echo "SONUC: HATA yok (UYARI'lari inceleyin)."
