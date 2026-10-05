"""Blender background verification; does not save changes to the source."""
from pathlib import Path
import hashlib
import json
import bpy
from mathutils import Vector

root = Path(__file__).resolve().parent
manifest = json.loads((root / 'manifest.json').read_text())
for name, expected in manifest['sha256'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
scene = bpy.context.scene
assert [s.name for s in bpy.data.scenes] == ['empty']
assert scene['package_version'] == '1.1.0'
mesh_objects = [o for o in scene.objects if o.type == 'MESH']
assert len(mesh_objects) == 61, len(mesh_objects)
assert {pad_id: tuple(round(v, 6) for v in scene.objects[f'{pad_id}__platform_body'].dimensions)
        for pad_id in ('landing_pad_01', 'landing_pad_02')} == {
            'landing_pad_01': (2.5, 2.5, .3), 'landing_pad_02': (2.5, 2.5, .3)}
assert abs(scene.objects['landing_pad_01__platform_body'].location.x + 3) < 1e-6
assert abs(scene.objects['landing_pad_02__platform_body'].location.x - 3) < 1e-6
assert scene.unit_settings.scale_length == 1
ground = scene.objects['ground_plane']
assert tuple(ground.scale) == (1, 1, 1)
source = sorted(tuple(ground.matrix_world @ v.co) for v in ground.data.vertices)
bpy.ops.import_scene.gltf(filepath=str(root / 'world.glb'))
imported = [o for o in bpy.context.selected_objects if o.type == 'MESH']
assert len(imported) == len(mesh_objects) == 61
errors = []
remaining = imported.copy()
for original in mesh_objects:
    expected = sorted(tuple(original.matrix_world @ Vector(v)) for v in original.bound_box)
    candidates = []
    for exported in remaining:
        actual = sorted(tuple(exported.matrix_world @ Vector(v)) for v in exported.bound_box)
        if len(expected) == len(actual):
            candidates.append((max(abs(a-b) for p, q in zip(expected, actual)
                                   for a, b in zip(p, q)), exported))
    assert candidates, original.name
    error, match = min(candidates, key=lambda item: item[0])
    errors.append(error)
    remaining.remove(match)
error = max(errors)
assert error <= 0.02, (source, actual)
print(json.dumps({'status': 'ok', 'glb_roundtrip_max_error_m': error, 'meshes': len(imported)}))
