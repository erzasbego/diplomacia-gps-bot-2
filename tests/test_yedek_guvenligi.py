"""deploy/backup.sh, guncelle.sh, onkontrol.sh, restore.sh: canlı SQLite'a yalnız uygulama kullanıcısı (dipbot) dokunur.

Gerçek sunucu/servis yok. Root davranışı PATH'e konan SAHTE `id` (uid 0 bildirir) ve SAHTE `runuser` (komutu kaydedip
çalıştırır, ortama RUNUSER_ACTIVE=1 koyar) ile sınanır; böylece testler root olmadan da çalışır. Sahte `sqlite3` sarmalayıcı,
canlı DB'ye yapılan HER çağrının runuser altında olup olmadığını kaydeder. Gerçek sahiplik/izin geçişleri (chown) GERÇEK
sunucuda doğrulanmadı."""
import os
import shutil
import sqlite3
import subprocess

import pytest

KOK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP = os.path.join(KOK, "deploy", "backup.sh")
GUNCELLE = os.path.join(KOK, "deploy", "guncelle.sh")
ONKONTROL = os.path.join(KOK, "deploy", "onkontrol.sh")
RESTORE = os.path.join(KOK, "deploy", "restore.sh")

pytestmark = pytest.mark.skipif(
    not (shutil.which("sqlite3") and shutil.which("git") and shutil.which("bash")),
    reason="sqlite3/git/bash gerekli",
)
GERCEK_SQLITE = shutil.which("sqlite3")
BEN = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _db_olustur(yol, n=5):
    c = sqlite3.connect(yol)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE onayli_gruplar(group_id INTEGER PRIMARY KEY, donem INTEGER)")
    c.execute("CREATE TABLE takip_oyuncular(group_id INTEGER, oyuncu_id TEXT)")
    c.execute("INSERT INTO onayli_gruplar VALUES(-1004375821471, 1)")
    c.execute("INSERT INTO onayli_gruplar VALUES(-1004485206993, 1)")
    c.executemany("INSERT INTO takip_oyuncular VALUES(-1004375821471, ?)", [(str(i),) for i in range(n)])
    c.execute("PRAGMA user_version=6")
    c.commit()
    c.close()


def _ozet(db):
    c = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        return [list(c.execute("SELECT * FROM onayli_gruplar")), list(c.execute("SELECT * FROM takip_oyuncular ORDER BY 2"))]
    finally:
        c.close()


@pytest.fixture
def ortam(tmp_path):
    veri = tmp_path / "data"
    veri.mkdir()
    db = veri / "dipbot.db"
    _db_olustur(db)
    stub = tmp_path / "stub"
    stub.mkdir()
    olay = tmp_path / "olay.log"
    # uid 0 bildiren sahte id; diğer kullanımlar gerçeğe gider
    (stub / "id").write_text('#!/bin/bash\n[[ "$*" == "-u" ]] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n')
    # runuser -u KULLANICI -- KOMUT...: kaydet, RUNUSER_ACTIVE=1 ile çalıştır
    (stub / "runuser").write_text(
        '#!/bin/bash\n[[ "$1" == "-u" ]] || exit 99\nK="$2"; shift 2; [[ "$1" == "--" ]] && shift\n'
        f'echo "runuser -u $K -- $*" >> "{olay}"\nRUNUSER_ACTIVE=1 exec "$@"\n')
    # sqlite3 sarmalayıcı: canlı DB'ye (DB_PATH) dokunan çağrının runuser altında olup olmadığını kaydeder
    (stub / "sqlite3").write_text(
        f'#!/bin/bash\necho "sqlite3 active=${{RUNUSER_ACTIVE:-0}} $*" >> "{olay}"\nexec {GERCEK_SQLITE} "$@"\n')
    for p in stub.iterdir():
        p.chmod(0o755)
    env = dict(
        os.environ, PATH=f"{stub}:{os.environ['PATH']}", DB_PATH=str(db), BACKUP_DIR=str(tmp_path / "backups"),
        APP_USER=BEN, TMPDIR=str(tmp_path),
    )
    return dict(env=env, db=db, veri=veri, olay=olay, tmp=tmp_path, stub=stub, yedek=tmp_path / "backups")


def _normal_kullanici(o, ad=None):
    """Sahte `id`: uid 1000 ve verilen kullanıcı adını bildirir (gerçek root olsak bile 'root değil' yolu sınanır)."""
    (o["stub"] / "id").write_text(
        f'#!/bin/bash\ncase "$*" in "-u") echo 1000;; "-un") echo {ad or BEN};; *) exec /usr/bin/id "$@";; esac\n')


