"""Повторный аудит 9d02331: реальные flight-команды не выполняются.

Exit 1 означает нарушение контракта. Fixtures подменяют MAVSDK и телеметрию.
"""
import asyncio
import json
import time

from check_hub import fixture
from app.flight import Execution


async def partial_arm_with_delayed_telemetry():
    async with fixture(fail_start=True) as (c, calls, _, _):
        await c.create_session()
        c.session.owner_connected = True

        async def arm_without_telemetry_update():
            calls.append("arm")  # PX4 accepted arm; subscription update is delayed.

        c.record.system.action.arm = arm_without_telemetry_update
        try:
            await c.session_action(c.session.id, "arm")
        except RuntimeError:
            pass
        await asyncio.sleep(.1)
        assert "rtl" in calls or "disarm" in calls, (
            f"No recovery after accepted arm with pre-command telemetry: {calls}; "
            f"session.armed={c.session.armed}, status={c.session.status}")


async def pause_during_arm_preparation():
    async with fixture() as (c, calls, _, _):
        await c.create_session()
        c.session.owner_connected = True
        arming = asyncio.create_task(c.session_action(c.session.id, "arm"))
        await asyncio.sleep(.1)  # inside the 1.1 second setpoint preparation
        c.service.simulation_paused = True
        try: await arming
        except Exception: pass
        assert "arm" not in calls, f"Arm was executed after pause: {calls}"


async def ready_session_is_revoked_on_pause():
    async with fixture() as (c, _, _, _):
        await c.create_session()
        c.service.simulation_paused = True
        await asyncio.sleep(.15)
        assert c.session is None or c.session.status == "paused", (
            "Unarmed reserved session survives pause with usable token")


async def land_during_mission_return():
    async with fixture() as (c, calls, _, _):
        c.active = Execution("e", "r", "mission", status="returning")
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_rtl():
            entered.set()
            await release.wait()
            calls.append("rtl")

        c.record.system.action.return_to_launch = delayed_rtl
        c.active.return_task = asyncio.create_task(c._complete_return(c.active, "cancelled"))
        await entered.wait()
        await c.action("land", "land-request")
        release.set()
        await asyncio.sleep(.1)
        assert calls == ["land"], f"Old return task continued after land: {calls}"


async def watchdog_races_with_return_command():
    async with fixture() as (c, calls, _, _):
        await c.create_session()
        session = c.session
        session.armed, session.status = True, "active"
        session.last_input = time.monotonic() - 1
        entered, release = asyncio.Event(), asyncio.Event()
        original = c._send_velocity

        async def delayed_neutral(*axes):
            entered.set()
            await release.wait()
            await original(*axes)

        c._send_velocity = delayed_neutral
        await entered.wait()
        returning = asyncio.create_task(c.action("return", "return-request"))
        await asyncio.sleep(.05)
        release.set()
        await returning
        await asyncio.sleep(.1)
        assert session.status == "returning", (
            f"Watchdog overwrote RTL state: status={session.status}, calls={calls}")


async def main():
    failed = 0
    for check in (partial_arm_with_delayed_telemetry, pause_during_arm_preparation,
                  ready_session_is_revoked_on_pause, land_during_mission_return,
                  watchdog_races_with_return_command):
        try:
            await asyncio.wait_for(check(), 8)
        except Exception as error:
            failed += 1
            print(json.dumps({"check": check.__name__, "passed": False, "error": str(error)}), flush=True)
        else:
            print(json.dumps({"check": check.__name__, "passed": True}), flush=True)
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
