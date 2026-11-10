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

## Dashboard v2 — tested 10 Nov 2026

Same throwaway-copy method as above (`/tmp/v2test`, `/tmp/v2test2`), port
8097, no GPU. All v1 CLI behaviour re-checked: `--help` exit 0,
`--check` exit 1 (no GPU), `--download --dry-run` exit 0.

- `/?page=home`, `/?page=profile`, `/?page=setup` — all **200**, burger
  (☰) + nav links to all three pages present on each, no forbidden hue
  in any page or `/api.json`.
- Home: same status cards as v1; `?lines=50&refresh=off` → no
  `<meta refresh>`, 50-line segment selected.
- `/api.json` — same JSON as v1, no seed/vault/words fields.
- POST `/action/check` — **200**, result block shows real `--check`
  output + exit 1 (`FAIL: no NVIDIA GPU`).
- POST `/action/download` — **200**, real 31 MB download, `OK: SHA-256
  verified`, extracted to the throwaway copy's `vearl/` folder.
- POST `/action/download` with `RAILWAY_ENVIRONMENT=production` (second
  server, port 8096) — **200**, `REFUSED: will not 'download the miner'
  on Railway without explicit consent.` Gate works through the dashboard.
- POST `/action/start-miner` — before download: "Miner not downloaded
  yet"; after download, no wallet: "No wallet yet — first run must be
  interactive in a terminal". Miner never actually started here.
- POST `/action/register` (contact `TestUser123`, no wallet) —
  "No wallet yet — run the miner once first". No real registration.
- POST `/action/check-registration` (no wallet) — "No wallet address yet".
- Vault round-trip, with **fake test words** (NATO alphabet, never a real
  seed) + passphrase `correct-horse-123`:
  - save → **200**, `seed.vault` created mode `0600`, file contains no
    plaintext words (grep 0 matches).
  - reveal with wrong passphrase → refused, words not leaked.
  - reveal with correct passphrase → exact 24 words shown, only in that
    POST response.
  - validation errors shown correctly: 3 words → "Need exactly 24 words",
    short passphrase → "Passphrase must be at least 8 characters",
    mismatched passphrases → "Passphrases do not match".
  - delete with correct passphrase → vault file gone.
  - `/api.json`, server log and a plain profile GET contain none of the
    test words (grep 0 matches everywhere).
- **Bug found & fixed during testing:** `_result_block` ran `safe_line`
  over action output, which redacts any line mentioning the words — so
  the vault validation errors above were rendered as
  `[redacted — ...]` instead of the real message. Fixed: action output is
  no longer blanket-redacted (it is generated by our own code and never
  contains the words); miner log lines are still redacted in
  `last_log_lines()`. Re-tested after the fix — all messages above now
  display, vault round-trip still passes, workspace `run.py` verified
  byte-identical to the tested copy (`diff` clean).
- Screenshot: `docs/dashboard-setup.png` is a real browser render of the
  live Setup page (Playwright-core + system Chromium, 390 px viewport),
  read back and verified visually.

## Bahasa Indonesia UI — tested 10 Nov 2026

All web dashboard strings in `run.py` were translated to Bahasa Indonesia
(nav: Beranda / Profil / Akun / Pengaturan; buttons, cards, forms, vault
messages, action result titles). Verified live on port 8099:
`/?page=home`, `/?page=profile`, `/?page=setup` all **200**, `/api.json`
**200**, and the rendered HTML was scanned — no leftover English UI
phrases (only allowed tokens remain: Vearl, Testnet, rc27, SHA-256, `vrl1`,
Discord, GitHub, PBKDF2, `?page=` values, URLs, and tech loanwords like
GPU / vault / wallet / passphrase). CLI output was re-checked and is
unchanged English (`--check` exit 1 no GPU, `--download --dry-run` exit 0).
Fresh Setup screenshot in Indonesian overwrote `docs/dashboard-setup.png`
(390×1481, read back and verified visually).

## Dashboard auth — tested 10 Nov 2026

Throwaway copy in `/tmp/authtest`, port 8095, no GPU. Password used in
tests: `correct-horse-123` (test-only, never a real password); fake test
words were NATO alphabet, never a real seed. All test auth/vault files
were deleted with the throwaway copy; the workspace ships with NO
`.dashboard-auth.json` and NO `seed.vault`, so the user's first run is a
fresh "Buat Password".

- Unauthenticated `GET /`, `/?page=home|profile|setup`, `/api.json` — all
  **302 → /register** (first run), no dashboard content leaked (0 matches
  for "Beranda / Poin / Tidak ada GPU" in the response). Unauthenticated
  `POST /action/check` — **401 `{"error": "login required"}`**.
- `GET /register` — **200**, "Buat Password" page with the no-recovery
  warning. Register with mismatched passwords → refused ("Password tidak
  cocok"); short password → refused ("minimal 8 karakter"); valid password
  → **302 → /?page=home** with a `Set-Cookie: vearl_session=...` (HttpOnly,
  SameSite=Lax). `.dashboard-auth.json` created mode `0600`, contains only
  `salt / hash / secret / iterations` (200,000) — **grep 0 for the plaintext
  password**.
- Authenticated `/?page=home|profile|setup` and `/api.json` — all **200**;
  `/api.json` contains no seed/password fields.
- `POST /auth/logout` — **302 → /login**, cookie cleared; `GET /` with the
  old cookie afterwards — **302 → /login**.
- `GET /login` — **200**, "Masuk" page. Wrong password → "Password salah";
  correct password → **302** + new session cookie.
- Vault (login password is now the vault key, no separate passphrase):
  save fake 24 words → **200**, `seed.vault` mode `0600`, no plaintext
  words in the file (grep 0); `GET /?page=profile` shows the words with
  `id="seed"` and a **Salin** button; `/api.json` has none of the words
  (grep 0); delete → **200**, "Vault terhapus", file gone.
- CLI re-checked: `--check` exit 1 (no GPU), `--download --dry-run`
  exit 0. Workspace `run.py` verified byte-identical to the tested copy
  (`diff` clean).
- Screenshot: `docs/dashboard-login.png` is a real browser render of the
  live login page (Playwright-core + system Chromium, 390 px viewport),
  read back and verified visually: "Masuk", password field, no-recovery
  warning, teal button, no purple.

## NOT tested in v2 either

- Real mining via the dashboard "Start miner" button, real wallet
  creation, or real registration against a real wallet — the button paths
  were exercised only up to their safe refusals (no binary / no wallet).
- A real GPU machine's Profile/Setup pages, and the dashboard on a real
  iPhone.
