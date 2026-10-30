"""deploy/onkontrol.sh: hesap sırlarının (token/proxy) izin ve sahiplik denetimi. Yerel geçici dizinle; gerçek sunucu yok."""
import os
import shutil
import subprocess

import pytest

KOK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BETIK = os.path.join(KOK, "deploy", "onkontrol.sh")
pytestmark = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("find"), reason="bash/find gerekli")
BEN = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()


def _calistir(tmp_path, hesap_kur=None):
    veri = tmp_path / "data"
    veri.mkdir(mode=0o750, exist_ok=True)
    if hesap_kur:
        hesap_kur(veri / "hesaplar")
    envf = tmp_path / "dipbot.env"
    envf.write_text("BOT_TOKEN=1:x\n")
    envf.chmod(0o600)
    env = dict(os.environ, DATA_DIR=str(veri), DB_FILE=str(veri / "dipbot.db"), ENV_FILE=str(envf), APP_DIR=str(tmp_path / "yok"),
               UNIT_FILE=str(tmp_path / "yok.service"), APP_USER=BEN)
    return subprocess.run(["bash", BETIK], env=env, capture_output=True, text=True, timeout=60).stdout


def _hesap(kok, mod_dizin=0o700, mod_dosya=0o600):
    kok.mkdir(parents=True, mode=0o700)
    os.chmod(kok, mod_dizin)
    d = kok / "1"
    d.mkdir(mode=0o700)
    os.chmod(d, mod_dizin)
    for ad in ("token", "proxy"):
        (d / ad).write_text("GIZLI-ICERIK-ASLA-YAZDIRILMAZ")
        os.chmod(d / ad, mod_dosya)


def test_hesap_dizini_yoksa_uyari_tarama_olmayacagini_soyler(tmp_path):
    out = _calistir(tmp_path)
    assert "UYARI : henuz hesap dizini yok" in out and "Account Management" in out


def test_dogru_izinler_tamam_ve_icerik_yazdirilmaz(tmp_path):
    out = _calistir(tmp_path, _hesap)
    assert "TAMAM : " in out and "dizinler 0700" in out and "token/proxy dosyalari 0600" in out
    assert "GIZLI-ICERIK" not in out and "HATA  : dizin izni" not in out and "HATA  : dosya izni" not in out


def test_gevsek_dosya_veya_dizin_izni_hata_verir(tmp_path):
    out = _calistir(tmp_path, lambda k: _hesap(k, mod_dosya=0o644))
    assert "HATA  : dosya izni 0600 degil" in out and "GIZLI-ICERIK" not in out
    out2 = _calistir(tmp_path / "b", lambda k: _hesap(k, mod_dizin=0o755)) if (tmp_path / "b").mkdir() is None else ""
    assert "HATA  : dizin izni 0700 degil" in out2


def test_veritabaninda_jwt_benzeri_deger_hata_verir(tmp_path):
    def kur(kok):
        _hesap(kok)
        (kok.parent / "dipbot.db").write_bytes(b"SQLite format 3\x00" + b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln" + b"\x00" * 50)

    assert "HATA  : veritabani dosyasinda JWT benzeri deger" in _calistir(tmp_path, kur)
