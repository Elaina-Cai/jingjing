"""转台渲染：绕场景一圈出 16 帧，供 GIF / 拼图使用。"""
import bpy, os, math, json

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "output", "3d")
FR = os.path.join(OUT, "turntable")
os.makedirs(FR, exist_ok=True)

sc = bpy.context.scene
sc.render.resolution_x, sc.render.resolution_y = 1100, 700
sc.render.image_settings.file_format = "PNG"
try:
    sc.render.image_settings.color_mode = "RGB"
except Exception:
    pass

cam = bpy.data.objects["Cam"]
N = 16
done = []
for i in range(N):
    ang = -180.0 + 360.0 * i / N
    a = math.radians(ang)
    dist, height = 32.0, 11.0
    cam.location = (4.3 + dist * math.sin(a), -dist * math.cos(a), height)
    bpy.context.view_layer.update()
    p = os.path.join(FR, "f_%02d.png" % i)
    sc.render.filepath = p
    bpy.ops.render.render(write_still=True)
    done.append(os.path.getsize(p))
print(json.dumps({"frames": len(done), "bytes": sum(done)}))
