#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

detect_public_host_ip() {
    if [ -n "${PUBLIC_HOST:-}" ]; then
        printf "%s" "$PUBLIC_HOST"
        return 0
    fi

    if command -v ip >/dev/null 2>&1; then
        local ip_route_ip
        ip_route_ip="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
        if [ -n "$ip_route_ip" ]; then
            printf "%s" "$ip_route_ip"
            return 0
        fi
    fi

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

    printf "%s" "localhost"
}

export PUBLIC_HOST="$(detect_public_host_ip)"
echo "[compose-up] PUBLIC_HOST=$PUBLIC_HOST"

docker compose -f deploy/docker/docker-compose.yml up -d --build "$@"
