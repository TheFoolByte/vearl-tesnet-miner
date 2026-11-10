# Vearl Miner Wrapper

A thin Python (stdlib-only) wrapper around the **official Vearl testnet miner**.
It downloads, SHA-256-verifies, runs and monitors the official binary — nothing else.

## What it is / is NOT

- ✅ It IS a helper: preflight checks, verified download, auto-restart, local dashboard.
- ❌ It is NOT a miner. All mining / Proof-of-Useful-Work is done by the official
  `vearl-miner` binary from `github.com/vearl-verified-compute/vearl`.
- ❌ It does NOT scrape Pearl, does NOT reimplement mining, and does NOT mine on
  your behalf anywhere. Vearl is not Pearl and Pearl has not endorsed it.
- Testnet tokens have **no value** and cannot be sold or converted.

## Requirements

- NVIDIA GPU, Ampere generation or newer (RTX 30/40/50, A/L/H100, compute 8.0+),
  Linux driver 575+ / Windows 580+, glibc 2.35+ (Ubuntu 22.04+).
- Without a GPU the miner runs on CPU **very slowly** — testing only. It will
  not realistically earn the 0.005% of total points needed for eligibility.

## Quick start — laptop (Linux / Windows / Termux)

```bash
python3 run.py --check      # preflight: GPU, driver, disk, internet
python3 run.py --download   # fetch rc27 + verify SHA-256 + extract to ./vearl/
python3 run.py              # run the miner (auto-restart, logs to miner.log)
```

First run creates your wallet and shows **24 recovery words ONCE** — write them
on paper. Never photograph them, never type them anywhere, never give them to
anybody. This script never prints or logs them; only your public `vrl1...`
address is ever shown.

Headless / VPS with an existing address (skips the wallet wizard, pool mode):

```bash
python3 run.py --address vrl1xxxxxxxxxxxxxxxxxxxx
```

In another terminal:

```bash
python3 run.py --dashboard  # http://127.0.0.1:8080 — status, GPU, log, live points
```

The dashboard is iOS-style: dark graphite, frosted-glass cards, big rounded
corners, mobile-first. It has a segmented control for log lines (10 / 20 / 50)
and a toggle switch for auto-refresh (every 15s). No purple anywhere — the
palette is graphite + mint/teal/amber.

## Dashboard v2 — Home / Profile / Setup

`python3 run.py --dashboard` serves a local-only page on
`http://127.0.0.1:8080` (never `0.0.0.0`). Tap the **☰** burger menu
(top right) to switch pages — `?page=home|profile|setup`, server-rendered,
no JS framework, only a tiny inline script for the menu and the copy button.

- **Home** — the original status page: miner status, GPU, live points from
  the official leaderboard, your address, log lines (10 / 20 / 50) and the
  auto-refresh toggle (15 s).
- **Setup** — the terminal actions as cards + buttons, no terminal needed:
  1) preflight check, 2) download + SHA-256-verify the official miner,
  3) create wallet / start miner in background, 4) register for rewards
  (contact + optional print-only), 5) check registration status. Each
  action shows its real output and exit code on the page. The banned-platform
  gate still applies: nothing mines/downloads on Railway/Render/Colab/Kaggle
  unless `I_UNDERSTAND_BANNED_PLATFORM=1`.
- **Profile / Account** — public `vrl1...` address (copy button), points /
  rank / active days / eligible, registration status, wallet file presence.

The web dashboard UI is in **Bahasa Indonesia** (Beranda / Profil / Akun /
Pengaturan); the terminal CLI output stays in English, and `?page=` values
stay `home` / `profile` / `setup`.

### Password dashboard — wajib masuk dulu

Every dashboard page (`/?page=home|profile|setup`, `/api.json`, and all
`/action/*` endpoints) requires a password. There is no anonymous access,
even on localhost.

- **First run:** the dashboard shows a **Buat Password** page (password +
  confirm, min. 8 characters). The password is stored ONLY as a salted
  hash in `.dashboard-auth.json` (mode `0600`, git-ignored) — PBKDF2-HMAC-SHA256,
  200,000 iterations — plus a random server secret for signing session
  cookies. **The plaintext password is never stored anywhere, never logged.**
- **Later visits:** a **Masuk** page asks for the password. A correct login
  issues a session cookie (random 32-byte token, HMAC-signed with the
  server secret, HttpOnly, SameSite=Lax, 12 hours). A wrong password gets a
  generic "Password salah" and a 0.5 s delay. **Keluar** in the burger menu
  logs out and clears the cookie.
- **⚠️ Lupa password = terkunci selamanya.** There is no password recovery
  and no password-change feature. The dashboard stays locked and the seed
  vault cannot be opened again. Write the password down somewhere safe,
  like the 24 words themselves.

