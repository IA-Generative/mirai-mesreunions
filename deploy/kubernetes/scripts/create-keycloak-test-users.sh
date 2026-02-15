#!/usr/bin/env bash
set -euo pipefail

# Create 10 test users in Keycloak realm (audio-upload by default).
# Requirements: curl, jq
#
# Example:
# KEYCLOAK_ADMIN_USER=admin KEYCLOAK_ADMIN_PASSWORD='change-me' \
#   ./deploy/kubernetes/scripts/create-keycloak-test-users.sh

KEYCLOAK_BASE_URL="${KEYCLOAK_BASE_URL:-https://openwebui-sso.fake-domain}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-audio-upload}"
KEYCLOAK_ADMIN_REALM="${KEYCLOAK_ADMIN_REALM:-master}"
KEYCLOAK_ADMIN_USER="${KEYCLOAK_ADMIN_USER:-}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"
TEST_USER_PASSWORD="${TEST_USER_PASSWORD:-change-me-test-user}"
USER_PREFIX="${USER_PREFIX:-testuser}"
USER_COUNT="${USER_COUNT:-10}"

if [[ -z "${KEYCLOAK_ADMIN_USER}" || -z "${KEYCLOAK_ADMIN_PASSWORD}" ]]; then
  echo "ERROR: KEYCLOAK_ADMIN_USER and KEYCLOAK_ADMIN_PASSWORD are required."
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required."
  exit 1
fi

echo "Getting admin token from ${KEYCLOAK_BASE_URL}..."
TOKEN="$(
  curl -sS -X POST \
    "${KEYCLOAK_BASE_URL}/realms/${KEYCLOAK_ADMIN_REALM}/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=password" \
    --data-urlencode "client_id=admin-cli" \
    --data-urlencode "username=${KEYCLOAK_ADMIN_USER}" \
    --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
  | jq -r '.access_token'
)"

if [[ -z "${TOKEN}" || "${TOKEN}" == "null" ]]; then
  echo "ERROR: Unable to retrieve Keycloak admin token."
  exit 1
fi

for i in $(seq 1 "${USER_COUNT}"); do
  num="$(printf '%02d' "${i}")"
  username="${USER_PREFIX}${num}"
  email="${username}@example.com"
  first_name="Test${num}"
  last_name="User"

  existing_user_id="$(
    curl -sS \
      "${KEYCLOAK_BASE_URL}/admin/realms/${KEYCLOAK_REALM}/users?username=${username}&exact=true" \
      -H "Authorization: Bearer ${TOKEN}" \
    | jq -r '.[0].id // empty'
  )"

  if [[ -z "${existing_user_id}" ]]; then
    create_code="$(
      curl -sS -o /tmp/kc_create_user.json -w "%{http_code}" \
        -X POST "${KEYCLOAK_BASE_URL}/admin/realms/${KEYCLOAK_REALM}/users" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
          \"username\": \"${username}\",
          \"enabled\": true,
          \"emailVerified\": true,
          \"firstName\": \"${first_name}\",
          \"lastName\": \"${last_name}\",
          \"email\": \"${email}\"
        }"
    )"
    if [[ "${create_code}" != "201" ]]; then
      echo "ERROR: create user ${username} failed (HTTP ${create_code})"
      cat /tmp/kc_create_user.json || true
      exit 1
    fi
    existing_user_id="$(
      curl -sS \
        "${KEYCLOAK_BASE_URL}/admin/realms/${KEYCLOAK_REALM}/users?username=${username}&exact=true" \
        -H "Authorization: Bearer ${TOKEN}" \
      | jq -r '.[0].id // empty'
    )"
  fi

  if [[ -z "${existing_user_id}" ]]; then
    echo "ERROR: unable to resolve id for ${username}"
    exit 1
  fi

  reset_code="$(
    curl -sS -o /tmp/kc_set_pwd.json -w "%{http_code}" \
      -X PUT "${KEYCLOAK_BASE_URL}/admin/realms/${KEYCLOAK_REALM}/users/${existing_user_id}/reset-password" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{
        \"type\": \"password\",
        \"value\": \"${TEST_USER_PASSWORD}\",
        \"temporary\": false
      }"
  )"

  if [[ "${reset_code}" != "204" ]]; then
    echo "ERROR: set password for ${username} failed (HTTP ${reset_code})"
    cat /tmp/kc_set_pwd.json || true
    exit 1
  fi

  echo "OK: ${username} / ${email}"
done

echo "Done. Created or updated ${USER_COUNT} users in realm ${KEYCLOAK_REALM}."