def _calistir(betik, env, *arg, girdi=None):
    return subprocess.run(["bash", betik, *arg], env=env, capture_output=True, text=True, timeout=120, input=girdi)


def _olaylar(o):
    return o["olay"].read_text().splitlines() if o["olay"].exists() else []


def _yedekler(d):
    return sorted(p for p in os.listdir(d) if p.startswith("dipbot-") and p.endswith(".db")) if os.path.isdir(d) else []


# ---------------------------------------------------------------- backup.sh: canlı DB'ye root dokunmaz
def test_root_calisinca_canli_db_yalniz_runuser_ile_acilir(ortam):
    r = _calistir(BACKUP, ortam["env"])
    assert r.returncode == 0, r.stderr + r.stdout
    ol = _olaylar(ortam)
    canli = [l for l in ol if f"{ortam['db']}" in l and l.startswith("sqlite3")]
    assert canli, "canlı DB'ye yedek çağrısı yapılmalı"
    assert all(l.startswith("sqlite3 active=1 ") for l in canli), canli  # HER canlı DB çağrısı runuser altında
    assert any(l.startswith(f"runuser -u {BEN} -- sqlite3 ") for l in ol)
    assert any("-readonly" in l and ".backup" in l for l in canli), "kaynak salt okunur açılmalı"
    assert not [l for l in ol if f"{ortam['db']}" in l and l.startswith("sqlite3 active=0")]
    # veri dizininde betik tarafından -wal/-shm bırakılmadı
    assert set(os.listdir(ortam["veri"])) <= {"dipbot.db", "dipbot.db-wal", "dipbot.db-shm"}
    assert {os.stat(ortam["veri"] / f).st_uid for f in os.listdir(ortam["veri"])} == {os.getuid()}  # tümü APP_USER'ın
    # yedek: tek, 0600, doğrulanmış, aynı veri, geçici dosya yok
    y = _yedekler(ortam["yedek"])
    assert len(y) == 1 and not [p for p in os.listdir(ortam["yedek"]) if p.endswith(".partial")]
    assert oct(os.stat(ortam["yedek"] / y[0]).st_mode & 0o777) == "0o600"
    assert _ozet(ortam["yedek"] / y[0]) == _ozet(ortam["db"])
    assert not [p for p in os.listdir(ortam["tmp"]) if p.startswith("dipbot-yedek.")], "geçici dizin silinmeli"


def test_root_degilken_yalniz_app_user_calistirabilir(ortam):
    _normal_kullanici(ortam)
    env = ortam["env"]
    r = _calistir(BACKUP, env)  # APP_USER=BEN, root değil: doğrudan çalışır, runuser kullanılmaz
    assert r.returncode == 0, r.stderr
    assert len(_yedekler(ortam["yedek"])) == 1
    assert not [l for l in _olaylar(ortam) if l.startswith("runuser")]
    r = _calistir(BACKUP, dict(env, APP_USER="baska_kullanici_yok"))
    assert r.returncode != 0 and "olarak calistirin" in r.stderr
    assert len(_yedekler(ortam["yedek"])) == 1, "yetkisiz kullanıcıyla yeni yedek alınmamalı"


