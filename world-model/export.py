"""Run inside Blender: create the empty package or re-export its saved source."""
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT.parent / "gazebo-service/app/worlds/empty.sdf"


def export_package():
    scene = bpy.context.scene
    if scene.name != "empty":
        scene = bpy.data.scenes.new("empty")
        bpy.context.window.scene = scene
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1.0
        scene.unit_settings.length_unit = "METERS"
        bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, 0))
        ground = bpy.context.object
        ground.name = "ground_plane"
        material = bpy.data.materials.new("ground")
        material.diffuse_color = (0.5, 0.5, 0.5, 1)
        material.use_nodes = True
        bsdf = material.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = (0.5, 0.5, 0.5, 1)
        bsdf.inputs["Roughness"].default_value = 1.0
        ground.data.materials.append(material)
        light = bpy.data.lights.new("sun", "SUN")
        light.energy = 0.8
        sun = bpy.data.objects.new("sun", light)
        scene.collection.objects.link(sun)
        sun.location = (0, 0, 10)
        sun.rotation_euler = Vector((-0.5, 0.1, -0.9)).to_track_quat('-Z', 'Y').to_euler()
        world = bpy.data.worlds.new("empty_sky")
        world.color = (0.7, 0.8, 1.0)
        scene.world = world
        scene["sdf_template"] = TEMPLATE.read_text()
        scene["package_id"] = "simdrone-empty"
        scene["package_version"] = "1.0.0"
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces.active.region_3d.view_distance = 240
                    area.spaces.active.clip_end = 2000
    ground = scene.objects["ground_plane"]
    bpy.context.view_layer.objects.active = ground
    for obj in scene.objects:
        obj.select_set(obj == ground)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.context.view_layer.update()
    vertices = [tuple(ground.matrix_world @ v.co) for v in ground.data.vertices]
    xs, ys, zs = zip(*vertices)
    assert len(vertices) == 4 and len(ground.data.polygons) == 1
    assert min(xs) == -max(xs) and min(ys) == -max(ys)
    assert all(abs(z) < 1e-6 for z in zs), 'empty ground must remain at Z=0'
    assert ground.data.polygons[0].normal.z > 0.99
    width, depth = max(xs) - min(xs), max(ys) - min(ys)
    tree = ET.fromstring(scene["sdf_template"])
    for plane in tree.findall('.//model[@name="ground_plane"]//plane'):
        plane.find('size').text = f'{width:g} {depth:g}'
    ET.indent(tree, space='  ')
    ET.ElementTree(tree).write(ROOT / 'world.sdf', encoding='utf-8', xml_declaration=True)
    bpy.ops.export_scene.gltf(filepath=str(ROOT / 'world.glb'), export_format='GLB',
                              use_selection=True, use_active_scene=True, export_yup=True, export_cameras=False,
                              export_extras=False)
    # Write just the package scene and its dependencies, preserving other open scenes.
    if len(bpy.data.scenes) == 1:
        bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / 'empty.blend'), copy=True)
    else:
        bpy.data.libraries.write(str(ROOT / 'empty.blend'), {scene}, fake_user=True)
    points = [(0, 0, 0), (max(xs), 0, 0), (0, max(ys), 0), (min(xs), min(ys), 0)]
    manifest = {
        'package_id': scene['package_id'], 'version': scene['package_version'],
        'world_name': 'empty', 'coordinate_system': 'gazebo_xyz_z_up', 'units': 'meters',
        'model': 'world.glb', 'source': 'empty.blend', 'sdf': 'world.sdf',
        'bounds': {'min': [min(xs), min(ys), 0], 'max': [max(xs), max(ys), 0]},
        'gazebo_to_glb': [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]],
        'glb_to_gazebo': [[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
        'control_points': [{'name': name, 'gazebo': list(p), 'glb': [p[0], p[2], -p[1]]}
                           for name, p in zip(['origin', 'positive_x', 'positive_y', 'corner'], points)],
        'collision': 'Gazebo plane (infinite); visual footprint is finite',
        'blender_version': bpy.app.version_string,
        'sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                   for name in ['empty.blend', 'world.glb', 'world.sdf']},
    }
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return {'scene': scene.name, 'dimensions_m': [width, depth],
            'files': ['empty.blend', 'world.glb', 'world.sdf', 'manifest.json']}


if __name__ == '__main__':
    result = export_package()
