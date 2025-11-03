#!/usr/bin/env bash
set -euo pipefail

PHONE_IP="${PHONE_IP:-}"
ADB_PORT="${ADB_PORT:-5555}"

if [ -z "$PHONE_IP" ]; then
  echo "ERROR: PHONE_IP not set. Provide PHONE_IP env (e.g. 192.168.1.123)." >&2
  exit 1
fi

echo "[entrypoint] Starting ADB server..."
adb start-server

# Optional Wireless Debugging pairing (Android 11+)
if [[ -n "${ADB_PAIR_IP_PORT:-}" && -n "${ADB_PAIR_CODE:-}" ]]; then
  echo "[entrypoint] Pairing with ${ADB_PAIR_IP_PORT} ..."
  adb pair "${ADB_PAIR_IP_PORT}" <<<"${ADB_PAIR_CODE}" || true
fi

echo "[entrypoint] Connecting to ${PHONE_IP}:${ADB_PORT} ..."
TRIES=0
until adb connect "${PHONE_IP}:${ADB_PORT}" >/dev/null 2>&1; do
  TRIES=$((TRIES+1))
  if [ "$TRIES" -gt 30 ]; then
    echo "ERROR: Could not adb connect to ${PHONE_IP}:${ADB_PORT} after retries." >&2
    adb devices
    exit 2
  fi
  sleep 2
done

echo "[entrypoint] ADB devices:"
adb devices

if ! adb devices | grep -q "${PHONE_IP}:${ADB_PORT}.*device"; then
  echo "ERROR: Phone not in 'device' state. Check the phone screen/IP/port." >&2
  exit 3
fi

echo "[entrypoint] Launching Python script..."
exec python /app/byd_mqtt_bridge.py
