"""Launch and control one Gazebo Sim world from Python.

Example::

    from app.gazebo.world import World

    world = World("empty")
    world.start("worlds/empty.sdf", gui=True, paused=False)
    world.wait_until_ready()
    try:
        world.pause()
        world.set_physics(max_step_size=0.004, real_time_update_rate=250)
        print(world.get_stats().paused)
        world.resume()
    finally:
        world.stop()

``start`` launches the ``gz sim`` process. The class uses Gazebo Transport
services and protobuf messages to control it; it is not a MAVSDK/PX4 interface.
"""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from collections.abc import Sequence
from typing import Any


_BINDING_PAIRS = ((15, 12), (14, 11), (13, 10), (12, 9), (11, 8))


class GazeboServiceError(RuntimeError):
    """Raised when Gazebo Transport cannot complete a service call."""


def _load_bindings() -> dict[str, Any]:
    errors = []
    for transport_version, msgs_version in _BINDING_PAIRS:
        try:
            transport = importlib.import_module(f"gz.transport{transport_version}")
            prefix = f"gz.msgs{msgs_version}"
            bindings = {"Node": transport.Node}
            for key, module_name, class_name in (
                ("Empty", "empty_pb2", "Empty"),
                ("Double", "double_pb2", "Double"),
                ("StringMsg_V", "stringmsg_v_pb2", "StringMsg_V"),
                ("StringMsg", "stringmsg_pb2", "StringMsg"),
                ("Boolean", "boolean_pb2", "Boolean"),
                ("Entity", "entity_pb2", "Entity"),
                ("EntityFactory", "entity_factory_pb2", "EntityFactory"),
                ("Physics", "physics_pb2", "Physics"),
                ("WorldControl", "world_control_pb2", "WorldControl"),
                ("WorldStatistics", "world_stats_pb2", "WorldStatistics"),
                ("Scene", "scene_pb2", "Scene"),
                ("SdfGeneratorConfig", "sdf_generator_config_pb2", "SdfGeneratorConfig"),
                (
                    "SphericalCoordinates",
                    "spherical_coordinates_pb2",
                    "SphericalCoordinates",
                ),
                ("Pose", "pose_pb2", "Pose"),
                ("Pose_V", "pose_v_pb2", "Pose_V"),
            ):
                module = importlib.import_module(f"{prefix}.{module_name}")
                bindings[key] = getattr(module, class_name)
            sensor_types = {}
            for message_name, module_name, class_name in (
                ("gz.msgs.Image", "image_pb2", "Image"),
                ("gz.msgs.CameraInfo", "camera_info_pb2", "CameraInfo"),
                ("gz.msgs.IMU", "imu_pb2", "IMU"),
                ("gz.msgs.FluidPressure", "fluid_pressure_pb2", "FluidPressure"),
                ("gz.msgs.Fluid", "fluid_pb2", "Fluid"),
                ("gz.msgs.NavSat", "navsat_pb2", "NavSat"),
                ("gz.msgs.Magnetometer", "magnetometer_pb2", "Magnetometer"),
            ):
                try:
                    module = importlib.import_module(f"{prefix}.{module_name}")
                    sensor_types[message_name] = getattr(module, class_name)
                except (ImportError, AttributeError):
                    pass
            bindings["sensor_types"] = sensor_types
            return bindings
        except ImportError as exc:
            errors.append(exc)
    raise RuntimeError(
        "Не найдены совместимые Python bindings Gazebo Transport / Messages. "
        "Установи bindings для версии своего Gazebo Sim."
    ) from (errors[-1] if errors else None)


