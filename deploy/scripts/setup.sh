#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo " Secure Audio Upload - Setup"
echo "============================================"

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

# ---- Audio de test : hébergé dans des repos dédiés (hors historique git) ------
# Fixtures publiques (poèmes domaine public, eicar, snippets) + datasets réels
# privés. Override possible via env FIXTURES_REPO / DATASETS_REPO. Datasets en
# opt-in : `setup.sh --with-datasets`.
FIXTURES_REPO="${FIXTURES_REPO:-https://github.com/IA-Generative/mirai-mesreunions-fixtures.git}"
DATASETS_REPO="${DATASETS_REPO:-https://github.com/IA-Generative/mirai-mesreunions-datasets.git}"
WITH_DATASETS=0
for arg in "$@"; do
    case "$arg" in
        --with-datasets) WITH_DATASETS=1 ;;
    esac
done

_fetch_into() {  # <repo-url> <dest-dir> <label> <soft-fail-msg>
    local repo="$1" dest="$2" label="$3" softmsg="$4" tmp
    if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        echo "[OK] $label déjà présent(es) ($dest)."; return 0
    fi
    echo "[..] Récupération $label..."
    tmp="$(mktemp -d)"
    if git clone --depth 1 -q "$repo" "$tmp" 2>/dev/null; then
        mkdir -p "$dest"; cp -R "$tmp/audio/." "$dest/"
        echo "[OK] $label -> $dest"
    else
        echo "[!!] $softmsg"
    fi
    rm -rf "$tmp"
}

fetch_audio_data() {
    _fetch_into "$FIXTURES_REPO" "tests/fixtures/audio" "fixtures audio" \
        "fixtures indisponibles (réseau). On continue sans."
    [ "$WITH_DATASETS" = "1" ] && _fetch_into "$DATASETS_REPO" "datasets" \
        "datasets privés" "datasets indisponibles (accès privé requis)."
    return 0
}
fetch_audio_data

detect_public_host_ip() {
    # 1) Respect explicit override
    if [ -n "${PUBLIC_HOST:-}" ]; then
        printf "%s" "$PUBLIC_HOST"
        return 0
    fi

    # 2) Linux: ip route
    if command -v ip >/dev/null 2>&1; then
        local ip_route_ip
        ip_route_ip="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
        if [ -n "$ip_route_ip" ]; then
            printf "%s" "$ip_route_ip"
            return 0
        fi
    fi

    # 3) macOS: default interface + ipconfig
    if command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
        local iface mac_ip
        iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
        if [ -n "$iface" ]; then
            mac_ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
            if [ -n "$mac_ip" ]; then
                printf "%s" "$mac_ip"
                return 0
            fi
        fi
    fi

    # 4) Fallback
    printf "%s" "localhost"
}

export PUBLIC_HOST="$(detect_public_host_ip)"
echo "[OK] PUBLIC_HOST=$PUBLIC_HOST"

# 1. Copy env file
if [ ! -f configs/.env ]; then
    cp configs/.env.example configs/.env
    echo "[OK] configs/.env created from template. Edit it with your settings."
else
    echo "[OK] configs/.env already exists."
fi

# 2. Build images
echo ""
echo "Building Docker images..."
docker compose -f deploy/docker/docker-compose.yml build

# 3. Start infrastructure first
echo ""
echo "Starting infrastructure (PostgreSQL, RabbitMQ, MinIO, ClamAV, Keycloak)..."
docker compose -f deploy/docker/docker-compose.yml up -d \
    postgres-external postgres-internal rabbitmq \
    minio-upload minio-processed minio-internal \
    clamav keycloak

echo "Waiting for services to be healthy..."
sleep 10

# 4. Start application services
echo ""
echo "Starting application services..."
docker compose -f deploy/docker/docker-compose.yml up -d

echo ""
echo "============================================"
echo " All services started!"
echo "============================================"
echo ""
echo " Code Generator (OIDC) : http://localhost:8080"
echo " Upload Portal          : http://localhost:8081"
echo " Access LAN conseillé   : http://$PUBLIC_HOST:8080"
echo " Token Issuer (interne) : http://localhost:8091 (API only)"
echo " Keycloak Admin         : http://localhost:8180 (admin/admin)"
echo " RabbitMQ Management    : http://localhost:15672 (audio/changeme-rabbit)"
echo " MinIO Upload Console   : http://localhost:9001 (minioadmin/minioadmin)"
echo " MinIO Processed Console: http://localhost:9003 (minioadmin/minioadmin)"
echo " MinIO Internal Console : http://localhost:9005 (minioadmin/minioadmin)"
echo ""
echo " Test user: testuser / testpassword"
echo ""
echo " Note: ClamAV needs ~2 minutes to download signatures on first start."
echo ""
