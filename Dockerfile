# GPU VPS ONLY. Do NOT deploy to Railway / Render / Colab / Kaggle —
# they ban crypto mining and have no GPU. See README.md.
FROM nvidia/cuda:12.6.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY run.py .
# vearl/ (official binary + wallet) is downloaded at runtime and lives
# on a volume so the wallet survives container restarts.
VOLUME ["/app/vearl"]

CMD ["python3", "run.py"]
