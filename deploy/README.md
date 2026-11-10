# Deploy options

One-click templates for every platform — **including the ones you should not use.**
Each banned-platform template carries the same warning in the file itself,
and `run.py` refuses to mine on those platforms unless you explicitly set
`I_UNDERSTAND_BANNED_PLATFORM=1`. No stealth, no renamed processes, no
detection bypass: the service is always openly named `vearl-miner`.

| Platform | Template | Status |
|---|---|---|
| Local laptop / PC (own GPU) | `python3 run.py` (repo root) | ✅ Recommended |
| GPU VPS — Docker | `../Dockerfile`, `../docker-compose.yml` | ✅ Recommended |
| GPU VPS — systemd | `../vearl-miner.service` | ✅ Recommended |
| Railway | `railway.toml` | ❌ Banned — crypto mining is abuse per Railway AUP; no GPU |
| Render | `render.yaml` | ❌ Banned — AUP item 7 explicitly bans crypto mining; no GPU |
| Google Colab | `colab.ipynb` | ❌ Banned — Colab prohibits mining; ~12 h sessions; no Ampere GPU guaranteed |
| Kaggle | `kaggle.ipynb` | ❌ Banned — Kaggle AUP bans cryptomining; ~12 h sessions; limited GPU hours |

## Why the ❌ ones fail (verified 10 Nov 2026)

- **Railway:** no GPU instances; AUP lists cryptocurrency mining as abuse;
  free trial is $5, a 24/7 service burns it in ~6 days.
- **Render:** no GPU on any plan; AUP item 7 explicitly bans crypto mining
  (suspension / termination); free web services spin down after 15 min idle
  and there is no free background worker.
- **Google Colab:** mining banned; sessions cap ~12 h; GPU not guaranteed.
- **Kaggle:** cryptomining banned by AUP; sessions cap ~12 h; ~30 free
  GPU-hours/week.

## The honest alternative

Rent an RTX 3090 on a GPU marketplace (e.g. Vast.ai / RunPod) for roughly
$12–18 for the 3 active days Vearl requires — or mine on your own Ampere+
GPU. That is the only route that can plausibly reach the 0.005% of total
points needed for eligibility. Testnet tokens have no value either way.
