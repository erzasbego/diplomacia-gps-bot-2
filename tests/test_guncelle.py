"""deploy/guncelle.sh: yedek-önce güvenli güncelleme. Yerel git depoları, sahte systemctl/pip ve sahte dizinlerle
çalışır; GERÇEK sunucuda/servisle ÇALIŞTIRILMAZ. sqlite3 CLI ve git yoksa atlanır."""
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

KOK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BETIK = os.path.join(KOK, "deploy", "guncelle.sh")
BACKUP = os.path.join(KOK, "deploy", "backup.sh")

pytestmark = pytest.mark.skipif(
    not (shutil.which("sqlite3") and shutil.which("git") and shutil.which("bash")),
    reason="sqlite3/git/bash gerekli",
)


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def ortam(tmp_path):
    """origin (2 commit) + APP_DIR (1. commit'te) + dolu bir WAL veritabanı + sahte komutlar."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "requirements.txt").write_text("# bos\n")
    _git(origin, "add", "."); _git(origin, "commit", "-q", "-m", "v1")
    v1 = _git(origin, "rev-parse", "HEAD")
    app = tmp_path / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(app)], check=True)
    (origin / "yeni.txt").write_text("v2\n")
    _git(origin, "add", "."); _git(origin, "commit", "-q", "-m", "v2")
    v2 = _git(origin, "rev-parse", "HEAD")

    veri = tmp_path / "data"; veri.mkdir()
    db = veri / "dipbot.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE onayli_gruplar(group_id INTEGER PRIMARY KEY, donem INTEGER)")
    c.execute("CREATE TABLE takip_oyuncular(group_id INTEGER, oyuncu_id TEXT)")
    c.execute("INSERT INTO onayli_gruplar VALUES(-1001, 1)")
    c.executemany("INSERT INTO takip_oyuncular VALUES(-1001, ?)", [(str(i),) for i in range(5)])
    c.execute("PRAGMA user_version=4")
    c.commit()
    c.close()

    stub = tmp_path / "stub"; stub.mkdir()
    gunluk = tmp_path / "olaylar.log"
    for ad, govde in {
        "systemctl": f'echo "systemctl $*" >> "{gunluk}"\nexit "${{SYSTEMCTL_KOD:-0}}"\n',
        "pip": f'echo "pip yedek_sayisi=$(ls {tmp_path}/backups 2>/dev/null | grep -c "^dipbot-.*\\.db$") HEAD=$(git -C {app} rev-parse HEAD)" >> "{gunluk}"\nexit "${{PIP_KOD:-0}}"\n',
        "onkontrol": f'echo "onkontrol" >> "{gunluk}"\nexit "${{ONKONTROL_KOD:-0}}"\n',
    }.items():
        yol = stub / ad
        yol.write_text("#!/bin/bash\n" + govde)
        yol.chmod(0o755)
    env = dict(
        os.environ, GUNCELLE_ROOT_ZORUNLU="0", APP_DIR=str(app), DB_PATH=str(db), BACKUP_DIR=str(tmp_path / "backups"),
        BACKUP_SCRIPT=BACKUP, ONKONTROL=str(stub / "onkontrol"), PIP_KOMUTU=str(stub / "pip"),
        SYSTEMCTL=str(stub / "systemctl"), BEKLE_SN="0", REMOTE="origin",
    )
    return dict(env=env, app=app, origin=origin, v1=v1, v2=v2, db=db, gunluk=gunluk, tmp=tmp_path, stub=stub)


def _calistir(o, *arg, **extra):
    env = dict(o["env"], **extra)
    return subprocess.run(["bash", BETIK, *arg], env=env, capture_output=True, text=True, timeout=120)


def _db_ozeti(db):
    c = sqlite3.connect(db)
    try:
        return [list(c.execute("SELECT * FROM onayli_gruplar")), list(c.execute("SELECT * FROM takip_oyuncular ORDER BY 2"))]
    finally:
        c.close()


def _olaylar(o):
    return o["gunluk"].read_text().splitlines() if o["gunluk"].exists() else []


def test_basarili_guncelleme_once_yedek_sonra_kod_sonra_servis(ortam):
    once = _db_ozeti(ortam["db"])
    r = _calistir(ortam)
    assert r.returncode == 0, r.stderr + r.stdout
    ol = _olaylar(ortam)
    pip = [l for l in ol if l.startswith("pip")][0]
    assert "yedek_sayisi=1" in pip, "bağımlılık/kod adımına gelindiğinde yedek ZATEN alınmış olmalı"
    assert f"HEAD={ortam['v2']}" in pip
    assert ol.index(pip) < ol.index("onkontrol") < ol.index("systemctl restart dipbot") < ol.index("systemctl is-active --quiet dipbot")
    assert _git(ortam["app"], "rev-parse", "HEAD") == ortam["v2"]
    assert _db_ozeti(ortam["db"]) == once, "betik veritabanına yazmaz; kayıtlar korunur"
    yedekler = [p for p in os.listdir(ortam["tmp"] / "backups") if p.startswith("dipbot-") and p.endswith(".db")]
    assert len(yedekler) == 1
    # yedek geri yüklenebilir ve aynı veriyi içerir
    assert _db_ozeti(ortam["tmp"] / "backups" / yedekler[0]) == once
    assert oct(os.stat(ortam["tmp"] / "backups" / yedekler[0]).st_mode & 0o777) == "0o600"
    kayit = [p for p in os.listdir(ortam["tmp"] / "backups") if p.startswith("guncelleme-")]
    assert len(kayit) == 1
    icerik = (ortam["tmp"] / "backups" / kayit[0]).read_text()
    assert f"onceki_surum={ortam['v1']}" in icerik and "sema_surumu_onceki=4" in icerik


def test_yedek_alinamazsa_hicbir_sey_degismez(ortam):
    bozuk = ortam["tmp"] / "backup_bozuk.sh"
    bozuk.write_text("#!/bin/bash\necho 'yedek patladi' >&2\nexit 1\n"); bozuk.chmod(0o755)
    once = _db_ozeti(ortam["db"])
    r = _calistir(ortam, BACKUP_SCRIPT=str(bozuk))
    assert r.returncode != 0 and "yedek alinamadi" in r.stderr and "Hicbir sey degismedi" in r.stderr
    assert _git(ortam["app"], "rev-parse", "HEAD") == ortam["v1"], "kod güncellenmemeli"
    assert _olaylar(ortam) == [], "pip/onkontrol/systemctl çağrılmamalı"
    assert _db_ozeti(ortam["db"]) == once


def test_yedek_dosyasi_dogrulanamazsa_durur(ortam):
    sahte = ortam["tmp"] / "backup_sahte.sh"
    sahte.write_text("#!/bin/bash\nmkdir -p \"$BACKUP_DIR\"\necho bozuk > \"$BACKUP_DIR/dipbot-x.db\"\n"
                     "echo \"Yedek alindi: $BACKUP_DIR/dipbot-x.db\"\n"); sahte.chmod(0o755)
    r = _calistir(ortam, BACKUP_SCRIPT=str(sahte))
    assert r.returncode != 0
    assert _git(ortam["app"], "rev-parse", "HEAD") == ortam["v1"] and _olaylar(ortam) == []


def test_veritabani_yoksa_durur_yanlis_yol_sessizce_gecilmez(ortam):
    r = _calistir(ortam, DB_PATH=str(ortam["tmp"] / "yok" / "dipbot.db"))
    assert r.returncode != 0 and "veritabani yok" in r.stderr
    assert _git(ortam["app"], "rev-parse", "HEAD") == ortam["v1"] and _olaylar(ortam) == []
    assert not (ortam["tmp"] / "backups").exists() or not os.listdir(ortam["tmp"] / "backups")


def test_ileri_sarma_degilse_kod_degismez_ama_yedek_hazirdir(ortam):
    (ortam["app"] / "yerel.txt").write_text("yerel\n")
    _git(ortam["app"], "add", "."); _git(ortam["app"], "commit", "-q", "-m", "yerel sapma")
    yerel = _git(ortam["app"], "rev-parse", "HEAD")
    r = _calistir(ortam)
    assert r.returncode != 0 and "ileri sarmasi degil" in r.stderr
    assert _git(ortam["app"], "rev-parse", "HEAD") == yerel
    assert not any(l.startswith("systemctl restart") for l in _olaylar(ortam))
    assert any(p.startswith("dipbot-") for p in os.listdir(ortam["tmp"] / "backups"))


def test_yerel_degisiklik_varsa_yedekten_once_durur(ortam):
    (ortam["app"] / "requirements.txt").write_text("# elle degistirildi\n")
    r = _calistir(ortam)
    assert r.returncode != 0 and "yerel degisiklik" in r.stderr
    assert not (ortam["tmp"] / "backups").exists() and _olaylar(ortam) == []


@pytest.mark.parametrize("anahtar,deger,beklenen", [
    ("ONKONTROL_KOD", "1", "on kontrol basarisiz"),
    ("PIP_KOD", "1", "bagimlilik kurulumu basarisiz"),
])
def test_onkontrol_veya_pip_basarisiz_servis_yeniden_baslatilmaz_geri_donus_plani(ortam, anahtar, deger, beklenen):
    r = _calistir(ortam, **{anahtar: deger})
    assert r.returncode != 0 and beklenen in r.stderr
    assert "GERI DONUS PLANI" in r.stderr and ortam["v1"] in r.stderr and "dipbot-restore" in r.stderr
    assert not any(l.startswith("systemctl") for l in _olaylar(ortam)), "servise dokunulmaz"


def test_servis_acilmazsa_geri_donus_plani_yazilir(ortam):
    r = _calistir(ortam, SYSTEMCTL_KOD="3")
    assert r.returncode != 0 and "GERI DONUS PLANI" in r.stderr and ortam["v1"] in r.stderr


def test_gecersiz_ref_reddedilir(ortam):
    r = subprocess.run(["bash", BETIK, "main; rm -rf /"], env=ortam["env"], capture_output=True, text=True)
    assert r.returncode != 0 and "gecersiz REF" in r.stderr
    assert _olaylar(ortam) == []


def test_betikler_depoda_calistirilabilir_modda():
    """backup.sh depoda 0644'tü (guncelle.sh doğrudan çağırınca 'Permission denied'); artık hepsi 0755."""
    cikti = subprocess.run(["git", "-C", KOK, "ls-files", "-s", "deploy"], capture_output=True, text=True).stdout
    modlar = {satir.split("\t")[1]: satir.split()[0] for satir in cikti.splitlines()}
    for ad in ("backup.sh", "restore.sh", "onkontrol.sh", "guncelle.sh"):
        assert modlar[f"deploy/{ad}"] == "100755", ad
