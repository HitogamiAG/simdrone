#!/bin/sh
set -eu
ACTION=${1:-}
PATH_NAME=${MTX_PATH:-${2:-}}
DRONE_ID=$(printf '%s' "$PATH_NAME" | cut -d/ -f2)
SENSOR_ID=$(printf '%s' "$PATH_NAME" | cut -d/ -f4)
case "$DRONE_ID:$SENSOR_ID" in
  *[!A-Za-z0-9_:-]*|:) echo "invalid MediaMTX path: $PATH_NAME" >&2; exit 2 ;;
esac
API="${API_BASE:-http://gazebo-service:8000}/api/v1/drones/$DRONE_ID/sensors/$SENSOR_ID"
post() { curl --silent --show-error --fail --max-time 12 -X POST "$API/$1" >/dev/null; }
case "$ACTION" in
  activate)
    post activate
    finish() { post deactivate || true; }
    trap finish INT TERM EXIT
    while :; do sleep 3600 & wait $!; done
    ;;
  deactivate) post deactivate ;;
  *) echo "usage: hook.sh activate|deactivate" >&2; exit 2 ;;
esac
