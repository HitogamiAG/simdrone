"""Blender background verification; does not save changes to the source."""
from pathlib import Path
import hashlib
import json
import bpy

root = Path(__file__).resolve().parent
manifest = json.loads((root / 'manifest.json').read_text())
for name, expected in manifest['sha256'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
scene = bpy.context.scene
assert [s.name for s in bpy.data.scenes] == ['empty']
assert sorted(o.name for o in scene.objects) == ['ground_plane', 'sun']
assert scene.unit_settings.scale_length == 1
ground = scene.objects['ground_plane']
assert tuple(ground.scale) == (1, 1, 1)
source = sorted(tuple(ground.matrix_world @ v.co) for v in ground.data.vertices)
bpy.ops.import_scene.gltf(filepath=str(root / 'world.glb'))
imported = [o for o in bpy.context.selected_objects if o.type == 'MESH']
assert len(imported) == 1
actual = sorted(tuple(imported[0].matrix_world @ v.co) for v in imported[0].data.vertices)
assert len(source) == len(actual)
error = max(abs(a-b) for p, q in zip(source, actual) for a, b in zip(p, q))
assert error <= 0.02, (source, actual)
print(json.dumps({'status': 'ok', 'glb_roundtrip_max_error_m': error, 'vertices': actual}))
