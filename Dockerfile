# PV GrowthOS — lightweight runtime image.
# Built in CI (GitHub Actions), never on the production VPN server.
FROM python:3.12-slim AS base

# Isolated probe CLIENT, pinned to an official release and verified checksum.
# It never reads or modifies the host's Xray binary/configuration.
RUN python -c "import urllib.request,hashlib,zipfile,io; d=urllib.request.urlopen('https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-linux-64.zip',timeout=60).read(); assert hashlib.sha256(d).hexdigest()=='23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae'; z=zipfile.ZipFile(io.BytesIO(d)); open('/usr/local/bin/growth-xray','wb').write(z.read('xray'))" \
    && chmod 755 /usr/local/bin/growth-xray

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY alembic.ini pyproject.toml ./
COPY alembic ./alembic
COPY src ./src
RUN pip install --no-cache-dir --no-deps -e .

# run as non-root
RUN useradd --system --uid 10001 growth && chown -R growth:growth /app
USER growth

EXPOSE 8350
ENV PVG_HOST=0.0.0.0 PVG_PORT=8350

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import httpx,os;httpx.get('http://127.0.0.1:8350/health',timeout=4).raise_for_status()"

CMD ["python", "-m", "pv_growth", "serve"]
