# Tests — run 10 Nov 2026

Environment: this VM — Linux x86_64, Python 3.12.3, glibc 2.39, **no GPU**
(`nvidia-smi` absent). All download/dashboard tests ran on throwaway copies
in `/tmp` so `~/workspace/vearl-miner/` stays clean; the tested copy was
verified byte-identical to the workspace `run.py` (`diff` clean).

## 1. `python3 run.py --help` — PASS (exit 0)

Lists all flags: `--check --download --dry-run --dashboard --port
--register --contact --print-only --address`.

## 2. `python3 run.py --check` — PASS (exit 1, correct behaviour)

```
OS      : Linux 7.0.0-44-generic (x86_64)
Python  : 3.12.3
glibc   : glibc 2.39 (need >= 2.35)
Disk free: 152391 MB
GPU     : NONE detected (no nvidia-smi / no NVIDIA GPU)
Internet: vearl.org OK, github.com OK
FAIL: no NVIDIA GPU — Vearl needs Ampere+ (RTX 30/40/50, CC 8.0+)
Result: NOT READY — fix the FAIL lines above. Do not mine without a GPU.
```

Exits non-zero with no GPU, warns instead of faking it, does not crash.

## 3. `python3 run.py --download --dry-run` — PASS (exit 0)

Prints only the two official URLs (rc27 tarball + SHA256SUMS.txt from
`github.com/vearl-verified-compute/vearl/releases/download/v0.1.0-rc27/`),
downloads nothing.

## 4. Real `--download` — PASS (exit 0)

Downloaded the real 31 MB `vearl-0.1.0-rc27-linux-x86_64.tar.gz`,
SHA-256 verified OK against the official SHA256SUMS.txt, extracted.
Found a real layout quirk and fixed it: the tarball extracts to a
versioned subfolder (`vearl-0.1.0-rc27/vearl-miner`), so `run.py` now
locates the binary one level down (`locate_binary()`); verified the
resolved path exists. The official binary itself also runs:
`vearl-miner --help` prints pool default (`pool.vearl.org:3333`),
`--address`, `--no-models`, `--no-update`.

## 5. Checksum tamper tests — PASS

- Same filename + one appended byte → `FAIL: SHA-256 mismatch`, refused.
- Genuine archive → `OK: SHA-256 verified`.
- Renamed file with no entry in SHA256SUMS.txt → refused ("no SHA-256 entry").

## 6. Banned-platform gate — PASS

| Env | Command | Result |
|---|---|---|
| `RAILWAY_ENVIRONMENT=production` | `run.py` (default) | REFUSED, exit 1 |
| `RENDER=true` | `run.py` (default) | REFUSED, exit 1 |
| `COLAB_RELEASE_TAG=x` | `run.py --download` | REFUSED, exit 1 |
| `KAGGLE_KERNEL_RUN_TYPE=Interactive` | `run.py` (default) | REFUSED, exit 1 |
| Railway + `I_UNDERSTAND_BANNED_PLATFORM=1` | `run.py --download --dry-run` | Gate passed, exit 0 |
| Railway + ack, stub miner binary | `run.py` (default) | Gate passed, stub ran, clean exit 0 |
| Local (no platform env) | `run.py --check` | Normal, warn-only path |

The stub-miner test used a fake `vearl-miner` shell script — it proves the
wrapper's gate/run/restart plumbing, not real mining.

## 7. Dashboard (iOS-style) — PASS

Started `run.py --dashboard --port 8098` against a fake `miner.log`
(including a planted "recovery words" line and a real public leaderboard
address). Verified via curl:

- `/` renders: sticky frosted-glass header, large title, MINING/STOPPED
  badge, segmented control (10 / 20 / 50), auto-refresh toggle switch,
  GPU card, Points card, address card, log panel. `backdrop-filter` present.
- Live points work: test address shows **1873.5 points, rank #1** from the
  official leaderboard data file — same values as the file itself.
- `/?lines=50&refresh=off` → 50-line segment marked selected, **no**
  `<meta refresh>` (paused); default page has the 15 s refresh.
- `/api.json` returns the same status as JSON.
- The planted secrets line is shown as `[redacted — ...]`, never raw.
- Palette check: `grep -ri` for the forbidden hue (and its common hex
  codes) over `run.py`, the rendered dashboard HTML and `deploy/` →
  **no matches**. Palette is dark graphite + mint/teal/amber only.

## 8. Deploy templates — PASS (static checks)

- `deploy/colab.ipynb` and `deploy/kaggle.ipynb`: valid JSON (`json.tool`),
  nbformat 4, warning markdown cell is the first cell in both.
- `deploy/railway.toml`, `deploy/render.yaml`: warning header at the top,
  service openly named `vearl-miner`, no renamed/stealth process.
- README deploy table marks Railway/Render/Colab/Kaggle ❌ Banned and
  Local/Docker/systemd ✅ Recommended.

## Endpoints verified live (10 Nov 2026)

- `https://vearl.org/rewards/leaderboard.json` → **200, JSON** (rows:
  `address / points / rank / active_days / eligible / share`). This is the
  URL the dashboard uses. Note: `https://vearl.org/leaderboard.json`
  (root) is **404** — the correct path is under `/rewards/`.
- `https://vearl.org/rewards/registered.json` → 200.
- GitHub release `v0.1.0-rc27` + SHA256SUMS.txt → verified via download.
- One transient `github.com UNREACHABLE` during a `/tmp` check run (the
  512 MB `/tmp` was 63% full from test downloads at the time); re-checked
  afterwards: `github.com: 200`, `vearl.org: 200`. `/tmp` cleaned after tests.

## NOT tested (no GPU / not on those platforms)

- Real mining for any length of time; no real wallet was created, no
  24 words were ever generated, no `--register` against a real wallet.
- GPU paths: `--check` PASS branch, driver-version warnings, GPU util bar
  with a real GPU, `vearl-miner check`.
- `docker build` / `docker compose up` (no Docker/GPU here) and the
  systemd unit on a real VPS (unit file only syntax-reviewed).
- Colab / Kaggle notebooks were **not executed** on Colab/Kaggle.
- Dashboard was verified via curl + HTML inspection, not on a real
  iPhone — mobile layout is CSS review only (max-width 680 px container,
  single-column cards, `viewport-fit=cover`, 15 s auto-refresh).
