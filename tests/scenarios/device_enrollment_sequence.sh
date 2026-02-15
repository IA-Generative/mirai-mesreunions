#!/usr/bin/env bash
set -euo pipefail

# Scenario simulé (smoke) pour enrôlement device en local Docker.
# Pré-requis:
# - utilisateur déjà connecté dans le navigateur pour générer un QR/code
# - remplacer QR_TOKEN ci-dessous

QR_TOKEN="${QR_TOKEN:-}"
BASE_UPLOAD="${BASE_UPLOAD:-http://localhost:8081}"

if [[ -z "$QR_TOKEN" ]]; then
  echo "Usage: QR_TOKEN=<token> $0"
  exit 1
fi

echo "[1] Bootstrap session (sans device token) ..."
curl -sS "${BASE_UPLOAD}/api/device/session/${QR_TOKEN}" | sed -n '1,120p'

echo "[2] Enroll (device_key/fingerprint fictifs) ..."
ENROLL_JSON="$(curl -sS -X POST "${BASE_UPLOAD}/api/device/enroll/${QR_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"device_key":"test-device-key-001","device_fingerprint":"ua|platform|lang","device_name":"Smoke Test Device"}')"
echo "$ENROLL_JSON" | sed -n '1,120p'

DEVICE_TOKEN="$(printf '%s' "$ENROLL_JSON" | python -c 'import sys,json; print(json.load(sys.stdin).get("device_token",""))')"
if [[ -z "$DEVICE_TOKEN" ]]; then
  echo "device_token absent"
  exit 2
fi

echo "[3] Bootstrap session (avec device token) ..."
curl -sS "${BASE_UPLOAD}/api/device/session/${QR_TOKEN}" \
  -H "X-Device-Token: ${DEVICE_TOKEN}" | sed -n '1,120p'

echo "[OK] Séquence simulée terminée."
