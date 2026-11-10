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
Only the public vrl1... address is ever shown. Testnet tokens have no value.

Env: VEALR_VERSION (default 0.1.0-rc27, VEARL_VERSION also accepted),
     VEALR_DIR (default ./vearl), DASHBOARD_PORT (default 8080).
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
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


def render_dashboard(d, lines=20, refresh=True):
    import html as _html
    running = d["running"]
    badge = ('<span class="badge on">● Mining</span>' if running
             else '<span class="badge off">● Stopped</span>')
    refresh_q = "on" if refresh else "off"
    seg = "".join(
        f'<a href="/?lines={n}&refresh={refresh_q}" class="{"sel" if n == lines else ""}">{n}</a>'
        for n in (10, 20, 50))
    toggle = (f'<a class="switch on" href="/?lines={lines}&refresh=off" '
              f'title="Auto-refresh on — tap to turn off"></a>' if refresh else
              f'<a class="switch off" href="/?lines={lines}&refresh=on" '
              f'title="Auto-refresh off — tap to turn on"></a>')
    gpu = d["gpu"]
    if gpu:
        pct = _gpu_util_pct(gpu["util"])
        gpu_html = (f'<div class="gpu-name">{_html.escape(gpu["name"])}</div>'
                    f'<div class="small">driver {gpu["driver"]} · VRAM {gpu["vram"]} · '
                    f'{gpu["temp"]}</div>'
                    f'<div class="bar"><i style="width:{pct}%"></i></div>'
                    f'<div class="muted">GPU load {pct}%</div>')
    else:
        gpu_html = ('<div class="big amber">No GPU</div>'
                    '<div class="small">CPU-only mining is testing-only and very slow. '
                    'Vearl needs an NVIDIA Ampere+ GPU.</div>')
    row, totals = d["points"], d["totals"]
    if row:
        points_html = (f'<div class="big green">{row.get("points")}</div>'
                       f'<div class="small">rank #{row.get("rank")} · {row.get("blocks")} blocks · '
                       f'{row.get("active_days")} active days · share {row.get("share", 0) * 100:.4f}%</div>'
                       f'<div class="muted">Eligible: {"yes" if row.get("eligible") else "not yet"} '
                       f'(needs 3+ days and 0.005% of points)</div>')
    elif d["leaderboard_error"]:
        points_html = ('<div class="big amber">n/a</div>'
                       f'<div class="small">Leaderboard unreachable: '
                       f'{_html.escape(str(d["leaderboard_error"]))}</div>')
    elif not d["address"]:
        points_html = ('<div class="big amber">n/a</div>'
                       '<div class="small">No wallet address found yet — run the miner once first.</div>')
    else:
        points_html = ('<div class="big amber">0 yet</div>'
                       '<div class="small">Address not on the leaderboard yet — blocks are a lottery, '
                       'it can take a while.</div>')
    addr_html = (f'<div class="addr">{_html.escape(d["address"])}</div>' if d["address"]
                 else '<div class="small">No wallet yet.</div>')
    totals_html = (f'Total: {totals.get("points")} points · {totals.get("miners")} miners · '
                   f'{totals.get("eligible")} eligible'
                   if totals else 'Leaderboard totals unavailable right now.')
    log_html = _html.escape("\n".join(d["log"])) if d["log"] else "(miner.log is empty — start the miner first)"
    age = (f'last update {d["log_age_s"]}s ago' if d["log_age_s"] is not None
           else 'no log file yet')
    meta = '<meta http-equiv="refresh" content="15">' if refresh else ''
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{meta}
<title>Vearl Miner</title>
<style>{DASHBOARD_CSS}</style></head>
<body><div class="wrap">
<header><div class="header-row"><div><h1>Vearl <span>Miner</span></h1>
<div class="sub">Testnet 1 · official miner v{d["version"]}</div></div>
{badge}</div></header>
<div class="controls">
<div class="row"><div><label>Log lines</label>
<div class="hint">How many lines of miner.log to show.</div></div>
<div class="segmented">{seg}</div></div>
<div class="row"><div><label>Auto-refresh</label>
<div class="hint">{"Reloads every 15s." if refresh else "Paused — tap the switch to resume."}</div></div>
{toggle}</div>
</div>
<div class="grid">
<div class="card"><h2>GPU</h2>{gpu_html}</div>
<div class="card"><h2>Points</h2>{points_html}</div>
<div class="card wide"><h2>Your address</h2>{addr_html}
<div class="muted">{totals_html}</div></div>
</div>
<div class="log"><h2>Last log lines · {age}</h2><pre>{log_html}</pre></div>
<footer>Points come live from the official leaderboard data file
<a href="{LEADERBOARD_URL}">{LEADERBOARD_URL}</a>
(check manually at <a href="{LEADERBOARD_PAGE}">vearl.org/leaderboard</a>).
Testnet tokens have no value. Never share your 24 recovery words with anyone —
this page never shows them.</footer>
</div></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        try:
            lines = int(q.get("lines", ["20"])[0])
        except ValueError:
            lines = 20
        lines = lines if lines in (10, 20, 50) else 20
        refresh = q.get("refresh", ["on"])[0] != "off"
        if parsed.path in ("/api.json", "/api"):
            d = dashboard_data(lines)
            body = json.dumps(d, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = render_dashboard(dashboard_data(lines), lines, refresh).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def do_dashboard(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Dashboard: http://127.0.0.1:{port}/  (local only, Ctrl-C to stop)")
    print("JSON status: http://127.0.0.1:%d/api.json" % port)
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
