# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Build du bundle Vite pour mesreunions-web (PR0 toolchain front).
#
# Ce stage tourne pour TOUS les services (image partagée), mais le `dist/`
# produit n'est utile qu'à mesreunions-web. L'overhead est minime grâce au
# cache BuildKit (~10s npm install au premier build, <1s ensuite si les
# sources frontend n'ont pas changé).
#
# Le `npm run build` enchaîne `vite build` (bundle JS → app/static/dist/) et
# `scripts/copy-dsfr-assets.mjs` (copie les assets DSFR auto-hébergés depuis
# node_modules vers app/static/dsfr/).
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-alpine AS node-builder
WORKDIR /build
COPY services/mesreunions-web/package.json services/mesreunions-web/package-lock.json* services/mesreunions-web/vite.config.js ./
COPY services/mesreunions-web/scripts/ ./scripts/
COPY services/mesreunions-web/frontend/ ./frontend/
RUN npm install --no-fund --no-audit \
 && npm run build

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Image runtime Python (image partagée tous services).
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (ffmpeg for transcode worker)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared library
COPY libs/shared /app/libs/shared
# Copy service code for Kubernetes/runtime images
COPY services /app/services
# Embed the static general glossary used by the Kevent meeting-intelligence
# `glossary_correction` step (KEVENT_GLOSSARY_DIR defaults to /app/glossaire).
COPY glossaire /app/glossaire

# Inject bundle Vite (utile uniquement pour mesreunions-web ; inerte pour les
# autres services qui n'incluent pas le partial _vite_shell_loader.html).
COPY --from=node-builder /build/app/static/dist /app/services/mesreunions-web/app/static/dist
# Inject assets DSFR auto-hébergés (CSS/JS DSFR + fonts Marianne + icônes).
# Servis par Flask via /static/dsfr/... — référencés par index.html.
COPY --from=node-builder /build/app/static/dsfr /app/services/mesreunions-web/app/static/dsfr

# Default: overridden per service
CMD ["python", "-m", "app.main"]
