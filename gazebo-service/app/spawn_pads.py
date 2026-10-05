"""Read the allowlisted spawn-pad frames from the configured world SDF."""
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path


PREFIX = "spawn_pad__"
MODEL_URI = "model://drone_pad"


def _pad_dimensions(model_roots):
    for root in model_roots:
        path = Path(root) / "drone_pad" / "model.sdf"
        if not path.is_file():
            continue
        model = ET.parse(path).getroot().find("model")
        if model is None or model.get("name") != "drone_pad":
            break
        for collision in model.findall(".//collision"):
            values = (collision.findtext("geometry/box/size") or "").split()
            if len(values) == 3:
                dimensions = [float(value) for value in values]
                if all(math.isfinite(value) and value > 0 for value in dimensions):
                    return dict(zip(("x", "y", "z"), dimensions))
        break
    raise ValueError("model://drone_pad has no valid box collision in configured model roots")


def _pose(text):
    values = [float(value) for value in (text or "0 0 0 0 0 0").split()]
    if len(values) != 6 or not all(math.isfinite(value) for value in values):
        raise ValueError("pose must contain six finite values")
    return values


def _world_pose(base, local):
    x, y, z, roll, pitch, yaw = base
    lx, ly, lz, lr, lp, lyaw = local
    if abs(roll) > 1e-8 or abs(pitch) > 1e-8 or abs(lr) > 1e-8 or abs(lp) > 1e-8:
        raise ValueError("spawn pads must be horizontal; roll and pitch are unsupported")
    return [x + math.cos(yaw) * lx - math.sin(yaw) * ly,
            y + math.sin(yaw) * lx + math.cos(yaw) * ly, z + lz,
            0.0, 0.0, yaw + lyaw]


def _orientation(yaw):
    return {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2), "w": math.cos(yaw / 2)}


def read_spawn_pads(world_path, world_name, model_roots):
    try:
        root = ET.parse(world_path).getroot()
        world = root.find(f"world[@name='{world_name}']")
        if world is None:
            raise ValueError(f"SDF does not contain world {world_name!r}")
        dimensions = _pad_dimensions(model_roots)
        includes = {}
        for include in world.findall("include"):
            uri = (include.findtext("uri") or "").strip()
            name = include.findtext("name")
            if uri == MODEL_URI:
                if (not name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", name)
                        or name in includes):
                    raise ValueError("each drone_pad include needs a unique name")
                include_pose = include.find("pose")
                if include_pose is not None and include_pose.get("relative_to") not in (None, "world"):
                    raise ValueError("drone_pad include pose must be relative to world")
                includes[name] = _pose(include.findtext("pose"))
            elif name and name.startswith(PREFIX):
                raise ValueError(f"spawn pad {name!r} must include {MODEL_URI}")
        pads = []
        seen = set()
        for frame in world.findall("frame"):
            frame_name = frame.get("name", "")
            if not frame_name.startswith(PREFIX):
                continue
            pad_id = frame_name[len(PREFIX):]
            attached = frame.get("attached_to")
            pose = frame.find("pose")
            relative_to = pose.get("relative_to") if pose is not None else None
            if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", pad_id)
                    or pad_id in seen or pad_id not in includes or attached != pad_id
                    or relative_to != pad_id):
                raise ValueError(f"invalid spawn-pad frame {frame_name!r}")
            seen.add(pad_id)
            base = includes[pad_id]
            local = _pose(pose.text if pose is not None else None)
            if abs(local[0]) > 1e-6 or abs(local[1]) > 1e-6:
                raise ValueError(f"spawn frame for {pad_id!r} must be centered over the pad")
            if not dimensions["z"] - .1 <= local[2] <= dimensions["z"] + .1:
                raise ValueError(f"spawn frame for {pad_id!r} must place drone chassis on pad surface")
            spawn = _world_pose(base, local)
            surface = _world_pose(base, [0, 0, dimensions["z"], 0, 0, 0])
            pads.append({"id": pad_id, "name": pad_id, "model": "drone_pad",
                         "pose": {"position": {"x": base[0], "y": base[1], "z": base[2]},
                                  "orientation": _orientation(base[5])},
                         "surface_pose": {"position": {"x": surface[0], "y": surface[1], "z": surface[2]},
                                          "orientation": _orientation(surface[5])},
                         "size": dimensions.copy(),
                         "spawn_pose": {"position": {"x": spawn[0], "y": spawn[1], "z": spawn[2]},
                                        "orientation": _orientation(spawn[5])},
                         "supported_models": ["x500_gimbal"]})
        if len(seen) != len(includes):
            raise ValueError("each drone_pad include needs exactly one spawn_pad__<include-name> frame")
        return sorted(pads, key=lambda pad: pad["id"])
    except (OSError, ET.ParseError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid spawn-pad configuration: {exc}") from exc
