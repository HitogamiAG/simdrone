"""Decode the Harmonic ECM snapshot; SDF export is not a live settings source."""
from gz.msgs10.physics_pb2 import Physics
from gz.msgs10.spherical_coordinates_pb2 import SphericalCoordinates
from gz.msgs10.scene_pb2 import Scene
from gz.msgs10.serialized_map_pb2 import SerializedStepMap


def component_id(name: str) -> int:
    # gz::common::hash64 (FNV-1a), used by the component Factory.
    value = 0xcbf29ce484222325
    for byte in f"gz_sim_components.{name}".encode():
        value = ((value ^ byte) * 0x100000001b3) & 0xffffffffffffffff
    return value


def components(entity):
    return {item.type: item.component for item in entity.components.values() if not item.remove}


def decode_world(snapshot, name: str) -> dict:
    for entity in snapshot.state.entities.values():
        data = components(entity)
        if component_id("World") not in data or data.get(component_id("Name"), b"").decode() != name:
            continue
        physics = Physics.FromString(data[component_id("Physics")])
        gravity = [float(v) for v in data[component_id("Gravity")].decode().split()]
        spherical = None
        if component_id("SphericalCoordinates") in data:
            msg = SphericalCoordinates.FromString(data[component_id("SphericalCoordinates")])
            enum = msg.DESCRIPTOR.fields_by_name["surface_model"].enum_type
            spherical = {"latitude_deg": msg.latitude_deg, "longitude_deg": msg.longitude_deg,
                         "elevation": msg.elevation, "heading_deg": msg.heading_deg,
                         "surface_model": enum.values_by_number[msg.surface_model].name}
        scene = Scene.FromString(data[component_id("Scene")]) if component_id("Scene") in data else None
        magnetic = data.get(component_id("MagneticField"))
        return {"physics": {"max_step_size": physics.max_step_size, "real_time_factor": physics.real_time_factor},
                "gravity": gravity, "spherical_coordinates": spherical,
                "magnetic_field": [float(v) for v in magnetic.decode().split()] if magnetic else None,
                "scene": scene, "entity_id": entity.id}
    raise ValueError(f"World {name!r} missing from Gazebo state")


def model_sdf(snapshot, entity_id: int) -> str | None:
    entity = snapshot.state.entities.get(entity_id)
    if entity is None:
        return None
    raw = components(entity).get(component_id("ModelSdf"))
    return raw.decode() if raw else None
