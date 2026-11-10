#!/usr/bin/env python3
"""Vearl testnet miner wrapper — stdlib only.

This is NOT a miner. It downloads, verifies and runs the OFFICIAL Vearl
miner binary (github.com/vearl-verified-compute/vearl) and shows a local
status dashboard. All mining / Proof-of-Useful-Work is done by that binary.

    python3 run.py --check        system + GPU preflight
    python3 run.py --download     fetch + SHA-256 verify + extract rc27
    python3 run.py                run the miner (auto-restart, logs to miner.log)
    python3 run.py --dashboard    status page on http://127.0.0.1:8080
    python3 run.py --register --contact <discord-or-email>

Safety: wallet.json / 24 recovery words are NEVER printed or logged.
Only the public vrl1... address is ever shown in the terminal. On the
dashboard, the words appear only on the Profile page, only after login,
and only if you saved them in the encrypted vault (opt-in). The dashboard
password itself is stored only as a salted PBKDF2 hash — never plaintext —
and there is no password recovery: forget it and the dashboard + vault
stay locked. Testnet tokens have no value.

Env: VEALR_VERSION (default 0.1.0-rc27, VEARL_VERSION also accepted),
     VEALR_DIR (default ./vearl), DASHBOARD_PORT (default 8080).
"""
import argparse
import contextlib
import hashlib
import hmac
import io
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = os.environ.get("VEALR_VERSION") or os.environ.get("VEARL_VERSION") or "0.1.0-rc27"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
VEALR_DIR = os.environ.get("VEALR_DIR") or os.environ.get("VEARL_DIR") or os.path.join(BASE_DIR, "vearl")
REPO = "vearl-verified-compute/vearl"
RELEASE_BASE = f"https://github.com/{REPO}/releases/download/v{VERSION}"
LEADERBOARD_URL = "https://vearl.org/rewards/leaderboard.json"  # verified live 2026-11-10
LEADERBOARD_PAGE = "https://vearl.org/leaderboard/"
MAX_RESTARTS = 5
BACKOFF = [5, 10, 30, 30, 30]
LOG_FILE = os.path.join(BASE_DIR, "miner.log")
PID_FILE = os.path.join(BASE_DIR, ".miner.pid")
CACHE_FILE = os.path.join(BASE_DIR, ".leaderboard-cache.json")
VAULT_FILE = os.path.join(BASE_DIR, "seed.vault")
AUTH_FILE = os.path.join(BASE_DIR, ".dashboard-auth.json")
AUTH_ITERATIONS = 200_000
SESSION_TTL = 12 * 3600  # 12 jam
REGISTERED_URL = "https://vearl.org/rewards/registered.json"  # verified live 2026-11-10
CACHE_TTL = 300

SENSITIVE_WORDS = ("recovery word", "mnemonic", "seed phrase",
                   "private key", "24 words", "secret key", "passphrase")

