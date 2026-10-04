import asyncio
import os
from pathlib import Path
from .processes import prepare_rootfs, stop_process
from .telemetry import TelemetryFanout
from .live_logs import LiveLogs, LineDecoder


class Px4Adapter:
    def __init__(self, settings):
        self.settings = settings

    async def start(self, record):
        from mavsdk_grpc import System
        workdir = record.workdir
        workdir.mkdir(parents=True, exist_ok=True)
        if not any(workdir.iterdir()):
            prepare_rootfs(self.settings.px4_rootfs, workdir)
        server_command = [self.settings.mavsdk_server_binary, "-p", str(record.grpc_port),
                         "--sysid", "245", "--compid", "190", f"udpin://127.0.0.1:{record.udp_port}"]
        with (workdir / "mavsdk.log").open("ab") as log:
            record.mavsdk_process = await asyncio.create_subprocess_exec(
                *server_command, cwd=workdir, stdout=log, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
        env = {
            **os.environ,
            "PX4_GZ_STANDALONE": "1", "PX4_GZ_MODEL_NAME": record.gazebo_model,
            "PX4_SYS_AUTOSTART": "4019", "PX4_SIM_MODEL": "x500_gimbal",
            "PX4_SIMULATOR": "gz", "GZ_PARTITION": self.settings.partition,
        }
        record.logs = LiveLogs("px4")
        record.px4_process = await asyncio.create_subprocess_exec(
                str(self.settings.px4_binary), "-i", str(record.slot), cwd=workdir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        record.log_reader = asyncio.create_task(self._read_output(record.px4_process, record.logs, workdir / "px4.log"))
        # MAVSDK only exposes its gRPC server after discovering a MAVLink system.
        # Start PX4 first so connect() cannot wait for a server that is waiting for PX4.
        record.system = System(mavsdk_server_address="127.0.0.1", port=record.grpc_port)
        await record.system.connect()
        record.telemetry = TelemetryFanout(record.system, record.id)
        record.telemetry.start()
        try:
            await asyncio.wait_for(asyncio.gather(
                record.telemetry.connection_received.wait(),
                record.telemetry.position_received.wait(),
            ), self.settings.startup_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("PX4 did not produce position telemetry before startup timeout") from exc
        if record.px4_process.returncode is not None or record.mavsdk_process.returncode is not None:
            raise RuntimeError("PX4 or MAVSDK process exited during startup")

    async def _read_output(self, process, logs, path):
        decoder = LineDecoder(logs)
        try:
            with path.open("ab", buffering=0) as logfile:
                while chunk := await process.stdout.read(4096):
                    try:
                        logfile.write(chunk)
                    except OSError:
                        pass
                    decoder.feed(chunk)
                decoder.finish()
        finally:
            logs.close(discard=False)

    async def stop(self, record):
        if record.telemetry:
            await record.telemetry.close()
        await stop_process(record.px4_process, self.settings.stop_timeout)
        await stop_process(record.mavsdk_process, self.settings.stop_timeout)
        if record.logs:
            record.logs.close()
        if record.log_reader:
            record.log_reader.cancel()
            await asyncio.gather(record.log_reader, return_exceptions=True)
            record.log_reader = None
        record.px4_process = record.mavsdk_process = None
        record.system = record.telemetry = None

    async def get_parameters(self, record):
        result = {}
        for name in ("MPC_XY_VEL_MAX", "MPC_Z_VEL_MAX_UP", "MPC_Z_VEL_MAX_DN"):
            result[name] = await record.system.param.get_param_float(name)
        return result

    async def set_parameter(self, record, name, value):
        await record.system.param.set_param_float(name, value)
        observed = await record.system.param.get_param_float(name)
        if abs(observed - value) > max(1e-4, abs(value) * 1e-4):
            raise RuntimeError(f"Parameter {name} was not confirmed")
        return observed
