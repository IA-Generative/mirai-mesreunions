#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo " Secure Audio Upload - Setup"
echo "============================================"

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

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
