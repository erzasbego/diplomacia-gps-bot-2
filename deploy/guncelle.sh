#!/usr/bin/env bash
# Diplomacia GPS Botu - GUVENLI GUNCELLEME (VPS'te kullanici tarafindan, root ile calistirilir).
# Sira ZORUNLUDUR: (1) SQLite yedegi alinip dogrulanir  (2) sonra kod guncellenir  (3) on kontrol  (4) servis yeniden baslar.
# Yedek alinamaz/dogrulanamazsa HICBIR sey degismez (kod, veritabani, servis oldugu gibi kalir).
# Veritabanina bu betik YAZMAZ; yalnizca okur (yedek). Canli DB'ye root dokunmaz: backup.sh sqlite3'u APP_USER
# (dipbot) olarak calistirir (root sahipli -wal/-shm olusmaz). Git islemleri deponun SAHIBI kullanici olarak yapilir
# (root'un SSH deploy anahtari / git safe.directory ayari varsayilmaz). Sema gecisi gerekiyorsa uygulamanin ilk
# acilisinda otomatik yapilir ve mevcut kayitlari korur (bkz. deploy/GUNCELLEME-PLANI.md: geri donus plani).
#
# Kullanim:  sudo ./guncelle.sh [REF]      REF = dal/etiket/commit (varsayilan: main). Yalniz ileri sarma (ff-only).
# Ortam (hepsi isteğe bagli; test icin de kullanilir):
#   APP_DIR=/opt/dipbot  DB_PATH=/var/lib/dipbot/dipbot.db  BACKUP_DIR=/var/backups/dipbot  SERVICE=dipbot
#   APP_USER=dipbot (canli DB sahibi)  GIT_USER=<APP_DIR/.git sahibi, otomatik> (git bu kullanici olarak calisir)
#   BACKUP_SCRIPT=<APP_DIR>/deploy/backup.sh  ONKONTROL=<APP_DIR>/deploy/onkontrol.sh
#   PIP_KOMUTU="<APP_DIR>/.venv/bin/python -m pip"  SYSTEMCTL=systemctl  BEKLE_SN=5  REMOTE=origin
# Durum: bulutta yalniz sahte dizin/sahte systemctl/yerel git deposuyla denendi; GERCEK sunucuda calistirilmadi.
set -euo pipefail
umask 077

REF="${1:-main}"
APP_DIR="${APP_DIR:-/opt/dipbot}"
DB_PATH="${DB_PATH:-/var/lib/dipbot/dipbot.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dipbot}"
SERVICE="${SERVICE:-dipbot}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-$APP_DIR/deploy/backup.sh}"
ONKONTROL="${ONKONTROL:-$APP_DIR/deploy/onkontrol.sh}"
PIP_KOMUTU="${PIP_KOMUTU:-$APP_DIR/.venv/bin/python -m pip}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
BEKLE_SN="${BEKLE_SN:-5}"
REMOTE="${REMOTE:-origin}"
APP_USER="${APP_USER:-dipbot}"

hata() { echo "HATA: $*" >&2; exit 1; }
bilgi() { echo "[guncelle] $*"; }

BEN_ID="$(id -u)"
[[ "$BEN_ID" == "0" || "${GUNCELLE_ROOT_ZORUNLU:-1}" == "0" ]] || hata "root olarak calistirin (sudo)."
command -v git >/dev/null 2>&1 || hata "git kurulu degil."
command -v sqlite3 >/dev/null 2>&1 || hata "sqlite3 kurulu degil (yedek dogrulamasi icin gerekli)."
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || hata "gecersiz REF."
[[ -d "$APP_DIR/.git" ]] || hata "$APP_DIR bir git deposu degil."

# ---- git'i kim calistiracak? Deponun sahibi (root'un deploy anahtari/safe.directory ayari varsayilmaz) --------
GIT_USER="${GIT_USER:-$(stat -c '%U' "$APP_DIR/.git")}"
if [[ "$BEN_ID" == "0" && "$GIT_USER" != "root" ]]; then
  command -v runuser >/dev/null 2>&1 || hata "runuser yok: git, deponun sahibi ($GIT_USER) olarak calistirilamaz. Elle guncelleme yolunu kullanin (GUNCELLEME-PLANI.md)."
  id -u "$GIT_USER" >/dev/null 2>&1 || hata "git kullanicisi yok: $GIT_USER (GIT_USER ile belirtin)."
  GIT_HOME="$(getent passwd "$GIT_USER" | cut -d: -f6)"
  GIT=(runuser -u "$GIT_USER" -- env HOME="$GIT_HOME" GIT_TERMINAL_PROMPT=0 git -C "$APP_DIR")
else
  GIT=(env GIT_TERMINAL_PROMPT=0 git -C "$APP_DIR")
fi
[[ -f "$DB_PATH" ]] || hata "veritabani yok: $DB_PATH (yedeklenecek veri bulunamadi; yanlis DB_PATH olabilir). Hicbir sey degismedi."

