"""Tracks observed Gazebo poses and confirms pose commands by sequence."""
import math
import threading
import time
from datetime import datetime, timezone
from ..errors import ApiFault
from ..geometry import Pose, Quaternion, Vector3
# Generated Pose_V schemas import their nested dependencies from ``gz.msgs``.
# Register the nested class through that same canonical package: importing it
# through the ``gz.msgs10`` compatibility alias leaves Pose_V.pose unresolved.
from gz.msgs.pose_pb2 import Pose as _PoseMessage  # noqa: F401
from gz.msgs.pose_v_pb2 import Pose_V

class PoseTracker:
    def __init__(self, client, world_name: str, generation):
        self.client = client
        self.world_name = world_name
        self._generation = generation
        self.condition = threading.Condition()
        self.poses = {}
        self.sequence = 0
        self._node = None
        self._subscribed = False

    def start(self):
        self._node = self.client
        generation = self._generation()
        def callback(msg):
            with self.condition:
                if generation != self._generation():
                    return
                self.sequence += 1
                for pose in msg.pose:
                    self.poses[pose.id] = (self.from_proto(pose), self.sequence, time.monotonic(),
                                           datetime.now(timezone.utc).isoformat())
                self.condition.notify_all()
        if not self.client.subscribe(Pose_V, self.client.service("pose/info"), callback):
            raise RuntimeError("Cannot subscribe to Gazebo pose/info")
        self._subscribed = True

    def current(self, entity_id, after=0, timeout=3):
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                value = self.poses.get(entity_id)
                if value and value[1] > after:
                    return value[0].model_copy(deep=True)
                self.condition.wait(max(0, deadline - time.monotonic()))
        raise ApiFault(504, "pose_timeout", "Gazebo did not publish a current pose", {"entity_id": entity_id})

    def latest(self, entity_id):
        """Return a non-blocking copy of the newest pose and its source age."""
        with self.condition:
            value = self.poses.get(entity_id)
            if value is None:
                return None
            pose, sequence, received_mono, received_at = value
            return {"pose": pose.model_copy(deep=True), "sequence": sequence,
                    "age_s": max(0.0, time.monotonic() - received_mono),
                    "received_at": received_at}

    def stop(self):
        if self._subscribed:
            self.client.unsubscribe(self.client.service("pose/info"))
            self._subscribed = False
        self._node = None
        with self.condition:
            self.poses.clear()
            self.condition.notify_all()

    @staticmethod
    def from_proto(pose):
        return Pose(position=Vector3(x=pose.position.x, y=pose.position.y, z=pose.position.z), orientation=Quaternion(x=pose.orientation.x, y=pose.orientation.y, z=pose.orientation.z, w=pose.orientation.w))

    @staticmethod
    def matches(actual, expected):
        position_ok = all(abs(a-b) < .02 for a, b in zip(actual.position.values(), expected.position.values()))
        dot = sum(getattr(actual.orientation, k) * getattr(expected.orientation, k) for k in ("x", "y", "z", "w"))
        return position_ok and abs(dot) > math.cos(.01 / 2)