def test_db_sahibi_app_user_degilse_yedek_alinmaz(ortam):
    try:
        subprocess.run(["id", "-u", "nobody"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pytest.skip("nobody kullanıcısı yok")
    if BEN == "nobody":
        pytest.skip("nobody olarak çalışıyor")
    r = _calistir(BACKUP, dict(ortam["env"], APP_USER="nobody"))
    assert r.returncode != 0 and "sahibi" in r.stderr and "beklenen 'nobody'" in r.stderr
    assert not os.path.exists(ortam["yedek"]) or not _yedekler(ortam["yedek"])
    assert not [l for l in _olaylar(ortam) if str(ortam["db"]) in l], "sahiplik uygun değilken DB'ye dokunulmamalı"


def test_root_sahipli_wal_shm_varsa_yedek_alinmaz(ortam):
    # sahte stat: -wal dosyasının sahibini 'root' bildirir (CI'da gerçek chown mümkün değil)
    (ortam["stub"] / "stat").write_text(
        '#!/bin/bash\nfor a in "$@"; do case "$a" in *-wal) [[ "$*" == *"%U"* ]] && { echo mallory; exit 0; };; esac; done\nexec /usr/bin/stat "$@"\n')
    (ortam["stub"] / "stat").chmod(0o755)
    (ortam["veri"] / "dipbot.db-wal").write_bytes(b"")
    r = _calistir(BACKUP, ortam["env"])
    assert r.returncode != 0 and "-wal sahibi 'mallory'" in r.stderr
    assert not _yedekler(ortam["yedek"])
    assert not [l for l in _olaylar(ortam) if l.startswith("sqlite3")], "DB'ye hiç dokunulmamalı"


def test_mevcut_yedek_dizini_sahip_izin_degismez_ve_icerik_korunur(ortam):
    d = ortam["yedek"]
    d.mkdir(mode=0o750)
    os.chmod(d, 0o750)
    eski = d / "dipbot-20200101T000000Z.db"
    eski.write_bytes(b"eski")
    onceki = os.stat(d)
    r = _calistir(BACKUP, ortam["env"])
    assert r.returncode == 0, r.stderr
    sonra = os.stat(d)
    assert (sonra.st_mode & 0o7777, sonra.st_uid, sonra.st_gid) == (onceki.st_mode & 0o7777, onceki.st_uid, onceki.st_gid)
    assert eski.read_bytes() == b"eski"
    assert "diger kullanicilarca okunabilir" not in r.stderr  # 0750: 'diğer' erişimi yok -> uyarı yok
    d2 = ortam["tmp"] / "backups2"
    d2.mkdir()
    os.chmod(d2, 0o755)  # diğerleri okuyabilir: UYARI, ama izne dokunulmaz
    r = _calistir(BACKUP, dict(ortam["env"], BACKUP_DIR=str(d2)))
    assert r.returncode == 0 and "UYARI" in r.stderr
    assert oct(os.stat(d2).st_mode & 0o7777) == "0o755"
    y = _yedekler(d2)
    assert oct(os.stat(d2 / y[0]).st_mode & 0o777) == "0o600"


def test_yazilabilir_veya_sembolik_yedek_dizini_reddedilir(ortam):
    d = ortam["tmp"] / "gevsek"
    d.mkdir()
    os.chmod(d, 0o770)
    r = _calistir(BACKUP, dict(ortam["env"], BACKUP_DIR=str(d)))
    assert r.returncode != 0 and "yazilabilir" in r.stderr
    assert oct(os.stat(d).st_mode & 0o7777) == "0o770", "izin BETİK tarafından değiştirilmemeli"
    assert not _yedekler(d)
    hedef = ortam["tmp"] / "hedef"
    hedef.mkdir(mode=0o700)
    (ortam["tmp"] / "baglanti").symlink_to(hedef)
    r = _calistir(BACKUP, dict(ortam["env"], BACKUP_DIR=str(ortam["tmp"] / "baglanti")))
    assert r.returncode != 0 and "sembolik" in r.stderr
    assert not _yedekler(hedef)
    assert not [l for l in _olaylar(ortam) if str(ortam["db"]) in l]


def test_yeni_yedek_dizini_0700_olusur(ortam):
    r = _calistir(BACKUP, ortam["env"])
    assert r.returncode == 0
    assert oct(os.stat(ortam["yedek"]).st_mode & 0o777) == "0o700"


def test_bozuk_veritabani_yedegi_butunluk_kontrolunden_gecmez_yedek_kalmaz(ortam):
    ortam["db"].write_bytes(b"SQLite format 3\x00" + b"\x00" * 100 + b"garbage" * 500)
    r = _calistir(BACKUP, ortam["env"])
    assert r.returncode != 0
    assert not _yedekler(ortam["yedek"]) and not [p for p in os.listdir(ortam["yedek"]) if "partial" in p]


def test_saklama_son_keep_yedek_davranisi_degismedi(ortam):
    d = ortam["yedek"]
    d.mkdir(mode=0o700)
    for i in range(1, 6):
        (d / f"dipbot-2020010{i}T000000Z.db").write_bytes(b"x")
    r = _calistir(BACKUP, dict(ortam["env"], KEEP="3"))
    assert r.returncode == 0, r.stderr
    kalan = _yedekler(d)
    assert len(kalan) == 3 and kalan[-1] > "dipbot-2021", "en yeni yedek (bugünkü) kalmalı"
    assert "dipbot-20200101T000000Z.db" not in kalan and "dipbot-20200102T000000Z.db" not in kalan


def test_yedek_sir_icermez(ortam):
    (ortam["tmp"] / "dipbot.env").write_text("BOT_TOKEN=123456:SECRET\n")
    r = _calistir(BACKUP, ortam["env"])
    assert r.returncode == 0
    y = ortam["yedek"] / _yedekler(ortam["yedek"])[0]
    assert b"SECRET" not in y.read_bytes() and "SECRET" not in r.stdout + r.stderr


# ---------------------------------------------------------------- guncelle.sh: önce yedek, git deponun sahibi olarak
@pytest.fixture
def depo(ortam):
    tmp = ortam["tmp"]
    origin = tmp / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "requirements.txt").write_text("# bos\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "v1")
    v1 = _git(origin, "rev-parse", "HEAD")
    app = tmp / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(app)], check=True)
    (origin / "yeni.txt").write_text("v2\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "v2")
    v2 = _git(origin, "rev-parse", "HEAD")
    for ad, govde in {"pip": "exit 0", "onkontrol": 'echo "onkontrol" >> "$OLAY"', "systemctl": 'echo "systemctl $*" >> "$OLAY"'}.items():
        p = ortam["stub"] / f"s_{ad}"
        p.write_text(f"#!/bin/bash\n{govde}\n")
        p.chmod(0o755)
    env = dict(
        ortam["env"], GUNCELLE_ROOT_ZORUNLU="0", APP_DIR=str(app), BACKUP_SCRIPT=BACKUP, OLAY=str(ortam["olay"]),
        ONKONTROL=str(ortam["stub"] / "s_onkontrol"), PIP_KOMUTU=str(ortam["stub"] / "s_pip"),
        SYSTEMCTL=str(ortam["stub"] / "s_systemctl"), BEKLE_SN="0", REMOTE="origin",
    )
    return dict(ortam, env=env, app=app, v1=v1, v2=v2)


def test_guncelle_git_deponun_sahibi_olarak_calisir_yedek_runuser_ile(depo):
    # GIT_USER açıkça verilir; root(sahte id) iken git `runuser -u GIT_USER -- env HOME=... git` ile çalışmalı
    r = _calistir(GUNCELLE, dict(depo["env"], GIT_USER="nobody"), "main")
    assert r.returncode == 0, r.stderr + r.stdout
    assert _git(depo["app"], "rev-parse", "HEAD") == depo["v2"]
    ol = _olaylar(depo)
    git_cagrilari = [l for l in ol if l.startswith("runuser -u nobody -- env HOME=") and " git -C " in l]
    assert any(" fetch " in l for l in git_cagrilari) and any(" merge --ff-only " in l for l in git_cagrilari), git_cagrilari
    assert all("GIT_TERMINAL_PROMPT=0" in l for l in git_cagrilari)
    # yedek: canlı DB çağrıları runuser altında, APP_USER ile
    canli = [l for l in ol if l.startswith("sqlite3") and str(depo["db"]) in l]
    assert canli and all(l.startswith("sqlite3 active=1 ") for l in canli), canli
    assert any(l.startswith(f"runuser -u {BEN} -- sqlite3 ") for l in ol)
    assert {os.stat(depo["veri"] / f).st_uid for f in os.listdir(depo["veri"])} == {os.getuid()}, "veri dizinindeki her dosya APP_USER'ın"
    assert _ozet(depo["db"]) == _ozet(depo["yedek"] / _yedekler(depo["yedek"])[0])
    # sıra: yedek -> git fetch -> ön kontrol -> restart
    idx = lambda ön: next(i for i, l in enumerate(ol) if ön(l))  # noqa: E731
    assert idx(lambda l: l.startswith("runuser") and "sqlite3" in l) < idx(lambda l: " fetch " in l) \
        < idx(lambda l: l == "onkontrol") < idx(lambda l: l.startswith("systemctl restart"))


def test_guncelle_yedek_dizini_guvenli_degilse_kod_ve_servis_degismez(depo):
    d = depo["tmp"] / "gevsek"
    d.mkdir()
    os.chmod(d, 0o770)
    r = _calistir(GUNCELLE, dict(depo["env"], BACKUP_DIR=str(d), GIT_USER="nobody"), "main")
    assert r.returncode != 0 and "yedek alinamadi" in r.stderr
    assert _git(depo["app"], "rev-parse", "HEAD") == depo["v1"], "kod güncellenmemeli"
    ol = _olaylar(depo)
    assert not [l for l in ol if l.startswith("systemctl") or l == "onkontrol" or " fetch " in l], ol
    assert oct(os.stat(d).st_mode & 0o7777) == "0o770"


def test_guncelle_db_sahibi_yanlissa_hicbir_sey_degismez(depo):
    try:
        subprocess.run(["id", "-u", "nobody"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pytest.skip("nobody yok")
    if BEN == "nobody":
        pytest.skip("nobody olarak çalışıyor")
    r = _calistir(GUNCELLE, dict(depo["env"], APP_USER="nobody", GIT_USER="nobody"), "main")
    assert r.returncode != 0 and "yedek alinamadi" in r.stderr
    assert _git(depo["app"], "rev-parse", "HEAD") == depo["v1"]
    assert not [l for l in _olaylar(depo) if l.startswith("systemctl")]


def test_guncelle_root_degilken_git_dogrudan_calisir(depo):
    _normal_kullanici(depo)  # uid 1000: root değil (GUNCELLE_ROOT_ZORUNLU=0 yalnız testte kök zorunluluğunu atlar)
    r = _calistir(GUNCELLE, dict(depo["env"], APP_USER=BEN), "main")
    assert r.returncode == 0, r.stderr + r.stdout
    assert _git(depo["app"], "rev-parse", "HEAD") == depo["v2"]
    assert not [l for l in _olaylar(depo) if l.startswith("runuser")], "root değilken runuser/git sahte kullanıcı yok"


# ---------------------------------------------------------------- onkontrol.sh: root sahipli WAL/SHM tespiti
def test_onkontrol_root_sahipli_wal_hata_verir(ortam, tmp_path):
    # yalnız bu denetimi sınamak için: sahte stat -wal'ı root bildirir
    (ortam["stub"] / "stat").write_text(
        '#!/bin/bash\nfor a in "$@"; do case "$a" in *-wal) echo "600 mallory:mallory"; exit 0;; esac; done\nexec /usr/bin/stat "$@"\n')
    (ortam["stub"] / "stat").chmod(0o755)
    (ortam["veri"] / "dipbot.db-wal").write_bytes(b"")
    env = dict(ortam["env"], DATA_DIR=str(ortam["veri"]), DB_FILE=str(ortam["db"]), APP_DIR=str(tmp_path / "yok"),
               ENV_FILE=str(tmp_path / "yok.env"), UNIT_FILE=str(tmp_path / "yok.service"))
    r = _calistir(ONKONTROL, env)
    assert r.returncode == 1
    assert f"HATA  : {ortam['db']}-wal sahibi/izni" in r.stdout and "mallory:mallory" in r.stdout
    assert "root olarak sqlite3 ile dokunmayin" in r.stdout


# ---------------------------------------------------------------- restore.sh: güvenlik yedeği de runuser ile
def test_restore_guvenlik_yedegi_canli_dbye_root_dokunmadan_alinir(ortam):
    (ortam["stub"] / "systemctl").write_text(f'#!/bin/bash\necho "systemctl $*" >> "{ortam["olay"]}"\nexit 0\n')
    (ortam["stub"] / "systemctl").chmod(0o755)
    kaynak_yedek = ortam["tmp"] / "eski-yedek.db"
    _db_olustur(kaynak_yedek, n=2)  # farklı içerik: 2 oyuncu
    # WAL'dan kalma artık dosyalar olmasın diye tek bağlantıyla kapanmış durumda
    guvenlik = ortam["tmp"] / "guvenlik"
    env = dict(ortam["env"], SAFETY_DIR=str(guvenlik))
    onceki = _ozet(ortam["db"])
    r = _calistir(RESTORE, env, str(kaynak_yedek), girdi="EVET\n")
    assert r.returncode == 0, r.stderr + r.stdout
    ol = _olaylar(ortam)
    canli = [l for l in ol if l.startswith("sqlite3") and str(ortam["db"]) in l]
    assert canli and all(l.startswith("sqlite3 active=1 ") for l in canli), canli
    assert any(l.startswith(f"runuser -u {BEN} -- sqlite3 ") for l in ol)
    g = [p for p in os.listdir(guvenlik) if p.startswith("dipbot-pre-restore-")]
    assert len(g) == 1 and oct(os.stat(guvenlik / g[0]).st_mode & 0o777) == "0o600"
    assert _ozet(guvenlik / g[0]) == onceki, "geri yüklemeden önceki canlı veri güvenlik yedeğinde"
    assert len(_ozet(ortam["db"])[1]) == 2, "DB yedekle değiştirildi"
    assert ol.index("systemctl stop dipbot") < next(i for i, l in enumerate(ol) if l.startswith("runuser"))


def test_restore_yanlis_onayda_iptal_eder(ortam):
    (ortam["stub"] / "systemctl").write_text('#!/bin/bash\nexit 0\n')
    (ortam["stub"] / "systemctl").chmod(0o755)
    kaynak_yedek = ortam["tmp"] / "y.db"
    _db_olustur(kaynak_yedek, n=1)
    onceki = _ozet(ortam["db"])
    r = _calistir(RESTORE, ortam["env"], str(kaynak_yedek), girdi="hayir\n")
    assert r.returncode != 0
    assert _ozet(ortam["db"]) == onceki
