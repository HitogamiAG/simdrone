"""Typed public boundary around the pinned Gazebo Harmonic World wrapper."""
from .world import World
from .transport import Node
from .state import SerializedStepMap


class GazeboClient:
    def __init__(self, world_name: str, timeout_ms: int):
        self._world = World(world_name, timeout_ms=timeout_ms, node=Node())

    @property
    def sensor_types(self):
        return self._world._types["sensor_types"]

    def message_type(self, name: str):
        return self._world._types[name]

    def service(self, name: str):
        return self._world._world_service(name)

    def request(self, service, request, request_type, response_type, timeout_ms=None):
        if timeout_ms is None:
            return self._world._request(service, request, request_type, response_type)
        ok, response = self._world.node.request(service, request, request_type, response_type, timeout_ms)
        if not ok:
            raise RuntimeError(f"Gazebo request failed: {service}")
        return response

    def request_transport(self, service, request, request_type, response_type, timeout_ms):
        return self._world.node.request(service, request, request_type, response_type, timeout_ms)

    def subscribe(self, msg_type, topic, callback):
        return self._world.node.subscribe(msg_type, topic, callback)

    def unsubscribe(self, topic):
        return self._world.node.unsubscribe(topic)

    def topic_list(self):
        return self._world.node.topic_list()

    def topic_info(self, topic):
        return self._world.node.topic_info(topic)

    def service_list(self):
        return self._world.node.service_list()

    def snapshot(self, timeout_ms: int):
        empty = self.message_type("Empty")()
        ok, snapshot = self.request_transport(self.service("state"), empty, self.message_type("Empty"), SerializedStepMap, timeout_ms)
        if not ok:
            raise RuntimeError("Gazebo component state is unavailable")
        return snapshot

    def scene_info(self, timeout_ms: int):
        empty = self.message_type("Empty")()
        ok, scene = self.request_transport(self.service("scene/info"), empty, self.message_type("Empty"), self.message_type("Scene"), timeout_ms)
        if not ok:
            raise RuntimeError("Gazebo scene info is unavailable")
        return scene

    def export_world_sdf(self):
        config = self.message_type("SdfGeneratorConfig")()
        return self.request(self.service("generate_world_sdf"), config, self.message_type("SdfGeneratorConfig"), self.message_type("StringMsg")).data

    def create_model(self, name: str, pose, *, sdf: str | None = None, sdf_filename: str | None = None):
        entity_factory = self.message_type("EntityFactory")
        request = entity_factory(name=name, allow_renaming=False)
        if sdf:
            request.sdf = sdf
        elif sdf_filename:
            request.sdf_filename = sdf_filename
        else:
            raise ValueError("sdf or sdf_filename is required")
        request.pose.name = ""
        request.pose.position.x, request.pose.position.y, request.pose.position.z = pose.position.values()
        request.pose.orientation.x = pose.orientation.x
        request.pose.orientation.y = pose.orientation.y
        request.pose.orientation.z = pose.orientation.z
        request.pose.orientation.w = pose.orientation.w
        self.request(self.service("create/blocking"), request, entity_factory, self.message_type("Boolean"))

    def remove_model(self, name: str):
        entity_type = self.message_type("Entity")
        entity = entity_type(name=name, type=entity_type.MODEL)
        self.request(self.service("remove/blocking"), entity, entity_type, self.message_type("Boolean"))

    def set_process(self, process):
        self._world._process = process

    def __getattr__(self, name):
        # The pinned wrapper owns command semantics such as pause, set_pose and
        # settings updates. Private wrapper state stays behind this boundary.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._world, name)
