"""Allowlisted model catalog and SDF-derived sensor defaults."""
import xml.etree.ElementTree as ET
from pathlib import Path
from .config import Settings, ROOT, LOCAL_ROOT

class ModelCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings

    def models(self) -> dict[str, Path]:
        models = {}
        for root in self.settings.model_roots:
            if not root.exists():
                continue
            for sdf in root.glob("*/model.sdf"):
                try:
                    model = ET.parse(sdf).getroot().find("model")
                    if model is not None and model.get("name"):
                        models[model.get("name")] = sdf
                except (OSError, ET.ParseError):
                    continue
        for root in self.settings.model_roots:
            candidate = root / "x500_gimbal/model.sdf"
            if candidate.is_file():
                models["x500_gimbal"] = candidate
        test_model = ROOT / "app/models/test_quad/model.sdf"
        local_test = LOCAL_ROOT / "app/models/test_quad/model.sdf"
        models["test_quad"] = test_model if test_model.exists() else local_test
        return models

    def initial_sensor_rate(self, model_path: Path | None, sensor_name: str, initial_sdf: str | None = None):
        if initial_sdf:
            root = ET.fromstring(initial_sdf)
            for sensor in root.findall(".//sensor"):
                if sensor.get("name") == sensor_name:
                    return float(sensor.findtext("update_rate", "0"))
        pending = [model_path] if model_path else []
        visited = set()
        roots = [*self.settings.model_roots, ROOT / "app/models", LOCAL_ROOT / "app/models"]
        while pending:
            path = pending.pop()
            resolved = path.resolve()
            if resolved in visited or not path.is_file():
                continue
            visited.add(resolved)
            try:
                root = ET.parse(path).getroot()
            except (OSError, ET.ParseError):
                continue
            for sensor in root.findall(".//sensor"):
                if sensor.get("name") == sensor_name:
                    raw_rate = sensor.findtext("update_rate")
                    try:
                        return float(raw_rate) if raw_rate else None
                    except ValueError:
                        return None
            for uri in root.findall(".//include/uri"):
                value = (uri.text or "").strip()
                if value.startswith("model://"):
                    name = value[len("model://"):].split("/", 1)[0]
                    pending.extend(base / name / "model.sdf" for base in roots)
        return None