class World:
    """Управляет состоянием и базовыми настройками одного Gazebo world.

    Args:
        name: Значение ``<world name="...">`` из SDF.
        timeout_ms: Таймаут каждого service request в миллисекундах.
        node: Необязательный Gazebo Transport Node; полезен при совместном
            использовании одного соединения с другими клиентами.

    Raises:
        ValueError: Если имя мира пустое или содержит недопустимый путь.
        RuntimeError: Если Gazebo Python bindings не установлены.
    """

    def __init__(
        self,
        name: str,
        timeout_ms: int = 3000,
        node: Any | None = None,
    ) -> None:
        if not name or "/" in name:
            raise ValueError("name должен быть непустым именем мира без '/' ")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms должен быть больше нуля")

        self.name = name
        self.timeout_ms = timeout_ms
        self._types = _load_bindings()
        self.node = node if node is not None else self._types["Node"]()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        """Процесс ``gz sim``, если его запустил этот объект."""
        return self._process

    def start(
        self,
        sdf_path: str | Path,
        *,
        gui: bool = True,
        paused: bool = False,
        verbosity: int = 2,
        sim_args: Sequence[str] = (),
    ) -> subprocess.Popen[bytes]:
        """Запустить отдельный процесс ``gz sim`` с указанным SDF-миром.

        Args:
            sdf_path: Путь к SDF-файлу. Его ``<world name>`` должен совпадать
                с именем, переданным конструктору.
            gui: Запустить также GUI; при ``False`` используется режим ``-s``.
            paused: Если ``True``, запустить симуляцию на паузе; иначе с ``-r``.
            verbosity: Уровень сообщений Gazebo от 0 до 4.
            sim_args: Дополнительные аргументы CLI ``gz sim``, например
                ``("--iterations", "10000")`` или ``("--seed", "7")``.

        Returns:
            Дочерний процесс Gazebo. Вызови ``wait_until_ready`` перед
            отправкой ему Transport-запросов.
        """
        if self._process is not None and self._process.poll() is None:
            raise RuntimeError("Этот объект World уже запустил Gazebo")
        if not 0 <= verbosity <= 4:
            raise ValueError("verbosity должен быть от 0 до 4")

        path = Path(sdf_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        try:
            sdf_root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"Некорректный SDF-файл {path}: {exc}") from exc
        world_names = [world.get("name") for world in sdf_root.findall("world")]
        if self.name not in world_names:
            raise ValueError(
                f"SDF {path} не содержит мир {self.name!r}; "
                f"найдены: {world_names}"
            )

        command = ["gz", "sim", "-v", str(verbosity), "--headless-rendering", "ogre2"]
        if not gui:
            command.append("-s")
        if not paused:
            command.append("-r")
        command.extend(str(arg) for arg in sim_args)
        command.append(str(path))
        try:
            self._process = subprocess.Popen(command)
        except FileNotFoundError as exc:
            raise RuntimeError("Не найдена команда `gz`; установи Gazebo Sim") from exc
        return self._process

    def wait_until_ready(self, timeout_ms: int = 15000) -> None:
        """Дождаться регистрации world control service в Gazebo Transport."""
        if timeout_ms <= 0:
            raise ValueError("timeout_ms должен быть больше нуля")
        endpoint = self._world_service("control")
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(
                    f"Gazebo завершился с кодом {self._process.returncode} "
                    "до готовности мира"
                )
            if endpoint in self.node.service_list():
                return
            time.sleep(0.1)
        raise TimeoutError(
            f"Мир {self.name!r} не стал доступен за {timeout_ms} мс; "
            "проверь логи Gazebo, имя мира и GZ_PARTITION."
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Остановить только тот Gazebo-процесс, который запустил этот объект."""
        if timeout < 0:
            raise ValueError("timeout должен быть неотрицательным")
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    @staticmethod
    def stop_all(timeout: float = 5.0) -> int:
        """Остановить все локальные процессы ``gz sim``.

        Метод полезен, когда симуляцию запустили вручную или через другой
        экземпляр ``World``. Сначала отправляет SIGTERM, после таймаута —
        SIGKILL. Возвращает количество найденных процессов. На Linux процессы
        обнаруживаются через ``/proc``; другие команды ``gz`` не затрагиваются.
        """
        if timeout < 0:
            raise ValueError("timeout должен быть неотрицательным")

        def find_gz_sim_pids() -> set[int]:
            pids: set[int] = set()
            try:
                entries = os.scandir("/proc")
            except OSError as exc:
                raise RuntimeError(
                    "Не удалось прочитать /proc для поиска gz sim"
                ) from exc
            with entries:
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    try:
                        raw = Path(entry.path, "cmdline").read_bytes()
                    except (FileNotFoundError, PermissionError, ProcessLookupError):
                        continue
                    args = [
                        arg.decode(errors="replace")
                        for arg in raw.split(b"\0")
                        if arg
                    ]
                    if (
                        len(args) >= 2
                        and Path(args[0]).name == "gz"
                        and args[1] == "sim"
                    ):
                        pids.add(int(entry.name))
            return pids

        pids = find_gz_sim_pids()
        target_count = len(pids)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        deadline = time.monotonic() + timeout
        while pids and time.monotonic() < deadline:
            alive: set[int] = set()
            for pid in pids:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                alive.add(pid)
            pids = alive
            if pids:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return target_count

    def _request(
        self,
        endpoint: str,
        request: Any,
        request_type: Any,
        response_type: Any,
    ) -> Any:
        executed, response = self.node.request(
            endpoint,
            request,
            request_type,
            response_type,
            self.timeout_ms,
        )
        if not executed:
            raise GazeboServiceError(
                f"Вызов {endpoint} не выполнен. Проверь, что мир "
                f"{self.name!r} запущен и endpoint доступен."
            )
        if hasattr(response, "data") and isinstance(response.data, bool):
            if not response.data:
                raise GazeboServiceError(f"Gazebo отклонил запрос {endpoint}")
        return response

    def _world_service(self, suffix: str) -> str:
        return f"/world/{self.name}/{suffix.lstrip('/')}"

    def _query_world_names(self, timeout_ms: int) -> list[str] | None:
        """Return world names, or None when no server answers the query."""
        executed, response = self.node.request(
            "/gazebo/worlds",
            self._types["Empty"](),
            self._types["Empty"],
            self._types["StringMsg_V"],
            timeout_ms,
        )
        if not executed:
            return None
        return list(response.data)

    def server_exists(self, timeout_ms: int | None = None) -> bool:
        """Проверить, отвечает ли Gazebo server на запрос списка миров.

        Возвращает ``False``, если сервис ``/gazebo/worlds`` не ответил за
        указанный таймаут. Это проверка доступности сервера по Gazebo
        Transport, а не проверка локального процесса операционной системы.
        """
        timeout_ms = self.timeout_ms if timeout_ms is None else timeout_ms
        if timeout_ms <= 0:
            raise ValueError("timeout_ms должен быть больше нуля")
        return self._query_world_names(timeout_ms) is not None

    def world_exists(self, timeout_ms: int | None = None) -> bool:
        """Проверить, загружен ли мир с именем, переданным в конструктор."""
        timeout_ms = self.timeout_ms if timeout_ms is None else timeout_ms
        if timeout_ms <= 0:
            raise ValueError("timeout_ms должен быть больше нуля")
        names = self._query_world_names(timeout_ms)
        return names is not None and self.name in names

    def pause(self) -> None:
        """Поставить симуляционное время мира на паузу."""
        request = self._types["WorldControl"](pause=True)
        self._request(
            self._world_service("control"),
            request,
            self._types["WorldControl"],
            self._types["Boolean"],
        )

    def resume(self) -> None:
        """Продолжить симуляцию мира."""
        request = self._types["WorldControl"](pause=False)
        self._request(
            self._world_service("control"),
            request,
            self._types["WorldControl"],
            self._types["Boolean"],
        )

    def step(self, iterations: int = 1) -> None:
        """Выполнить заданное число шагов симуляции (обычно на паузе)."""
        if iterations < 1:
            raise ValueError("iterations должен быть не меньше 1")
        request = self._types["WorldControl"](step=True, multi_step=iterations)
        self._request(
            self._world_service("control"),
            request,
            self._types["WorldControl"],
            self._types["Boolean"],
        )

    def reset(
        self,
        *,
        all: bool = False,
        time_only: bool = False,
        model_only: bool = False,
    ) -> None:
        """Сбросить всё, время или состояния моделей.

        Если флаги не заданы, ``all=True`` сбрасывает мир целиком.
        Для каждого из трёх режимов одновременно допускается только один флаг.
        """
        selected = sum((all, time_only, model_only))
        if selected > 1:
            raise ValueError("Выбери только один вариант reset")
        request = self._types["WorldControl"]()
        request.reset.all = all or selected == 0
        request.reset.time_only = time_only
        request.reset.model_only = model_only
        self._request(
            self._world_service("control"),
            request,
            self._types["WorldControl"],
            self._types["Boolean"],
        )

    def get_stats(self, timeout_ms: int | None = None) -> Any:
        """Получить следующий sample статистики: пауза, время и real-time factor."""
        timeout_ms = timeout_ms or self.timeout_ms
        topic = self._world_service("stats")
        received = threading.Event()
        messages = []

        def callback(message: Any) -> None:
            messages.append(message)
            received.set()

        if not self.node.subscribe(self._types["WorldStatistics"], topic, callback):
            raise GazeboServiceError(f"Не удалось подписаться на {topic}")
        try:
            if not received.wait(timeout_ms / 1000):
                raise TimeoutError(f"За {timeout_ms} мс не получена статистика {topic}")
            return messages[0]
        finally:
            self.node.unsubscribe(topic)

    def set_physics(
        self,
        *,
        max_step_size: float | None = None,
        real_time_factor: float | None = None,
        real_time_update_rate: float | None = None,
        gravity: tuple[float, float, float] | None = None,
        magnetic_field: tuple[float, float, float] | None = None,
        enable_physics: bool | None = None,
    ) -> None:
        """Изменить переданные параметры физики мира.

        Неуказанные поля не заполняются этим клиентом. Частоту реальной выдачи
        сенсора всё равно ограничивают шаг физики и фактическая скорость мира.
        """
        request = self._types["Physics"]()
        values = {
            "max_step_size": max_step_size,
            "real_time_factor": real_time_factor,
            "real_time_update_rate": real_time_update_rate,
            "enable_physics": enable_physics,
        }
        for field, value in values.items():
            if value is not None:
                setattr(request, field, value)
        for field, value in (
            ("gravity", gravity),
            ("magnetic_field", magnetic_field),
        ):
            if value is not None:
                if len(value) != 3:
                    raise ValueError(f"{field} должен содержать три координаты")
                vector = getattr(request, field)
                vector.x, vector.y, vector.z = value
        if all(value is None for value in values.values()) and gravity is None and magnetic_field is None:
            raise ValueError("Передай хотя бы одну настройку физики")
        self._request(
            self._world_service("set_physics"),
            request,
            self._types["Physics"],
            self._types["Boolean"],
        )

    def set_spherical_coordinates(
        self,
        latitude_deg: float,
        longitude_deg: float,
        elevation: float,
        heading_deg: float = 0.0,
        surface_model: str = "EARTH_WGS84",
    ) -> None:
        """Задать географическую привязку локального начала мира."""
        message_type = self._types["SphericalCoordinates"]
        enum_value = message_type.DESCRIPTOR.fields_by_name[
            "surface_model"
        ].enum_type.values_by_name.get(surface_model)
        if enum_value is None:
            valid = ", ".join(
                message_type.DESCRIPTOR.fields_by_name[
                    "surface_model"
                ].enum_type.values_by_name
            )
            raise ValueError(f"surface_model должен быть одним из: {valid}")
        request = message_type(
            surface_model=enum_value.number,
            latitude_deg=latitude_deg,
            longitude_deg=longitude_deg,
            elevation=elevation,
            heading_deg=heading_deg,
        )
        self._request(
            self._world_service("set_spherical_coordinates"),
            request,
            message_type,
            self._types["Boolean"],
        )

    def set_pose(self, pose: Any) -> None:
        """Установить позу сущности мира; pose — сообщение ``gz.msgs.Pose``."""
        if pose.DESCRIPTOR.full_name != "gz.msgs.Pose":
            raise TypeError("pose должен быть экземпляром gz.msgs.Pose")
        self._request(
            self._world_service("set_pose"),
            pose,
            self._types["Pose"],
            self._types["Boolean"],
        )


__all__ = ["World", "GazeboServiceError"]