ONCEKI_REV="$("${GIT[@]}" rev-parse HEAD)"
bilgi "mevcut surum: $ONCEKI_REV"
"${GIT[@]}" diff --quiet && "${GIT[@]}" diff --cached --quiet \
  || hata "$APP_DIR icinde yerel degisiklik var; once temizleyin. Hicbir sey degismedi."

# ---- 1) YEDEK (zorunlu) ------------------------------------------------------------------------------
bilgi "1/4 SQLite yedegi aliniyor..."
CIKTI="$(DB_PATH="$DB_PATH" BACKUP_DIR="$BACKUP_DIR" APP_USER="$APP_USER" bash "$BACKUP_SCRIPT" 2>&1)" || { echo "$CIKTI" >&2; hata "yedek alinamadi. Hicbir sey degismedi (kod, veritabani, servis)."; }
echo "$CIKTI"
YEDEK="$(printf '%s\n' "$CIKTI" | sed -n 's/^Yedek alindi: //p' | tail -n1)"
[[ -n "$YEDEK" && -f "$YEDEK" ]] || hata "yedek dosyasi bulunamadi. Hicbir sey degismedi."
[[ "$(sqlite3 "file:$YEDEK?immutable=1" 'PRAGMA integrity_check;' | head -n1)" == "ok" ]] || hata "yedek butunluk kontrolunden gecmedi. Hicbir sey degismedi."
SEMA_ONCE="$(sqlite3 "file:$YEDEK?immutable=1" 'PRAGMA user_version;')"
KAYIT="$BACKUP_DIR/guncelleme-$(date -u +%Y%m%dT%H%M%SZ).txt"
{
  echo "onceki_surum=$ONCEKI_REV"
  echo "yedek=$YEDEK"
  echo "sema_surumu_onceki=$SEMA_ONCE"
  echo "hedef_ref=$REF"
} > "$KAYIT"
chmod 600 "$KAYIT"
bilgi "yedek dogrulandi: $YEDEK (sema surumu $SEMA_ONCE); kayit: $KAYIT"

# ---- 2) KOD (yalniz ileri sarma) ---------------------------------------------------------------------
bilgi "2/4 kod guncelleniyor ($REMOTE/$REF, ff-only)..."
"${GIT[@]}" fetch --quiet "$REMOTE" "$REF" || hata "fetch basarisiz ($GIT_USER olarak). Kod degismedi; yedek hazir: $YEDEK. Elle yol: GUNCELLEME-PLANI.md"
HEDEF_REV="$("${GIT[@]}" rev-parse FETCH_HEAD)"
"${GIT[@]}" merge-base --is-ancestor "$ONCEKI_REV" "$HEDEF_REV" \
  || hata "hedef surum mevcut surumun ileri sarmasi degil (ff-only). Kod degismedi; yedek hazir: $YEDEK"
"${GIT[@]}" merge --ff-only --quiet "$HEDEF_REV" || hata "ff-only birlestirme basarisiz. Yedek hazir: $YEDEK"
bilgi "yeni surum: $HEDEF_REV"

geri_donus() {
  cat >&2 <<GERI

--- GERI DONUS PLANI --------------------------------------------------------------------------------
1) Kodu eski surume alin:   (depo sahibi olarak) git -C $APP_DIR checkout --detach $ONCEKI_REV
2) Servisi yeniden baslatin: $SYSTEMCTL restart $SERVICE
3) Sema surumu degistiyse ve eski kod veritabanini acamiyorsa yedekten geri yukleyin:
     /usr/local/sbin/dipbot-restore $YEDEK        (yedek tarihinden sonraki veriler kaybolur)
Yedek: $YEDEK    Kayit: $KAYIT
-----------------------------------------------------------------------------------------------------
GERI
}

# ---- 3) BAGIMLILIKLAR + ON KONTROL -------------------------------------------------------------------
bilgi "3/4 bagimliliklar ve on kontrol..."
# shellcheck disable=SC2086
(cd "$APP_DIR" && $PIP_KOMUTU install -r requirements.txt) || { geri_donus; hata "bagimlilik kurulumu basarisiz. Servis yeniden BASLATILMADI."; }
bash "$ONKONTROL" || { geri_donus; hata "on kontrol basarisiz. Servis yeniden BASLATILMADI."; }

# ---- 4) SERVIS ---------------------------------------------------------------------------------------
bilgi "4/4 servis yeniden baslatiliyor..."
"$SYSTEMCTL" restart "$SERVICE" || { geri_donus; hata "servis baslatilamadi."; }
sleep "$BEKLE_SN"
"$SYSTEMCTL" is-active --quiet "$SERVICE" || { geri_donus; hata "servis calismiyor (journalctl -u $SERVICE -n 50 --utc)."; }
bilgi "TAMAM: $SERVICE calisiyor. Kontrol: journalctl -u $SERVICE -n 30 --utc"
bilgi "Mevcut grup onaylari, takip edilen oyuncular ve partiler veritabaninda korunur (yedek: $YEDEK)."
