"""Генерация пакета: blender --background --factory-startup --python export.py."""
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET

import bpy

ROOT = Path(__file__).resolve().parent
SIZE = (2.5, 2.5, 0.3)
MARKER_SIZE = 0.5
# OpenCV 4.12.0 generateImageMarker(DICT_4X4_50, 0, 6, borderBits=1).
# Строки сверху вниз: +Y -> -Y, столбцы слева направо: -X -> +X.
BITS = (
    (0, 0, 0, 0, 0, 0),
    (0, 1, 0, 1, 1, 0),
    (0, 0, 1, 0, 1, 0),
    (0, 0, 0, 1, 1, 0),
    (0, 0, 0, 1, 0, 0),
    (0, 0, 0, 0, 0, 0),
)


def element(parent, tag, text=None, **attributes):
    node = ET.SubElement(parent, tag, attributes)
    if text is not None:
        node.text = str(text)
    return node


def write_xml(root, filename):
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(ROOT / filename, encoding="utf-8", xml_declaration=True)


def export():
    # Новая сцена: ранее открытые сцены/объекты не изменяются.
    scene = bpy.data.scenes.new("drone_pad")
    bpy.context.window.scene = scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.unit_settings.length_unit = "METERS"
    scene["aruco_dictionary"] = "DICT_4X4_50"
    scene["aruco_id"] = 0
    materials = {}
    for name, value in (("platform_gray", 0.35), ("marker_white", 1.0), ("marker_black", 0.0)):
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = (value, value, value, 1)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = mat.diffuse_color
        bsdf.inputs["Roughness"].default_value = 1
        materials[name] = mat

    sdf = ET.Element("sdf", version="1.9")
    model = element(sdf, "model", name="drone_pad")
    element(model, "static", "true")
    link = element(model, "link", name="platform")
    collision = element(link, "collision", name="platform_collision")
    element(collision, "pose", "0 0 0.15 0 0 0")
    element(element(element(collision, "geometry"), "box"), "size", "2.5 2.5 0.3")

    def visual(name, xy_size, position, material_name, box=False):
        value = materials[material_name].diffuse_color[0]
        if box:
            bpy.ops.mesh.primitive_cube_add(size=1, location=position)
            obj = bpy.context.object
            obj.dimensions = SIZE
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        else:
            bpy.ops.mesh.primitive_plane_add(size=xy_size, location=position)
            obj = bpy.context.object
        obj.name = name
        obj.data.materials.append(materials[material_name])
        node = element(link, "visual", name=name)
        element(node, "pose", " ".join(str(v) for v in (*position, 0, 0, 0)))
        geometry = element(node, "geometry")
        if box:
            element(element(geometry, "box"), "size", "2.5 2.5 0.3")
        else:
            plane = element(geometry, "plane")
            element(plane, "normal", "0 0 1")
            element(plane, "size", f"{xy_size:.12g} {xy_size:.12g}")
            element(node, "cast_shadows", "false")
        material = element(node, "material")
        rgba = f"{value:.6g} {value:.6g} {value:.6g} 1"
        element(material, "ambient", rgba)
        element(material, "diffuse", rgba)
        element(material, "specular", "0 0 0 1")

    visual("platform_body", 2.5, (0, 0, 0.15), "platform_gray", box=True)
    cell = MARKER_SIZE / 6
    visual("marker_quiet_zone", cell * 8, (0, 0, 0.3001), "marker_white")
    for row, bits in enumerate(BITS):
        for col, bit in enumerate(bits):
            if bit == 0:
                visual(f"marker_r{row}_c{col}", cell,
                       ((col - 2.5) * cell, (2.5 - row) * cell, 0.3002), "marker_black")
    bpy.context.view_layer.update()
    write_xml(sdf, "model.sdf")
    config = ET.Element("model")
    element(config, "name", "drone_pad")
    element(config, "version", "1.0.0")
    element(config, "sdf", "model.sdf", version="1.9")
    element(config, "description", "Static 2.5 x 2.5 x 0.3 m drone pad; ArUco DICT_4X4_50 ID 0, 0.5 m.")
    write_xml(config, "model.config")

    # PNG для печати/детектора; Gazebo использует ту же сетку как геометрию.
    side = 800
    image = bpy.data.images.new("aruco_4x4_50_id0", width=side, height=side, alpha=False)
    pixels = []
    for py in range(side):
        row = 7 - py // 100
        for px in range(side):
            col = px // 100
            bit = BITS[row - 1][col - 1] if 1 <= row <= 6 and 1 <= col <= 6 else 1
            pixels.extend((bit, bit, bit, 1))
    image.pixels.foreach_set(pixels)
    image.filepath_raw = str(ROOT / "aruco_4x4_50_id0.png")
    image.file_format = "PNG"
    image.save()
    image.pack()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=str(ROOT / "drone_pad.glb"), export_format="GLB",
                              use_selection=True, export_yup=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / "drone_pad.blend"), copy=True)
    metadata = {
        "model": "drone_pad", "version": "1.0.0", "units": "meters",
        "coordinates": "Gazebo XYZ, Z up", "origin": "center of bottom face",
        "size": list(SIZE), "collision_top_z": 0.3,
        "marker": {"dictionary": "DICT_4X4_50", "id": 0, "border_bits": 1,
                   "size": [0.5, 0.5], "quiet_zone_width": cell,
                   "center": [0, 0, 0.3002], "top_edge_direction": "+Y", "bits": BITS},
        "glb_to_gazebo": [[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
        "blender_version": bpy.app.version_string,
        "sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                   for p in ("model.sdf", "model.config", "drone_pad.blend", "drone_pad.glb", "aruco_4x4_50_id0.png")},
    }
    (ROOT / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


if __name__ == "__main__":
    if bpy.app.background:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    export()
