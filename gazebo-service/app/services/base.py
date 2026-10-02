from ..errors import ApiFault

class ServiceContext:
    """Provides operation services shared state and the public Gazebo client."""
    def __init__(self, runtime):
        self.runtime = runtime

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def _snapshot(self):
        if not self.world:
            raise ApiFault(503, "gazebo_unavailable", "Gazebo is not running")
        for _ in range(2):
            try:
                return self.world.snapshot(max(self.settings.request_timeout_ms, 6500))
            except Exception:
                continue
        raise ApiFault(503, "gazebo_unavailable", "Cannot read Gazebo component state")

    def _scene(self):
        if not self.world:
            raise ApiFault(503, "gazebo_unavailable", "Gazebo is not running")
        try:
            return self.world.scene_info(self.settings.request_timeout_ms)
        except Exception as exc:
            raise ApiFault(503, "gazebo_unavailable", "Gazebo scene service did not respond") from exc

    def _ensure_ready(self):
        if not self.world or not self.status()["world_ready"]:
            raise ApiFault(503, "gazebo_unavailable", "Gazebo world is not ready")
