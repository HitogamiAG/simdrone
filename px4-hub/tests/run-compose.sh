#!/bin/sh
# Run from repository root. Integration checks require an empty Gazebo world.
set -eu
docker compose build px4-hub
docker compose up -d --no-deps px4-hub
wait_health() {
    service=$1
    count=0
    until docker compose exec -T "$service" curl -fsS "http://127.0.0.1:$2/healthz" >/dev/null; do
        count=$((count + 1))
        [ "$count" -lt 60 ] || return 1
        sleep 1
    done
}
wait_health px4-hub 8002
docker run --rm --network none \
    -v "$PWD/px4-hub:/opt/px4-hub:ro" \
    -e PYTHONPATH=/opt/px4-hub -e PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint /opt/venv/bin/pytest uav-gazebo-service:local \
    -p no:cacheprovider -q /opt/px4-hub/tests
docker compose exec -T px4-hub python < px4-hub/tests/integration.py
# Test dependency loss and shutdown with a live pair, preserving the container FS.
restore() {
    docker compose start gazebo-service px4-hub >/dev/null
}
trap restore EXIT INT TERM
docker compose exec -T px4-hub python - --mode fixture < px4-hub/tests/integration.py
docker compose stop gazebo-service
docker compose exec -T px4-hub python - --mode loss < px4-hub/tests/integration.py
docker compose stop px4-hub
container_id=$(docker compose ps -aq px4-hub)
state=$(docker inspect --format '{{.State.ExitCode}} {{.State.Pid}} {{.State.OOMKilled}}' "$container_id")
# Uvicorn 0.34 re-raises the received SIGTERM after completing its lifespan.
case "$state" in '0 0 false'|'143 0 false') ;; *) echo "Unexpected shutdown: $state" >&2; exit 1 ;; esac
docker compose logs --tail 10 px4-hub | rg -q 'Application shutdown complete'
docker compose start gazebo-service px4-hub
wait_health gazebo-service 8000
wait_health px4-hub 8002
docker compose exec -T px4-hub python -c \
    'import httpx; from pathlib import Path; assert httpx.get("http://127.0.0.1:8002/api/v1/instances/").json() == []; assert not list(Path("/opt/uav/instances").iterdir())'
# Run existing Gazebo suites sequentially, after Hub has released its instances.
docker compose exec -T gazebo-service pytest -q /opt/uav/tests/test_contract.py
docker compose exec -T gazebo-service python /opt/uav/tests/regression.py
docker compose exec -T gazebo-service python /opt/uav/tests/smoke.py
docker compose config --quiet
trap - EXIT INT TERM
