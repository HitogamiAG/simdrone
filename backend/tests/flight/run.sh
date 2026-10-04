#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$ROOT"
PROJECT=simdrone-flight-test
COMPOSE="docker compose -p $PROJECT --env-file .env.flight -f docker-compose.flight.yml"
ENV_CREATED=0
ROOT_ENV_CREATED=0
if [ ! -f .env.flight ]; then
    cp .env.example .env.flight
    ENV_CREATED=1
fi
mkdir -p artifacts/flight

cleanup() {
    if [ "${STACK_UP:-0}" = 1 ]; then
        $COMPOSE logs --no-color >"artifacts/flight/compose-${RUN_INDEX:-0}.log" 2>&1 || true
        $COMPOSE down --volumes --remove-orphans || true
    fi
    if [ "$ENV_CREATED" = 1 ]; then
        python3 -c 'from pathlib import Path; Path(".env.flight").unlink(missing_ok=True)'
    fi
    if [ "$ROOT_ENV_CREATED" = 1 ]; then
        python3 -c 'from pathlib import Path; Path(".env").unlink(missing_ok=True)'
    fi
}
trap cleanup EXIT INT TERM

run_once() {
    RUN_INDEX=$1
    STACK_UP=1
    $COMPOSE up -d --build --wait gazebo-service px4-hub backend mediamtx
    $COMPOSE images --format json >"artifacts/flight/images-${RUN_INDEX}.json"
    $COMPOSE build flight-runner
    mkdir -p artifacts/flight/control
    rm -f artifacts/flight/control/backend-loss-*.request artifacts/flight/control/backend-loss-*.done
    set +e
    if [ -n "${FLIGHT_TEST_FILTER:-}" ]; then
        $COMPOSE run --rm --no-deps flight-runner python -m pytest -q test_flight.py -k "$FLIGHT_TEST_FILTER" >"artifacts/flight/run-${RUN_INDEX}.log" 2>&1 &
    else
        $COMPOSE run --rm --no-deps flight-runner >"artifacts/flight/run-${RUN_INDEX}.log" 2>&1 &
    fi
    TEST_PID=$!
    while kill -0 "$TEST_PID" 2>/dev/null; do
        for REQUEST in artifacts/flight/control/backend-loss-*.request; do
            [ -f "$REQUEST" ] || continue
            DONE=${REQUEST%.request}.done
            [ ! -f "$DONE" ] || continue
            printf 'Host runner stopping Backend for 8 seconds (%s)\n' "$(basename "$REQUEST")" >>"artifacts/flight/run-${RUN_INDEX}.log"
            $COMPOSE stop --timeout 3 backend >>"artifacts/flight/run-${RUN_INDEX}.log" 2>&1
            sleep 8
            $COMPOSE start backend >>"artifacts/flight/run-${RUN_INDEX}.log" 2>&1
            $COMPOSE up -d --wait backend >>"artifacts/flight/run-${RUN_INDEX}.log" 2>&1
            touch "$DONE"
        done
        sleep .2
    done
    wait "$TEST_PID"
    RESULT=$?
    set -e
    cat "artifacts/flight/run-${RUN_INDEX}.log"
    if [ "$RESULT" -ne 0 ]; then
        return 1
    fi
    $COMPOSE down --volumes --remove-orphans
    STACK_UP=0
}

RUNS=${FLIGHT_RUNS:-2}
INDEX=1
while [ "$INDEX" -le "$RUNS" ]; do
    run_once "$INDEX"
    INDEX=$((INDEX + 1))
done

if [ ! -f .env ]; then
    cp .env.example .env
    ROOT_ENV_CREATED=1
fi
docker compose config --quiet