# Platforms that BAN crypto mining (verified 10 Nov 2026). Detected via
# their own standard env vars. There is no stealth here: the wrapper
# refuses to mine on them unless the user explicitly opts in.
BANNED_PLATFORMS = [
    ("Railway", ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")),
    ("Render", ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_HOSTNAME")),
    ("Google Colab", ("COLAB_RELEASE_TAG", "COLAB_GPU")),
    ("Kaggle", ("KAGGLE_KERNEL_RUN_TYPE", "KAGGLE_URL_BASE")),
]
ACK_ENV = "I_UNDERSTAND_BANNED_PLATFORM"


def detect_banned_platform():
    """Return the banned platform name if running on one, else None."""
    for name, variables in BANNED_PLATFORMS:
        for var in variables:
            if os.environ.get(var):
                return name
    return None


def warn_banned_platform(platform_name):
    print("=" * 68)
    print(f"WARNING: you are running on {platform_name}.")
    print(f"Crypto mining is BANNED on {platform_name} and the account can be")
    print("suspended or terminated. It also has no suitable GPU, so Vearl")
    print("mining would earn ~nothing. Use a GPU VPS or your own GPU instead.")
    print("See README.md and deploy/README.md for the honest alternatives.")
    print("=" * 68)


def banned_platform_gate(action):
    """Hard gate for mining actions. Returns True if allowed to proceed."""
    platform_name = detect_banned_platform()
    if platform_name is None:
        return True
    warn_banned_platform(platform_name)
    if os.environ.get(ACK_ENV) == "1":
        print(f"You set {ACK_ENV}=1: proceeding with '{action}' at your own risk.")
        return True
    print(f"REFUSED: will not '{action}' on {platform_name} without explicit consent.")
    print(f"Set {ACK_ENV}=1 if you have read the warning and accept the risk.")
    print("Recommended instead: run this on a GPU VPS or a local Ampere+ GPU.")
    return False


def archive_name():
    if sys.platform.startswith("win"):
        return f"vearl-{VERSION}-windows-x86_64.zip"
    return f"vearl-{VERSION}-linux-x86_64.tar.gz"


def miner_name():
    return "vearl-miner.exe" if sys.platform.startswith("win") else "vearl-miner"


def locate_binary(name):
    """Find an official binary: flat in VEALR_DIR, or one level down
    (the rc27 tarball extracts to vearl-0.1.0-rc27/ with the binaries inside)."""
    direct = os.path.join(VEALR_DIR, name)
    if os.path.exists(direct):
        return direct
    if os.path.isdir(VEALR_DIR):
        for entry in sorted(os.listdir(VEALR_DIR)):
            cand = os.path.join(VEALR_DIR, entry, name)
            if os.path.exists(cand):
                return cand
    return direct  # not found yet — return the expected path for messages


def miner_path():
    return locate_binary(miner_name())


def miner_dir():
    return os.path.dirname(miner_path())


def wallet_path():
    return os.path.join(miner_dir(), "wallet.json")


def safe_line(line):
    """Redact anything that could be a secret. Address (vrl1...) is fine."""
    low = line.lower()
    if any(w in low for w in SENSITIVE_WORDS):
        return "[redacted — recovery words / secrets are never logged]\n"
    return line


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_url(url, dest, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "vearl-miner-wrapper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def parse_sums(text):
    sums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sums[parts[1].lstrip("*")] = parts[0]
    return sums


def verify_archive(archive_path, sums_path):
    """Return True only if the archive matches SHA256SUMS.txt. No match = refuse."""
    with open(sums_path, encoding="utf-8", errors="replace") as f:
        sums = parse_sums(f.read())
    name = os.path.basename(archive_path)
    expected = sums.get(name)
    if not expected:
        print(f"FAIL: no SHA-256 entry for {name} in SHA256SUMS.txt")
        return False
    actual = sha256_file(archive_path)
    if actual.lower() != expected.lower():
        print(f"FAIL: SHA-256 mismatch for {name}\n  expected {expected}\n  actual   {actual}")
        return False
    print(f"OK: SHA-256 verified for {name}")
    return True


def do_download(dry_run=False):
    name = archive_name()
    archive_url = f"{RELEASE_BASE}/{name}"
    sums_url = f"{RELEASE_BASE}/SHA256SUMS.txt"
    print(f"release : v{VERSION}")
    print(f"archive : {archive_url}")
    print(f"checksums: {sums_url}")
    if dry_run:
        print("(dry run — nothing downloaded)")
        return True
    tmp = os.path.join(BASE_DIR, ".download")
    os.makedirs(tmp, exist_ok=True)
    archive_path = os.path.join(tmp, name)
    sums_path = os.path.join(tmp, "SHA256SUMS.txt")
    print("downloading archive (~30 MB) ...")
    download_url(archive_url, archive_path)
    print("downloading SHA256SUMS.txt ...")
    download_url(sums_url, sums_path)
    if not verify_archive(archive_path, sums_path):
        for p in (archive_path, sums_path):
            try:
                os.remove(p)
            except OSError:
                pass
        print("REFUSED: bad checksum — files deleted, nothing extracted.")
        return False
    os.makedirs(VEALR_DIR, exist_ok=True)
    print(f"extracting to {VEALR_DIR} ...")
    if name.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as t:
            t.extractall(VEALR_DIR)
    else:
        with zipfile.ZipFile(archive_path) as z:
            z.extractall(VEALR_DIR)
    if not sys.platform.startswith("win"):
        try:
            os.chmod(locate_binary(miner_name()), 0o755)
        except OSError:
            pass
    print(f"OK: {miner_name()} ready at {miner_path()}")
    return True


def gpu_info():
    """Return dict(name, driver, vram_mb, util, temp) or None if no NVIDIA GPU."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,"
             "utilization.gpu,temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 5:
        return None
    return {"name": parts[0], "driver": parts[1], "vram": parts[2],
            "util": parts[3], "temp": parts[4]}


def check_internet(url, timeout=10):
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "vearl-miner-wrapper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except Exception:
        return False


def do_check():
    """Preflight. Exit 1 when there is no GPU — warn, never fake it."""
    failures, warnings = [], []
    print(f"OS      : {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"Python  : {platform.python_version()}")
    print(f"Release : v{VERSION}  (official repo: github.com/{REPO})")
    if platform.system() not in ("Linux", "Windows"):
        warnings.append("untested OS — Linux and Windows are the supported ones")
    if platform.machine().lower() not in ("x86_64", "amd64"):
        failures.append(f"unsupported CPU arch: {platform.machine()} (need x86_64)")
    if sys.platform.startswith("linux"):
        libc, ver = platform.libc_ver()
        print(f"glibc   : {libc} {ver} (need >= 2.35)")
        if ver and tuple(int(x) for x in ver.split(".")[:2]) < (2, 35):
            failures.append(f"glibc {ver} too old (need >= 2.35, e.g. Ubuntu 22.04+)")
    free = shutil.disk_usage(BASE_DIR).free // (1 << 20)
    print(f"Disk free: {free} MB (need >= 500 MB)")
    if free < 500:
        failures.append("less than 500 MB free disk")
    gpu = gpu_info()
    if gpu is None:
        print("GPU     : NONE detected (no nvidia-smi / no NVIDIA GPU)")
        failures.append("no NVIDIA GPU — Vearl needs Ampere+ (RTX 30/40/50, CC 8.0+); "
                        "CPU-only mining is 'very slowly' / testing only")
    else:
        print(f"GPU     : {gpu['name']} | driver {gpu['driver']} | VRAM {gpu['vram']} "
              f"| util {gpu['util']} | temp {gpu['temp']}")
        try:
            major = int(gpu["driver"].split(".")[0])
            need = 580 if sys.platform.startswith("win") else 575
            if major < need:
                warnings.append(f"driver {gpu['driver']} older than recommended {need}+")
        except ValueError:
            pass
    for label, url in (("vearl.org", "https://vearl.org/"),
                       ("github.com", "https://github.com/")):
        ok = check_internet(url)
        print(f"Internet: {label} {'OK' if ok else 'UNREACHABLE'}")
        if not ok:
            failures.append(f"cannot reach {url}")
    print(f"Miner   : {'present at ' + miner_path() if os.path.exists(miner_path()) else 'not downloaded yet (run: python3 run.py --download)'}")
    for w in warnings:
        print(f"WARN: {w}")
    if failures:
        print()
        for f in failures:
            print(f"FAIL: {f}")
        print("Result: NOT READY — fix the FAIL lines above. Do not mine without a GPU.")
        return 1
    print("Result: PASS — ready to mine (GPU present).")
    return 0


def get_address():
    """Public vrl1... address only. Never reads seed words / private keys."""
    if os.path.exists(wallet_path()):
        try:
            with open(wallet_path(), encoding="utf-8") as f:
                data = json.load(f)
            addr = data.get("address") or data.get("miner_address")
            if isinstance(addr, str) and addr.startswith("vrl1"):
                return addr
        except Exception:
            pass
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.findall(r"vrl1[0-9a-z]+", text)
            if m:
                return m[0]
        except OSError:
            pass
    return None


def fetch_leaderboard():
    """Live points from the official leaderboard data file (verified 2026-11-10).
    Returns (row_for_address_or_None, totals_dict_or_None)."""
    if os.path.exists(CACHE_FILE):
        try:
            if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
                with open(CACHE_FILE, encoding="utf-8") as f:
                    return json.load(f), None
        except Exception:
            pass
    try:
        req = urllib.request.Request(LEADERBOARD_URL,
                                     headers={"User-Agent": "vearl-miner-wrapper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass
        return data, None
    except Exception as e:
        return None, str(e)


def leaderboard_row(address):
    """(row, totals, error) for one address, or (None, totals, error)."""
    data, err = fetch_leaderboard()
    if data is None:
        return None, None, err or "leaderboard unreachable"
    totals = data.get("totals", {})
    if not address:
        return None, totals, None
    for row in data.get("miners", []):
        if row.get("address") == address:
            return row, totals, None
    return None, totals, None


def last_log_lines(n=20):
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return [safe_line(l).rstrip("\n") for l in lines[-n:]]
    except OSError:
        return []


def miner_running():
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    return False


def run_miner(address=None):
    """Run the official miner with auto-restart. Never logs secrets."""
    if not os.path.exists(miner_path()):
        print("Miner not found — downloading official release first ...")
        if not do_download():
            return 1
    cmd = [miner_path()]
    if address:
        cmd += ["--address", address]
        print(f"Mining to address {address} (pool mode, no wallet wizard needed).")
    print(f"Starting official miner: {miner_path()}")
    print(f"Log: {LOG_FILE}  (Ctrl-C to stop cleanly)")
    print("First run creates your wallet — write the 24 recovery words ON PAPER.")
    print("Nobody from Vearl will ever ask for them. This script never logs them.")
    log = open(LOG_FILE, "a", encoding="utf-8")
    attempt = 0
    try:
        while attempt <= MAX_RESTARTS:
            proc = subprocess.Popen(
                cmd, cwd=miner_dir(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1)
            with open(PID_FILE, "w") as f:
                f.write(str(proc.pid))
            print(f"[run] miner started (pid {proc.pid}, attempt {attempt + 1})")
            try:
                for raw in proc.stdout:
                    line = safe_line(raw)
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log.write(line)
                    log.flush()
            except KeyboardInterrupt:
                raise
            code = proc.wait()
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            if code == 0:
                print("[run] miner exited cleanly (code 0). Not restarting.")
                return 0
            attempt += 1
            if attempt > MAX_RESTARTS:
                print(f"[run] miner exited code {code} after {MAX_RESTARTS} restarts. Giving up.")
                return 1
            wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            print(f"[run] miner exited code {code}. Restart {attempt}/{MAX_RESTARTS} in {wait}s ...")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\n[run] Ctrl-C — stopping miner ...")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        print("[run] stopped. Your wallet stays in the vearl/ folder — back up the 24 words on paper.")
        return 130
    finally:
        log.close()
    return 0


def do_register(contact, print_only=False):
    wallet_bin = locate_binary(
        "vearl-wallet.exe" if sys.platform.startswith("win") else "vearl-wallet")
    if not os.path.exists(wallet_path()):
        print("No wallet yet — run the miner once first (python3 run.py),")
        print("write down the 24 recovery words on paper, then come back.")
        print("Manual command once the wallet exists:")
        print(f'  vearl-wallet register --file wallet.json --contact "{contact}"')
        return 1
    if not os.path.exists(wallet_bin):
        print(f"vearl-wallet binary not found at {wallet_bin}")
        print("Manual command (run inside the vearl/ folder):")
        print(f'  vearl-wallet register --file wallet.json --contact "{contact}"')
        return 1
    cmd = [wallet_bin, "register", "--file", wallet_path(), "--contact", contact]
    if print_only:
        cmd.append("--print-only")
    print("Running the OFFICIAL registration command (it signs + emails register@vearl.org):")
    print("  " + " ".join(cmd))
    print("Check the 3 lines it shows (address, message, signature) before confirming.")
    return subprocess.run(cmd, cwd=VEALR_DIR).returncode


# ---------------------------------------------------------------- dashboard auth ---
# Password dashboard TIDAK PERNAH disimpan sebagai plaintext — di variabel
# maupun di file. Yang disimpan di .dashboard-auth.json (mode 0600) hanya:
# salt acak + hash PBKDF2-HMAC-SHA256 dari password, dan sebuah secret acak
# untuk menandatangani cookie sesi (HMAC). Password yang benar hanya ada
# di memori selama sesi login berjalan (untuk membuka vault seed) dan
# tidak pernah ditulis ke log, file, atau /api.json.
# Lupa password = tidak ada pemulihan. Dashboard terkunci dan vault seed
# tidak bisa dibuka lagi — tulis password di tempat aman, seperti seed.
# Tidak ada fitur ganti password.

_SESSIONS = {}  # token -> {"expiry": float, "password": str} (memori saja)
_SESSIONS_LOCK = threading.Lock()


def _auth_load():
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _auth_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt, AUTH_ITERATIONS, dklen=32)


def auth_exists():
    return os.path.exists(AUTH_FILE)


def auth_register(password):
    """Buat password pertama. Returns (ok, message)."""
    if auth_exists():
        return False, "Password sudah dibuat — silakan masuk."
    if len(password) < 8:
        return False, "Password minimal 8 karakter."
    salt = os.urandom(16)
    secret = os.urandom(32)
    payload = json.dumps({
        "salt": salt.hex(),
        "hash": _auth_hash(password, salt).hex(),
        "secret": secret.hex(),
        "iterations": AUTH_ITERATIONS,
    })
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass
    return True, "Password dibuat. Simpan di tempat aman — tidak bisa dipulihkan."


def auth_verify(password):
    """True jika password cocok dengan hash yang tersimpan."""
    data = _auth_load()
    if data is None:
        return False
    try:
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["hash"])
        iters = int(data.get("iterations") or AUTH_ITERATIONS)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, iters, dklen=32)
    return hmac.compare_digest(actual, expected)


def _session_sign(rand, secret):
    return hmac.new(secret, rand.encode("utf-8"), hashlib.sha256).hexdigest()


def session_create(password):
    """Buat cookie sesi: token acak + HMAC dari server secret. Returns token."""
    data = _auth_load()
    if data is None:
        return None
    try:
        secret = bytes.fromhex(data["secret"])
    except Exception:
        return None
    rand = secrets.token_hex(32)
    token = f"{rand}.{_session_sign(rand, secret)}"
    with _SESSIONS_LOCK:
        _SESSIONS[token] = {"expiry": time.time() + SESSION_TTL,
                            "password": password}
    return token


def session_password(token):
    """Password sesi (memori saja) jika token valid dan belum kedaluwarsa."""
    if not token:
        return None
    data = _auth_load()
    if data is None:
        return None
    try:
        secret = bytes.fromhex(data["secret"])
        rand, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _session_sign(rand, secret)):
            return None
    except Exception:
        return None
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(token)
        if entry is None:
            return None
        if entry["expiry"] < time.time():
            del _SESSIONS[token]
            return None
        return entry["password"]


def session_destroy(token):
    if token:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(token, None)


# ---------------------------------------------------------------- seed vault ---
# Opt-in encrypted local vault for the 24 recovery words. Stdlib only:
# PBKDF2-HMAC-SHA256 derives a key from the DASHBOARD LOGIN password (the
# vault has NO separate passphrase anymore), a SHA-256 counter-mode stream
# cipher encrypts the words, and an HMAC tag proves the password was right.
# The words are NEVER logged, NEVER in /api.json, and are shown on the
# Profile page only after login, only when a vault exists.
# WARNING: anyone with dashboard access + the login password can steal
# the wallet. And if the login password is forgotten, the vault cannot
# be opened again — there is no recovery.

def _vault_key(passphrase, salt):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 200_000, dklen=32)


def _vault_stream(key, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:length])


def _vault_xor(data, key):
    return bytes(b ^ k for b, k in zip(data, _vault_stream(key, len(data))))


def vault_exists():
    return os.path.exists(VAULT_FILE)


def vault_save(words, password):
    """Store 24 words encrypted with a key from the login password.
    Returns (ok, message). Never logs the words."""
    words = " ".join(str(words).split()).lower()
    parts = words.split(" ")
    if len(parts) != 24 or not all(p.isalpha() for p in parts):
        return False, "Harus tepat 24 kata (huruf saja, dipisah spasi)."
    if len(password) < 8:
        return False, "Password dashboard tidak valid."
    salt = os.urandom(16)
    key = _vault_key(password, salt)
    ct = _vault_xor(words.encode("utf-8"), key)
    tag = hmac.new(key, salt + ct, hashlib.sha256).hexdigest()
    payload = json.dumps({"salt": salt.hex(), "ct": ct.hex(), "tag": tag})
    fd = os.open(VAULT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(VAULT_FILE, 0o600)
    except OSError:
        pass
    return True, "Vault tersimpan (seed.vault, mode 0600). Kata-kata terenkripsi dengan password dashboard — lupa password = vault tidak bisa dibuka lagi."


def vault_reveal(password):
    """Return the 24 words if the login password is right, else None.
    Never logs. A vault made with the old separate passphrase (v2) will
    not open with the login password — that is expected, save it again."""
    try:
        with open(VAULT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        salt = bytes.fromhex(data["salt"])
        ct = bytes.fromhex(data["ct"])
        key = _vault_key(password, salt)
        if not hmac.compare_digest(
                hmac.new(key, salt + ct, hashlib.sha256).hexdigest(), data["tag"]):
            return None
        return _vault_xor(ct, key).decode("utf-8")
    except Exception:
        return None


def vault_delete():
    """Delete the vault. Caller must already be logged in."""
    try:
        os.remove(VAULT_FILE)
    except OSError as e:
        return False, f"Tidak bisa menghapus vault: {e}"
    return True, "Vault terhapus. Kata-kata tidak lagi tersimpan di sini."


def check_registered(address):
    """(registered_bool_or_None, note). None = could not verify."""
    if not address:
        return None, "Belum ada alamat wallet — jalankan miner sekali dulu."
    try:
        req = urllib.request.Request(REGISTERED_URL,
                                     headers={"User-Agent": "vearl-miner-wrapper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except Exception as e:
        return None, f"Tidak bisa mengambil {REGISTERED_URL}: {e}"
    if isinstance(data, list):
        return address in data, "Sudah memeriksa daftar terdaftar resmi."
    if isinstance(data, dict):
        for key in ("registered", "addresses", "miners"):
            if isinstance(data.get(key), list):
                return address in data[key], "Sudah memeriksa daftar terdaftar resmi."
        return address in data, "Sudah memeriksa daftar terdaftar resmi."
    return None, "Format daftar terdaftar tidak terduga."


def run_action(func, *args, **kwargs):
    """Run a CLI action (do_check / do_download / do_register) capturing
    its printed output. Returns (exit_code, output_text)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = func(*args, **kwargs)
    except SystemExit as e:
        code = int(e.code or 0)
    except Exception as e:
        buf.write(f"\nERROR: {e}\n")
        code = 1
    return code, buf.getvalue()


# ---------------------------------------------------------------- dashboard ---
# Palette: deep dark + teal / emerald / amber. Responsive, mobile-first.

DASHBOARD_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(640px 320px at 50% -100px,rgba(0,199,190,.16),transparent 70%),#000;color:#f5f5f7;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Helvetica Neue","Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}
.wrap{max-width:680px;margin:0 auto;padding:0 16px 48px}
header{position:sticky;top:0;z-index:10;background:rgba(0,0,0,.62);-webkit-backdrop-filter:blur(20px) saturate(180%);backdrop-filter:blur(20px) saturate(180%);border-bottom:.5px solid rgba(255,255,255,.09);margin:0 -16px;padding:18px 16px 12px}
.header-row{display:flex;align-items:flex-end;justify-content:space-between;gap:12px}
h1{font-size:34px;font-weight:800;letter-spacing:-.8px;line-height:1.1}
h1 span{color:#00c7be}
.sub{color:#98989d;font-size:13px;margin-top:4px}
.badge{flex-shrink:0;font-size:13px;font-weight:700;padding:7px 13px;border-radius:999px}
.badge.on{background:rgba(48,209,88,.16);color:#30d158}
.badge.off{background:rgba(255,159,10,.16);color:#ff9f0a}
.controls{background:rgba(28,28,30,.72);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:.5px solid rgba(255,255,255,.09);border-radius:22px;padding:16px 18px;margin:16px 0 14px}
.controls .row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:6px 0}
.controls .row+.row{border-top:.5px solid rgba(255,255,255,.08);margin-top:8px;padding-top:14px}
.controls label{font-size:15px;font-weight:600}
.controls .hint{font-size:12px;color:#98989d;margin-top:2px}
.segmented{display:flex;background:rgba(118,118,128,.24);border-radius:10px;padding:2px;min-width:168px}
.segmented a{flex:1;text-align:center;text-decoration:none;font-size:13px;font-weight:600;color:#f5f5f7;padding:7px 0;border-radius:8px}
.segmented a.sel{background:#48484a;color:#fff;box-shadow:0 1px 4px rgba(0,0,0,.4)}
.switch{display:inline-block;width:51px;height:31px;border-radius:999px;position:relative;flex-shrink:0;text-decoration:none}
.switch.on{background:#30d158}
.switch.off{background:rgba(120,120,128,.32)}
.switch::after{content:"";position:absolute;top:2px;width:27px;height:27px;border-radius:50%;background:#fff;box-shadow:0 2px 6px rgba(0,0,0,.35)}
.switch.on::after{right:2px}
.switch.off::after{left:2px}
.grid{display:grid;grid-template-columns:1fr;gap:14px;margin-bottom:14px}
@media(min-width:640px){.grid{grid-template-columns:1fr 1fr}.grid .wide{grid-column:1/-1}}
.card{background:rgba(28,28,30,.72);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:.5px solid rgba(255,255,255,.09);border-radius:22px;padding:20px}
.card h2{font-size:13px;font-weight:600;color:#98989d;margin-bottom:10px}
.big{font-size:34px;font-weight:800;letter-spacing:-.8px;color:#00c7be}
.big.amber{color:#ff9f0a}
.big.green{color:#30d158}
.gpu-name{font-size:19px;font-weight:700;letter-spacing:-.3px;line-height:1.3}
.small{font-size:14px;color:#d1d1d6;line-height:1.55;margin-top:6px;word-break:break-word}
.muted{color:#98989d;font-size:12.5px;margin-top:8px;line-height:1.5}
.bar{height:8px;border-radius:999px;background:rgba(255,255,255,.12);margin-top:12px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#00c7be,#30b0c7)}
.addr{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;color:#30b0c7;word-break:break-all;line-height:1.5}
.log{background:rgba(28,28,30,.72);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:.5px solid rgba(255,255,255,.09);border-radius:22px;padding:20px}
.log h2{font-size:13px;font-weight:600;color:#98989d;margin-bottom:12px}
pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.65;color:#9be8d8;white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}
footer{margin-top:22px;color:#98989d;font-size:12.5px;line-height:1.7}
footer a{color:#30b0c7;text-decoration:none}
.burger{flex-shrink:0;width:44px;height:44px;border-radius:14px;border:.5px solid rgba(255,255,255,.12);background:rgba(28,28,30,.72);color:#f5f5f7;font-size:20px;line-height:1;cursor:pointer}
.nav{position:fixed;top:0;right:0;bottom:0;width:min(300px,84vw);background:rgba(28,28,30,.97);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border-left:.5px solid rgba(255,255,255,.12);padding:20px;z-index:50;display:none}
.nav.open{display:block}
.nav h2{font-size:13px;color:#98989d;margin:8px 0 14px}
.nav a{display:block;text-decoration:none;color:#f5f5f7;font-size:17px;font-weight:600;padding:14px 16px;border-radius:16px;margin-bottom:8px;background:rgba(118,118,128,.18)}
.nav a.sel{background:rgba(0,199,190,.18);color:#00c7be}
.nav button.navlink{display:block;width:100%;text-align:left;border:none;cursor:pointer;text-decoration:none;color:#ff9f0a;font-size:17px;font-weight:600;padding:14px 16px;border-radius:16px;margin-bottom:8px;background:rgba(118,118,128,.18);font-family:inherit}
.auth{max-width:480px;margin:48px auto 0}
.auth h1{font-size:28px;margin-bottom:8px}
.auth .lead{font-size:15px;color:#d1d1d6;line-height:1.6}
.nav .close{float:right;background:none;border:none;color:#98989d;font-size:22px;cursor:pointer}
.btn{display:inline-block;background:#00c7be;color:#000;font-weight:700;font-size:15px;border:none;border-radius:14px;padding:12px 18px;cursor:pointer;text-decoration:none}
.btn.amber{background:#ff9f0a}
.btn.ghost{background:rgba(118,118,128,.24);color:#f5f5f7}
.btnrow{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
form label{display:block;font-size:14px;font-weight:600;margin:14px 0 6px}
input[type=text],input[type=password],textarea{width:100%;background:rgba(118,118,128,.18);border:.5px solid rgba(255,255,255,.12);border-radius:14px;color:#f5f5f7;font-size:15px;padding:12px 14px;font-family:inherit}
textarea{min-height:90px;resize:vertical}
.check{display:flex;align-items:center;gap:10px;margin-top:14px;font-size:14px}
.warn{background:rgba(255,159,10,.12);border:.5px solid rgba(255,159,10,.4);border-radius:16px;padding:14px 16px;font-size:14px;line-height:1.6;color:#ffd08a;margin-top:12px}
.result{margin-top:16px;background:#000;border:.5px solid rgba(255,255,255,.12);border-radius:16px;padding:16px}
.result h3{font-size:13px;color:#98989d;margin-bottom:10px}
.result pre{max-height:300px}
.ok{color:#30d158;font-weight:700}
.bad{color:#ff9f0a;font-weight:700}
.copy{font-size:13px;font-weight:600;color:#30b0c7;background:none;border:none;cursor:pointer;padding:4px 0}
.seed{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:14px;line-height:1.8;color:#30d158;word-break:break-word}
"""




def dashboard_data(lines=20):
    address = get_address()
    row, totals, err = leaderboard_row(address)
    gpu = gpu_info()
    log_age = None
    if os.path.exists(LOG_FILE):
        log_age = int(time.time() - os.path.getmtime(LOG_FILE))
    return {
        "running": miner_running(),
        "address": address,
        "gpu": gpu,
        "points": row,
        "totals": totals,
        "leaderboard_error": err,
        "log": last_log_lines(lines),
        "log_age_s": log_age,
        "version": VERSION,
    }


def _gpu_util_pct(util_str):
    m = re.search(r"(\d+)", util_str or "")
    return max(0, min(100, int(m.group(1)))) if m else 0


NAV_PAGES = (("home", "Beranda"), ("profile", "Profil / Akun"), ("setup", "Pengaturan"))


def _esc(text):
    import html as _html
    return _html.escape(str(text))


def render_shell(page, body, d, refresh=True, lines=20):
    import html as _html
    running = d["running"]
    badge = ('<span class="badge on">● Menambang</span>' if running
             else '<span class="badge off">● Berhenti</span>')
    meta = '<meta http-equiv="refresh" content="15">' if (refresh and page == "home") else ""
    nav_links = "".join(
        f'<a href="/?page={p}" class="{"sel" if p == page else ""}">{label}</a>'
        for p, label in NAV_PAGES)
    title = {"home": "Vearl Miner", "profile": "Profil / Akun", "setup": "Pengaturan"}.get(page, "Vearl Miner")
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{meta}
<title>{title} — Vearl Miner</title>
<style>{DASHBOARD_CSS}</style></head>
<body><div class="wrap">
<header><div class="header-row"><div><h1>{title}</h1>
<div class="sub">Testnet 1 · miner resmi v{d["version"]}</div></div>
{badge}
<button class="burger" onclick="document.getElementById('nav').classList.toggle('open')" aria-label="Menu">☰</button>
</div></header>
<nav class="nav" id="nav"><button class="close" onclick="document.getElementById('nav').classList.remove('open')" aria-label="Close">✕</button>
<h2>MENU</h2>{nav_links}
<form method="post" action="/auth/logout"><button class="navlink" type="submit">Keluar</button></form>
<div class="muted">Hanya lokal — http://127.0.0.1. Sesi berlaku 12 jam.</div></nav>
{body}
<footer>Poin diambil live dari file data leaderboard resmi
<a href="{LEADERBOARD_URL}">{LEADERBOARD_URL}</a>
(cek manual di <a href="{LEADERBOARD_PAGE}">vearl.org/leaderboard</a>).
Token testnet tidak ada nilainya. Jangan pernah bagikan 24 kata pemulihanmu ke siapa pun —
halaman Profil hanya menampilkannya setelah kamu masuk dengan password, dan hanya kalau
kamu menyimpannya di vault terenkripsi.</footer>
</div>
<script>
function copyAddr(el){{var t=document.getElementById('addr');if(!t)return;
if(navigator.clipboard){{navigator.clipboard.writeText(t.textContent);el.textContent='Disalin ✓';}}
else{{var r=document.createRange();r.selectNode(t);window.getSelection().removeAllRanges();window.getSelection().addRange(r);document.execCommand('copy');el.textContent='Disalin ✓';}}}}
function copySeed(el){{var t=document.getElementById('seed');if(!t)return;
if(navigator.clipboard){{navigator.clipboard.writeText(t.textContent);el.textContent='Disalin ✓';}}
else{{var r=document.createRange();r.selectNode(t);window.getSelection().removeAllRanges();window.getSelection().addRange(r);document.execCommand('copy');el.textContent='Disalin ✓';}}}}
</script>
</body></html>"""


def _result_block(result):
    if not result:
        return ""
    title, code, output = result
    # NOTE: do NOT run safe_line over action output here. Action output is
    # generated by our own code (do_check / do_download / do_register /
    # vault messages) and never contains the words themselves; blanketing
    # it would hide useful errors that merely mention them (e.g. the
    # "Need exactly 24 words" validation message). Miner log lines are
    # redacted separately, in last_log_lines(), where raw miner output
    # could actually contain them.
    safe = (output or "").strip()
    status = '<span class="ok">exit 0</span>' if code == 0 else f'<span class="bad">exit {code}</span>'
    return (f'<div class="result"><h3>{_esc(title)} · {status}</h3>'
            f'<pre>{_esc(safe) if safe else "(tidak ada output)"}</pre></div>')


def render_auth(kind, error=None):
    """Halaman Buat Password (first run) / Masuk (login). Tanpa menu."""
    import html as _html
    if kind == "register":
        title = "Buat Password"
        lead = ("Ini pertama kalinya dashboard dibuka — buat password dulu. "
                "Password ini melindungi semua halaman dan vault 24 kata pemulihanmu.")
        warn = ("<b>Password ini TIDAK bisa dipulihkan.</b> Kalau lupa password, "
                "dashboard terkunci selamanya dan vault seed tidak bisa dibuka lagi. "
                "Tidak ada tombol 'lupa password'. Tulis password di tempat aman, seperti 24 kata itu.")
        fields = ('<label>Password (min. 8 karakter)</label>'
                  '<input type="password" name="password" autocomplete="new-password">'
                  '<label>Ulangi password</label>'
                  '<input type="password" name="password2" autocomplete="new-password">')
        btn = "Buat password &amp; masuk"
        action = "/auth/register"
    else:
        title = "Masuk"
        lead = "Masukkan password dashboard untuk membuka Beranda, Profil, dan Pengaturan."
        warn = ("Sesi berlaku 12 jam. Kalau lupa password, dashboard terkunci — "
                "tidak ada pemulihan. Tulis password di tempat aman.")
        fields = ('<label>Password</label>'
                  '<input type="password" name="password" autocomplete="current-password">')
        btn = "Masuk"
        action = "/auth/login"
    err = f'<div class="warn">{_html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title} — Vearl Miner</title>
<style>{DASHBOARD_CSS}</style></head>
<body><div class="wrap"><div class="auth">
<h1>Vearl <span>Miner</span></h1>
<div class="sub">Testnet 1 · miner resmi v{VERSION}</div>
<div class="card" style="margin-top:20px"><h2>{title}</h2>
<div class="lead">{lead}</div>
<div class="warn">{warn}</div>
{err}
<form method="post" action="{action}">{fields}
<div class="btnrow"><button class="btn" type="submit">{btn}</button></div></form>
</div>
<footer>Dashboard ini hanya lokal — http://127.0.0.1. Password disimpan
sebagai hash (PBKDF2), bukan plaintext, dan tidak pernah dikirim ke mana pun.</footer>
</div></div></body></html>"""


def render_home(d, lines=20, refresh=True):
    import html as _html
    refresh_q = "on" if refresh else "off"
    seg = "".join(
        f'<a href="/?page=home&lines={n}&refresh={refresh_q}" class="{"sel" if n == lines else ""}">{n}</a>'
        for n in (10, 20, 50))
    toggle = (f'<a class="switch on" href="/?page=home&lines={lines}&refresh=off" '
              f'title="Refresh otomatis nyala — ketuk untuk mematikan"></a>' if refresh else
              f'<a class="switch off" href="/?page=home&lines={lines}&refresh=on" '
              f'title="Refresh otomatis mati — ketuk untuk menyalakan"></a>')
    gpu = d["gpu"]
    if gpu:
        pct = _gpu_util_pct(gpu["util"])
        gpu_html = (f'<div class="gpu-name">{_html.escape(gpu["name"])}</div>'
                    f'<div class="small">driver {gpu["driver"]} · VRAM {gpu["vram"]} · '
                    f'{gpu["temp"]}</div>'
                    f'<div class="bar"><i style="width:{pct}%"></i></div>'
                    f'<div class="muted">Beban GPU {pct}%</div>')
    else:
        gpu_html = ('<div class="big amber">Tidak ada GPU</div>'
                    '<div class="small">Mining CPU saja hanya untuk testing dan sangat lambat. '
                    'Vearl butuh GPU NVIDIA Ampere+.</div>')
    row, totals = d["points"], d["totals"]
    if row:
        points_html = (f'<div class="big green">{row.get("points")}</div>'
                       f'<div class="small">peringkat #{row.get("rank")} · {row.get("blocks")} blok · '
                       f'{row.get("active_days")} hari aktif · bagian {row.get("share", 0) * 100:.4f}%</div>'
                       f'<div class="muted">Memenuhi syarat: {"ya" if row.get("eligible") else "belum"} '
                       f'(butuh 3+ hari dan 0,005% poin)</div>')
    elif d["leaderboard_error"]:
        points_html = ('<div class="big amber">n/a</div>'
                       f'<div class="small">Leaderboard tidak bisa dijangkau: '
                       f'{_html.escape(str(d["leaderboard_error"]))}</div>')
    elif not d["address"]:
        points_html = ('<div class="big amber">n/a</div>'
                       '<div class="small">Belum ada alamat wallet — jalankan miner sekali dulu.</div>')
    else:
        points_html = ('<div class="big amber">0 yet</div>'
                       '<div class="small">Alamat belum ada di leaderboard — blok itu undian, '
                       'bisa butuh waktu.</div>')
    addr_html = (f'<div class="addr">{_html.escape(d["address"])}</div>' if d["address"]
                 else '<div class="small">Belum ada wallet.</div>')
    totals_html = (f'Total: {totals.get("points")} poin · {totals.get("miners")} miner · '
                   f'{totals.get("eligible")} memenuhi syarat'
                   if totals else 'Total leaderboard belum tersedia saat ini.')
    log_html = _html.escape("\n".join(d["log"])) if d["log"] else "(miner.log kosong — jalankan miner dulu)"
    age = (f'update terakhir {d["log_age_s"]} dtk lalu' if d["log_age_s"] is not None
           else 'belum ada file log')
    return f"""
<div class="controls">
<div class="row"><div><label>Baris log</label>
<div class="hint">Berapa baris miner.log yang ditampilkan.</div></div>
<div class="segmented">{seg}</div></div>
<div class="row"><div><label>Refresh otomatis</label>
<div class="hint">{"Refresh setiap 15 dtk." if refresh else "Dijeda — ketuk sakelar untuk lanjut."}</div></div>
{toggle}</div>
</div>
<div class="grid">
<div class="card"><h2>GPU</h2>{gpu_html}</div>
<div class="card"><h2>Poin</h2>{points_html}</div>
<div class="card wide"><h2>Alamat kamu</h2>{addr_html}
<div class="muted">{totals_html}</div></div>
</div>
<div class="log"><h2>Baris log terakhir · {age}</h2><pre>{log_html}</pre></div>"""


def render_profile(d, result=None, password=None):
    import html as _html
    address = d["address"]
    if address:
        addr_html = (f'<div class="addr" id="addr">{_html.escape(address)}</div>'
                     f'<button class="copy" onclick="copyAddr(this)">Salin alamat</button>')
    else:
        addr_html = '<div class="small">Belum ada wallet — jalankan miner sekali dulu (halaman Pengaturan).</div>'
    row, totals = d["points"], d["totals"]
    if row:
        points_html = (f'<div class="big green">{row.get("points")}</div>'
                       f'<div class="small">peringkat #{row.get("rank")} · {row.get("blocks")} blok · '
                       f'{row.get("active_days")} hari aktif · bagian {row.get("share", 0) * 100:.4f}%</div>'
                       f'<div class="muted">Memenuhi syarat: {"ya" if row.get("eligible") else "belum"}</div>')
    elif not address:
        points_html = ('<div class="big amber">n/a</div>'
                       '<div class="small">Belum ada alamat wallet.</div>')
    else:
        points_html = ('<div class="big amber">0 yet</div>'
                       '<div class="small">Alamat belum ada di leaderboard.</div>')
    reg, note = check_registered(address)
    if reg is True:
        reg_html = '<div class="big green">Terdaftar ✓</div>'
    elif reg is False:
        reg_html = ('<div class="big amber">Belum terdaftar</div>'
                    '<div class="small">Daftar dari halaman Pengaturan — wajib untuk hadiah.</div>')
    else:
        reg_html = f'<div class="big amber">Tidak diketahui</div><div class="small">{_html.escape(note)}</div>'
    wallet_html = ('<div class="small">wallet.json ditemukan di folder miner.</div>'
                   if os.path.exists(wallet_path()) else
                   '<div class="small">Belum ada wallet.json.</div>')
    # Vault dibuka dengan password login (tidak ada passphrase terpisah
    # lagi). Sudah login = sudah terbukti punya password, jadi kata-kata
    # ditampilkan di halaman ini, dengan tombol Salin.
    if vault_exists():
        revealed = vault_reveal(password) if password else None
        if revealed:
            vault_html = (f'<div class="warn">Ini 24 kata pemulihanmu. '
                          f'Siapa pun yang melihatnya bisa mencuri walletmu — jangan pernah bagikan, '
                          f'jangan pernah tempel di mana pun kecuali di miner resmi.</div>'
                          f'<div class="seed" id="seed">{_html.escape(revealed)}</div>'
                          f'<button class="copy" onclick="copySeed(this)">Salin</button>'
                          f'<form method="post" action="/action/vault-delete?page=profile">'
                          f'<div class="btnrow"><button class="btn ghost" type="submit">Hapus vault</button></div></form>')
        else:
            vault_html = (f'<div class="warn">Ada vault terenkripsi (seed.vault) di mesin ini, '
                          f'tapi tidak bisa dibuka dengan password ini. Vault ini mungkin dibuat '
                          f'dengan passphrase lama (sebelum auth) — hapus, lalu simpan lagi dari halaman ini.</div>'
                          f'<form method="post" action="/action/vault-delete?page=profile">'
                          f'<div class="btnrow"><button class="btn ghost" type="submit">Hapus vault</button></div></form>')
    else:
        vault_html = (f'<div class="warn"><b>24 kata pemulihanmu TIDAK tersimpan di sini.</b> '
                      f'Kata-kata itu hanya ditampilkan sekali, saat wallet dibuat — tulis di kertas. '
                      f'Dashboard ini tidak memilikinya dan tidak bisa memulihkannya.</div>'
                      f'<p class="small" style="margin-top:12px">Opsional: simpan di sini, terenkripsi '
                      f'dengan password dashboardmu. Siapa pun yang punya akses dashboard <b>dan</b> '
                      f'passwordmu bisa mencuri wallet — kertas tetap cadangan paling aman. '
                      f'Lupa password = vault tidak bisa dibuka lagi.</p>'
                      f'<form method="post" action="/action/vault-save?page=profile">'
                      f'<label>24 kata pemulihan (tempel sekali, dipisah spasi)</label>'
                      f'<textarea name="words" autocomplete="off" spellcheck="false"></textarea>'
                      f'<div class="btnrow"><button class="btn" type="submit">Simpan vault terenkripsi</button></div></form>')
    return f"""
{_result_block(result)}
<div class="grid">
<div class="card wide"><h2>Alamat publik</h2>{addr_html}</div>
<div class="card"><h2>Poin</h2>{points_html}</div>
<div class="card"><h2>Pendaftaran hadiah</h2>{reg_html}</div>
<div class="card wide"><h2>File wallet</h2>{wallet_html}</div>
<div class="card wide"><h2>24 kata pemulihan — vault terenkripsi (opsional)</h2>{vault_html}</div>
</div>"""


def render_setup(d, result=None):
    return f"""
{_result_block(result)}
<div class="grid">
<div class="card wide"><h2>1 · Pemeriksaan awal</h2>
<div class="small">Sama seperti <b>python3 run.py --check</b>: OS, GPU, driver, disk, internet. Gagal dengan jujur kalau tidak ada GPU — tidak pernah pura-pura lolos.</div>
<form method="post" action="/action/check?page=setup"><div class="btnrow"><button class="btn" type="submit">Jalankan pemeriksaan awal</button></div></form></div>
<div class="card wide"><h2>2 · Unduh + verifikasi miner resmi</h2>
<div class="small">Mengunduh v{d["version"]} dari github.com/vearl-verified-compute/vearl dan memverifikasi SHA-256-nya. Ditolak di platform yang dilarang kecuali I_UNDERSTAND_BANNED_PLATFORM=1.</div>
<form method="post" action="/action/download?page=setup"><div class="btnrow"><button class="btn" type="submit">Unduh + verifikasi rc27</button></div></form></div>
<div class="card wide"><h2>3 · Buat wallet / jalankan miner</h2>
<div class="small">Jalan pertama wajib di terminal (<b>python3 run.py</b>) — wizard wallet itu interaktif dan menampilkan 24 kata pemulihanmu <b>sekali, di kertas</b>. Kalau wallet sudah ada, tombol ini menjalankan miner resmi di background, log-nya di miner.log.</div>
<form method="post" action="/action/start-miner?page=setup"><div class="btnrow"><button class="btn" type="submit">Jalankan miner di background</button></div></form></div>
<div class="card wide"><h2>4 · Daftar untuk hadiah</h2>
<div class="small">Sama seperti <b>python3 run.py --register</b>: wallet resmi menandatangani dan mengirim pendaftaranmu ke register@vearl.org. Wajib untuk hadiah (3+ hari aktif, 0,005% poin).</div>
<form method="post" action="/action/register?page=setup">
<label>Kontak (nama Discord atau e-mail)</label>
<input type="text" name="contact" autocomplete="off">
<label class="check"><input type="checkbox" name="print_only"> Hanya tampil — tampilkan baris yang ditandatangani, jangan kirim</label>
<div class="btnrow"><button class="btn" type="submit">Daftar untuk hadiah</button></div></form></div>
<div class="card wide"><h2>5 · Cek status pendaftaran</h2>
<div class="small">Memeriksa alamatmu di daftar resmi vearl.org/rewards/registered.json.</div>
<form method="post" action="/action/check-registration?page=setup"><div class="btnrow"><button class="btn ghost" type="submit">Cek pendaftaran</button></div></form></div>
</div>"""


def _action_start_miner():
    """Background-start the official miner. Only when a wallet already
    exists (so no interactive wizard / no new 24 words are involved)."""
    if not banned_platform_gate("run the miner"):
        return 1
    if not os.path.exists(miner_path()):
        print("Miner belum diunduh — pakai kartu 2 (Unduh) dulu.")
        return 1
    if not os.path.exists(wallet_path()):
        print("Belum ada wallet — jalan pertama harus interaktif di terminal:")
        print("  python3 run.py")
        print("Tulis 24 kata pemulihan di kertas, lalu kembali ke sini.")
        return 1
    log = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen([miner_path()], cwd=miner_dir(),
                            stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    print(f"OK: miner sudah jalan di background (pid {proc.pid}). Log: {LOG_FILE}")
    print("Cek Beranda untuk statusnya. Hentikan dari terminal (kill pid-nya).")
    return 0


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, content_type="text/html; charset=utf-8", code=200,
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("vearl_session="):
                return part.split("=", 1)[1].strip()
        return None

    def _session_password(self):
        return session_password(self._cookie_token())

    def _redirect(self, location, cookie=None):
        headers = {"Location": location}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        self._send("", "text/plain", code=302, extra_headers=headers)

    def _session_cookie(self, token):
        if token is None:
            return "vearl_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
        return (f"vearl_session={token}; HttpOnly; SameSite=Lax; Path=/; "
                f"Max-Age={SESSION_TTL}")

    def _read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        from urllib.parse import parse_qs
        return parse_qs(raw)

    def _page(self, page, lines=20, refresh=True, result=None, password=None):
        d = dashboard_data(lines)
        if page == "profile":
            body = render_profile(d, result=result, password=password)
        elif page == "setup":
            body = render_setup(d, result=result)
        else:
            page = "home"
            body = render_home(d, lines, refresh)
        return render_shell(page, body, d, refresh=refresh, lines=lines)

    def _auth_gate(self):
        """(password_or_None, redirect_target_or_None).
        redirect_target 'register' = belum ada password, 'login' = belum masuk."""
        if not auth_exists():
            return None, "register"
        password = self._session_password()
        if password is None:
            return None, "login"
        return password, None

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        # Halaman auth selalu boleh diakses (tanpa login).
        if parsed.path == "/register":
            if auth_exists():
                self._redirect("/login")
            else:
                self._send(render_auth("register"))
            return
        if parsed.path == "/login":
            if not auth_exists():
                self._redirect("/register")
            else:
                self._send(render_auth("login"))
            return
        password, target = self._auth_gate()
        if target:
            self._redirect(f"/{target}")
            return
        try:
            lines = int(q.get("lines", ["20"])[0])
        except ValueError:
            lines = 20
        lines = lines if lines in (10, 20, 50) else 20
        refresh = q.get("refresh", ["on"])[0] != "off"
        page = q.get("page", ["home"])[0]
        if page not in ("home", "profile", "setup"):
            page = "home"
        if parsed.path in ("/api.json", "/api"):
            d = dashboard_data(lines)
            self._send(json.dumps(d, default=str), "application/json")
        else:
            self._send(self._page(page, lines, refresh, password=password))

    def do_POST(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        form = self._read_form()
        field = lambda name: (form.get(name, [""])[0] or "").strip()
        # --- endpoint auth (publik, tapi dibatasi keadaan) ---
        if parsed.path == "/auth/register":
            if auth_exists():
                self._redirect("/login")
                return
            pw, pw2 = field("password"), field("password2")
            if pw != pw2:
                self._send(render_auth("register", "Password tidak cocok."))
                return
            ok, msg = auth_register(pw)
            if not ok:
                self._send(render_auth("register", msg))
                return
            token = session_create(pw)
            self._redirect("/?page=home", cookie=self._session_cookie(token))
            return
        if parsed.path == "/auth/login":
            if not auth_exists():
                self._redirect("/register")
                return
            time.sleep(0.5)  # perlambat tebakan password
            pw = field("password")
            if not auth_verify(pw):
                self._send(render_auth("login", "Password salah."))
                return
            token = session_create(pw)
            self._redirect("/?page=home", cookie=self._session_cookie(token))
            return
        if parsed.path == "/auth/logout":
            session_destroy(self._cookie_token())
            self._redirect("/login", cookie=self._session_cookie(None))
            return
        # --- semua /action/* butuh login ---
        password, target = self._auth_gate()
        if target:
            if parsed.path.startswith("/action/"):
                self._send(json.dumps({"error": "login required"}),
                           "application/json", code=401)
            else:
                self._redirect(f"/{target}")
            return
        page = q.get("page", ["setup"])[0]
        if page not in ("home", "profile", "setup"):
            page = "setup"
        action = parsed.path.rsplit("/", 1)[-1] if parsed.path.startswith("/action/") else ""
        result = None
        if action == "check":
            code, out = run_action(do_check)
            result = ("Pemeriksaan awal", code, out)
            page = "setup"
        elif action == "download":
            def _dl():
                if not banned_platform_gate("download the miner"):
                    return 1
                return 0 if do_download() else 1
            code, out = run_action(_dl)
            result = ("Unduh + verifikasi", code, out)
            page = "setup"
        elif action == "start-miner":
            code, out = run_action(_action_start_miner)
            result = ("Jalankan miner", code, out)
            page = "setup"
        elif action == "register":
            contact = field("contact")
            if not contact:
                result = ("Daftar untuk hadiah", 1, "Masukkan kontak (nama Discord atau e-mail) dulu.")
            else:
                code, out = run_action(do_register, contact,
                                       print_only=(field("print_only") == "on"))
                result = ("Daftar untuk hadiah", code, out)
            page = "setup"
        elif action == "check-registration":
            address = get_address()
            reg, note = check_registered(address)
            if reg is True:
                result = ("Status pendaftaran", 0, f"{address} SUDAH terdaftar.\n{note}")
            elif reg is False:
                result = ("Status pendaftaran", 1, f"{address} belum terdaftar.\n{note}")
            else:
                result = ("Status pendaftaran", 1, note)
            page = "setup"
        elif action == "vault-save":
            ok, msg = vault_save(field("words"), password)
            result = ("Simpan vault", 0 if ok else 1, msg)
            page = "profile"
        elif action == "vault-delete":
            ok, msg = vault_delete()
            result = ("Hapus vault", 0 if ok else 1, msg)
            page = "profile"
        else:
            self._send("Aksi tidak dikenal.", "text/plain", code=404)
            return
        self._send(self._page(page, result=result, password=password))


def render_dashboard(d, lines=20, refresh=True):
    """Back-compat wrapper: the Home page as a full document."""
    return render_shell("home", render_home(d, lines, refresh), d,
                        refresh=refresh, lines=lines)




def do_dashboard(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Dashboard: http://127.0.0.1:{port}/  (local only, Ctrl-C to stop)")
    print("JSON status: http://127.0.0.1:%d/api.json" % port)
    if not auth_exists():
        print("First run: the dashboard will ask you to create a password.")
        print("That password is stored as a salted hash only — never plaintext —")
        print("and it cannot be recovered if forgotten. Write it down somewhere safe.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Vearl testnet miner wrapper — runs the OFFICIAL Vearl miner binary.")
    p.add_argument("--check", action="store_true", help="preflight: OS, GPU, driver, disk, internet")
    p.add_argument("--download", action="store_true", help="download + SHA-256 verify + extract official release")
    p.add_argument("--dry-run", action="store_true", help="with --download: print URLs only, download nothing")
    p.add_argument("--dashboard", action="store_true", help="serve local status page (127.0.0.1:8080)")
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8080")),
                   help="dashboard port (default 8080, or DASHBOARD_PORT)")
    p.add_argument("--register", action="store_true", help="register your address for rewards (official wallet)")
    p.add_argument("--contact", default="", help="Discord name or e-mail for --register")
    p.add_argument("--address", default="", help="mine to this vrl1... address (pool mode, skips the wallet wizard)")
    p.add_argument("--print-only", action="store_true", help="with --register: show the signed lines, do not send")
    args = p.parse_args(argv)
    platform_name = detect_banned_platform()
    # --check / --dashboard / --register only warn; download / run are gated.
    if platform_name is not None and (args.check or args.dashboard or args.register):
        warn_banned_platform(platform_name)
    if args.check:
        return do_check()
    if args.download:
        if not banned_platform_gate("download the miner"):
            return 1
        return 0 if do_download(dry_run=args.dry_run) else 1
    if args.dashboard:
        return do_dashboard(args.port)
    if args.register:
        if not args.contact:
            p.error("--register needs --contact <discord-name-or-email>")
        return do_register(args.contact, print_only=args.print_only)
    if not banned_platform_gate("run the miner"):
        return 1
    return run_miner(address=args.address or None)


if __name__ == "__main__":
    sys.exit(main())