<img src="docs/dashboard-login.png" alt="Vearl Miner login page — Masuk, password field, no-recovery warning" width="390">

<img src="docs/dashboard.png" alt="Vearl Miner dashboard — iOS-style, dark graphite with mint/teal/amber" width="390">

Screenshot above is the real dashboard rendered from the live server
(`python3 run.py --dashboard`), captured on a test machine with no GPU —
so it honestly shows `Stopped` / `No GPU` / `n/a` points until the miner
runs on a GPU machine with a wallet.

<img src="docs/dashboard-setup.png" alt="Vearl Miner Setup page — cards and buttons for check, download, start miner, register, check registration" width="390">

### The 24 words — NOT stored by default

By default the dashboard does **not** store your recovery words anywhere.
They were shown once, when the wallet was created — write them on paper.
The Profile page says this plainly.

Opt-in only: you may paste the 24 words once into the Profile page. They
are stored in `seed.vault` (mode `0600`, git-ignored), encrypted with a key
derived from **your dashboard login password** (PBKDF2-HMAC-SHA256, 200,000
iterations + a SHA-256 counter-mode stream cipher + HMAC tag, stdlib only).
There is no separate vault passphrase anymore. After login, the Profile
page shows the words with a small **Salin** copy button. The words are
never logged, never in `/api.json`. **Warning: anyone with dashboard access
*and* your password can steal the wallet — paper is still the safest
backup, and forgetting the password locks the vault forever.** Delete the
vault from the Profile page (login required).

## Deploy

| Platform | How | Status |
|---|---|---|
| Local laptop / PC (own GPU) | `python3 run.py` | ✅ Recommended |
| GPU VPS — Docker | `docker compose up -d --build` | ✅ Recommended |
| GPU VPS — systemd | `vearl-miner.service` | ✅ Recommended |
| Railway | [Deploy on Railway](https://railway.app/new) with `deploy/railway.toml` | ❌ Banned — not recommended |
| Render | [Render Blueprint](https://dashboard.render.com/new) with `deploy/render.yaml` | ❌ Banned — not recommended |
| Google Colab | Open `deploy/colab.ipynb` in Colab | ❌ Banned — not recommended |
| Kaggle | Open `deploy/kaggle.ipynb` in Kaggle | ❌ Banned — not recommended |

The ❌ templates exist only so the repo shows every option (showcase). They
carry a big warning in the file itself, and `run.py` refuses to mine on those
platforms unless you set `I_UNDERSTAND_BANNED_PLATFORM=1`. Details in
`deploy/README.md` — mining is banned there, accounts can be suspended or
terminated, and there is no GPU anyway.

## Register for rewards

```bash
python3 run.py --register --contact "YourDiscordName"
# --print-only shows the signed lines without sending, if e-mail is blocked
```

The official wallet signs the registration and sends it to `register@vearl.org`.
Check your points at <https://vearl.org/leaderboard/> — you need 3+ active days
and at least 0.005% of all points. Testnet 1 allocation: 0.7% of max supply,
shared pro-rata by points.

## GPU VPS (the only cloud option that makes sense)

```bash
docker compose up -d --build   # needs an NVIDIA GPU + nvidia-container-toolkit
# or: systemd
sudo cp vearl-miner.service /etc/systemd/system/
sudo systemctl enable --now vearl-miner
```

## ⚠️ Where NOT to deploy this

**Railway, Render, Google Colab and Kaggle all ban crypto mining.** Running this
there will get the account/service suspended or terminated — Railway's AUP lists
cryptocurrency mining as abuse, Render's AUP (item 7) explicitly bans it, and
Colab/Kaggle ban it too. They also have no GPU, so it wouldn't earn points
anyway. The `deploy/` templates for those platforms exist only as a showcase
of every option, each carrying a big warning, and `run.py` refuses to mine on
them unless you set `I_UNDERSTAND_BANNED_PLATFORM=1`.
Use a GPU VPS you rent (e.g. an RTX 3090 on a GPU marketplace) or your own GPU.

## Files

| File | Purpose |
|---|---|
| `run.py` | The wrapper (stdlib only, Linux + Windows + Termux) |
| `Dockerfile`, `docker-compose.yml` | GPU VPS container |
| `vearl-miner.service` | systemd unit for a VPS |
| `TESTS.md` | What was tested here, and what was not |

## Env vars

`VEALR_VERSION` (default `0.1.0-rc27`), `VEALR_DIR` (default `./vearl`),
`DASHBOARD_PORT` (default `8080`). No secrets in code, ever.
