"""Sequential live integration test; run inside the Compose network."""
import time
import httpx

BASE = "http://backend:8003/api/v1"


def request(client, method, path, **kwargs):
    url = path if path.startswith("http://") else BASE + path
    response = client.request(method, url, timeout=150, **kwargs)
    if response.is_error:
        raise AssertionError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json() if response.content else None


def main():
    with httpx.Client() as client:
        initial = request(client, "GET", "/world")
        assert not request(client, "GET", "/drones/")
        assert request(client, "GET", "/system/status")["backend"]["ready"]
        created = request(client, "POST", "/drones/", json={
            "model": "x500_gimbal", "name": f"backend_it_{int(time.time())}",
            "spawn_pad_id": "landing_pad_01",
        })
        did = created["id"]
        assert created["status"] == "ready" and created["simulation"]["pose"]
        original_pose = created["simulation"]["pose"]
        params = request(client, "GET", f"/drones/{did}/autopilot/parameters")
        old = params["MPC_XY_VEL_MAX"]
        changed = max(1.0, old * 0.8)
        request(client, "PATCH", f"/drones/{did}/autopilot/parameters", json={"MPC_XY_VEL_MAX": changed})
        request(client, "POST", f"/drones/{did}/autopilot/stop")
        assert request(client, "GET", f"/drones/{did}/")["binding"]["valid"]
        request(client, "POST", f"/drones/{did}/autopilot/start")
        assert abs(request(client, "GET", f"/drones/{did}/autopilot/parameters")["MPC_XY_VEL_MAX"] - changed) < 1e-4
        request(client, "POST", f"/drones/{did}/autopilot/restart")
        assert abs(request(client, "GET", f"/drones/{did}/autopilot/parameters")["MPC_XY_VEL_MAX"] - changed) < 1e-4

        sensors = request(client, "GET", f"/drones/{did}/sensors/")
        imu = next(sensor for sensor in sensors if sensor["type"] == "imu")
        base_rate = imu.get("update_rate")
        assert base_rate and base_rate > 0
        new_rate = base_rate / 2
        request(client, "PATCH", f"/drones/{did}/sensors/{imu['id']}/", json={"update_rate": new_rate})
        assert abs(request(client, "GET", f"/drones/{did}/sensors/{imu['id']}/")["update_rate"] - new_rate) < 1e-4

        reset = request(client, "POST", f"/drones/{did}/reset")
        assert reset["id"] == did and reset["simulation"]["pose"]
        assert abs(reset["simulation"]["pose"]["position"]["z"] - original_pose["position"]["z"]) < 0.1
        params_after_reset = request(client, "GET", f"/drones/{did}/autopilot/parameters")
        assert abs(params_after_reset["MPC_XY_VEL_MAX"] - old) < 1e-4
        sensor_after_reset = request(client, "GET", f"/drones/{did}/sensors/{imu['id']}/")
        assert abs(sensor_after_reset["update_rate"] - base_rate) < 1e-4

        second = request(client, "POST", "/drones/", json={
            "model": "x500_gimbal", "name": f"backend_it_second_{int(time.time())}",
            "spawn_pad_id": "landing_pad_02",
        })
        second_id = second["id"]
        assert second["autopilot"]["px4"]["telemetry_fresh"]
        first_value, second_value = 8.0, 7.0
        request(client, "PATCH", f"/drones/{did}/autopilot/parameters", json={"MPC_XY_VEL_MAX": first_value})
        request(client, "PATCH", f"/drones/{second_id}/autopilot/parameters", json={"MPC_XY_VEL_MAX": second_value})
        first_parameters = request(client, "GET", f"/drones/{did}/autopilot/parameters")
        second_parameters = request(client, "GET", f"/drones/{second_id}/autopilot/parameters")
        assert abs(first_parameters["MPC_XY_VEL_MAX"] - first_value) < 1e-4
        assert abs(second_parameters["MPC_XY_VEL_MAX"] - second_value) < 1e-4
        assert request(client, "GET", f"/drones/{did}/")["autopilot"]["px4"]["telemetry_fresh"]
        assert request(client, "GET", f"/drones/{second_id}/")["autopilot"]["px4"]["telemetry_fresh"]

        request(client, "POST", "/world/pause")
        denied = client.post(BASE + "/drones/", json={"model": "x500_gimbal", "spawn_pad_id": "landing_pad_02"}, timeout=10)
        assert denied.status_code == 409 and denied.json()["error"]["code"] == "simulation_paused"
        request(client, "POST", "/world/resume")

        result = request(client, "PATCH", "/world", json={"physics": {"real_time_factor": 1.0}})
        assert result["applied"] == ["physics"]
        assert request(client, "GET", "/drones/") == []
        assert request(client, "GET", "/world")["physics"]["real_time_factor"] == 1.0
        stale = client.get(BASE + f"/drones/{did}/", timeout=10)
        assert stale.status_code == 404
        stale_second = client.get(BASE + f"/drones/{second_id}/", timeout=10)
        assert stale_second.status_code == 404
        assert request(client, "GET", "http://px4-hub:8002/api/v1/instances/") == []
        request(client, "POST", "/world/reset")
        assert request(client, "GET", "/world")["physics"]["real_time_factor"] == initial["physics"]["real_time_factor"]
    print("PASS backend integration: two drones, independent PX4 parameters, sensor reset, pause, world patch/reset")


if __name__ == "__main__":
    main()
