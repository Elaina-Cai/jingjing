"""在运行中的 Blender 里搭四层模型：暗色板 + 发光字，渲染多角度图，存 .blend。"""
import bpy, os, math, json
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "output", "3d")
PNG = {"persona": os.path.join(OUT, "persona.png"), "attitude": os.path.join(OUT, "attitude.png"),
       "mood": os.path.join(OUT, "mood.png"), "window": os.path.join(OUT, "window.png")}
# (name, x, z, 宽, 高, 厚)
LAYERS = [("persona", 0.0, -2.2, 10.71, 10.0, 0.12),
          ("attitude", 0.0, 0.0, 4.55, 10.0, 0.12),
          ("mood", 0.0, 2.2, 4.59, 10.0, 0.12),
          ("window", 8.6, 0.0, 2.02, 10.0, 0.12)]

def clear():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)

def plate_mat():
    m = bpy.data.materials.new("plate")
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (0.035, 0.045, 0.06, 1)
    bsdf.inputs["Roughness"].default_value = 0.55
    bsdf.inputs["Metallic"].default_value = 0.25
    return m

def glow_mat(name, png):
    m = bpy.data.materials.new("glow_" + name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    mix = nt.nodes.new("ShaderNodeMixShader")
    trans = nt.nodes.new("ShaderNodeBsdfTransparent")
    emis = nt.nodes.new("ShaderNodeEmission")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(png, check_existing=True)
    tex.interpolation = "Linear"
    emis.inputs["Strength"].default_value = 1.35
    nt.links.new(tex.outputs["Color"], emis.inputs["Color"])
    nt.links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
    nt.links.new(trans.outputs["BSDF"], mix.inputs[1])
    nt.links.new(emis.outputs["Emission"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    m.use_backface_culling = False
    return m

def build():
    clear()
    pm = plate_mat()
    for name, x, z, w, h, t in LAYERS:
        coll = bpy.data.collections.new("层_" + name)
        bpy.context.scene.collection.children.link(coll)
        # 板
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, 0.0, z))
        box = bpy.context.active_object
        box.name = "plate_" + name
        box.dimensions = (w, t, h)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        box.data.materials.append(pm)
        for c in box.users_collection:
            c.objects.unlink(box)
        coll.objects.link(box)
        # 发光字面片（贴在 -Y 面外侧）
        bpy.ops.mesh.primitive_plane_add(size=1, location=(x, -t / 2 - 0.004, z), rotation=(math.radians(90), 0, 0))
        pl = bpy.context.active_object
        pl.name = "text_" + name
        pl.scale = (w, h, 1.0)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        pl.data.materials.append(glow_mat(name, PNG[name]))
        for c in pl.users_collection:
            c.objects.unlink(pl)
        coll.objects.link(pl)

def stage():
    sc = bpy.context.scene
    pass
    for _eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES"):
        try:
            sc.render.engine = _eng
            break
        except Exception:
            continue
    sc.render.resolution_x, sc.render.resolution_y = 1600, 1000
    sc.render.film_transparent = False
    w = sc.world or bpy.data.worlds.new("W")
    sc.world = w
    w.use_nodes = True
    bg = next(n for n in w.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs[0].default_value = (0.008, 0.010, 0.016, 1)
    bg.inputs[1].default_value = 1.0
    # 相机
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 50
    cam = bpy.data.objects.new("Cam", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    tgt = bpy.data.objects.new("CamTarget", None)
    tgt.location = (4.3, 0.0, 0.0)
    sc.collection.objects.link(tgt)
    c = cam.constraints.new("TRACK_TO")
    c.target = tgt
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"
    # 灯
    for nm, loc, energy, size in [("key", (10, -18, 14), 900, 12), ("fill", (-14, -10, 6), 350, 14), ("rim", (6, 16, 10), 600, 12)]:
        ld = bpy.data.lights.new(nm, "AREA")
        ld.energy = energy
        ld.size = size
        lo = bpy.data.objects.new(nm, ld)
        lo.location = loc
        sc.collection.objects.link(lo)
        cc = lo.constraints.new("TRACK_TO")
        cc.target = tgt
        cc.track_axis = "TRACK_NEGATIVE_Z"
        cc.up_axis = "UP_Y"
    return cam

def shots(cam):
    paths = []
    for nm, ang, dist, height in [("bl_iso", -38, 34, 13), ("bl_front", 0, 30, 6), ("bl_side", -88, 30, 9), ("bl_window", 8, 18, 5)]:
        a = math.radians(ang)
        cam.location = (4.3 + dist * math.sin(a), -dist * math.cos(a), height)
        bpy.context.view_layer.update()
        p = os.path.join(OUT, nm + ".png")
        bpy.context.scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
    return paths

build()
cam = stage()
paths = shots(cam)
blend = os.path.join(OUT, "yinyue_layers_blender.blend")
bpy.ops.wm.save_as_mainfile(filepath=blend)
print(json.dumps({"shots": [(os.path.basename(p), os.path.getsize(p)) for p in paths if os.path.exists(p)],
                  "blend": (blend, os.path.getsize(blend)), "objects": len(bpy.data.objects)}, ensure_ascii=False))
