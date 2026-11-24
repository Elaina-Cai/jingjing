import json, struct
from pathlib import Path
from PIL import Image

OUT = Path("output/3d"); OUT.mkdir(parents=True, exist_ok=True)
SRC = Path(".test_tmp/3d")

LAYERS = [
    # name,      z,     height,  rotation(Y deg)
    ("persona",   0.0,  -2.2,  10.0,  0.0),
    ("attitude",  0.0,   0.0,  10.0,  0.0),
    ("mood",      0.0,   2.2,  10.0,  0.0),
    ("window",    8.6,   0.0,  10.0,  0.0),
]

def png_size(p):
    with Image.open(p) as im:
        return im.size

def box_mesh(w, h, t):
    x, y, z = w / 2.0, h / 2.0, t / 2.0
    pos, nrm, uv, idx = [], [], [], []
    # 前面 (z=+t/2)
    face = [(-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
    uvs = [(0, 1), (1, 1), (1, 0), (0, 0)]
    base = 0
    for p, q in zip(face, uvs):
        pos.append(p); nrm.append((0, 0, 1)); uv.append(q)
    idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    # 背面
    base = len(pos) // 1
    face = [(x, -y, -z), (-x, -y, -z), (-x, y, -z), (x, y, -z)]
    for p, q in zip(face, uvs):
        pos.append(p); nrm.append((0, 0, -1)); uv.append(q)
    idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    # 四周（UV 固定在边缘色，视觉上是实心边）
    sides = [
        ((x, -y, z), (x, -y, -z), (x, y, -z), (x, y, z), (1, 0, 0)),
        ((-x, -y, -z), (-x, -y, z), (-x, y, z), (-x, y, -z), (-1, 0, 0)),
        ((-x, y, z), (x, y, z), (x, y, -z), (-x, y, -z), (0, 1, 0)),
        ((-x, -y, -z), (x, -y, -z), (x, -y, z), (-x, -y, z), (0, -1, 0)),
    ]
    for a, b, c, d, n in sides:
        base = len(pos)
        for p in (a, b, c, d):
            pos.append(p); nrm.append(n); uv.append((0.5, 0.5))
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    return pos, nrm, uv, idx

def pad4(b):
    return b + b"\x00" * ((4 - len(b) % 4) % 4)

blob = bytearray(); views = []; accessors = []
def add_view(data, target=None):
    while len(blob) % 4: blob.append(0)
    off = len(blob); blob.extend(data)
    v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
    if target: v["target"] = target
    views.append(v)
    return len(views) - 1

def add_acc(view, comp, count, typ, mn=None, mx=None):
    a = {"bufferView": view, "componentType": comp, "count": count, "type": typ}
    if mn is not None: a["min"] = mn
    if mx is not None: a["max"] = mx
    accessors.append(a)
    return len(accessors) - 1

meshes, materials, textures, images, samplers, nodes = [], [], [], [], [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}], []

for i, (name, x, z, h, roty) in enumerate(LAYERS):
    png = SRC / ("canvas_%s.png" % name)
    w_px, h_px = png_size(png)
    w = h * w_px / h_px
    t = 0.10
    pos, nrm, uv, idx = box_mesh(w, h, t)
    pv = add_view(struct.pack("<%df" % (len(pos) * 3), *[c for p in pos for c in p]), 34962)
    nv = add_view(struct.pack("<%df" % (len(nrm) * 3), *[c for p in nrm for c in p]), 34962)
    uv_flat = [c for q in uv for c in q]
    uvv = add_view(struct.pack("<%df" % len(uv_flat), *uv_flat), 34962)
    iv = add_view(struct.pack("<%dH" % len(idx), *idx), 34963)
    xs = [p[0] for p in pos]; ys = [p[1] for p in pos]; zs = [p[2] for p in pos]
    a_pos = add_acc(pv, 5126, len(pos), "VEC3", [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)])
    a_nrm = add_acc(nv, 5126, len(nrm), "VEC3")
    a_uv = add_acc(uvv, 5126, len(uv), "VEC2")
    a_idx = add_acc(iv, 5123, len(idx), "SCALAR")
    data = png.read_bytes()
    iv2 = add_view(data)
    images.append({"bufferView": iv2, "mimeType": "image/png", "name": name})
    textures.append({"sampler": 0, "source": len(images) - 1})
    materials.append({
        "name": "mat_" + name,
        "pbrMetallicRoughness": {"baseColorTexture": {"index": len(textures) - 1}, "metallicFactor": 0.0, "roughnessFactor": 0.85},
        "alphaMode": "BLEND", "doubleSided": True,
        "emissiveFactor": [0.18, 0.18, 0.18],
    })
    meshes.append({"name": "mesh_" + name, "primitives": [{
        "attributes": {"POSITION": a_pos, "NORMAL": a_nrm, "TEXCOORD_0": a_uv},
        "indices": a_idx, "material": i}]})
    node = {"name": name, "mesh": i, "translation": [x, 0.0, z]}
    nodes.append(node)

gltf = {
    "asset": {"version": "2.0", "generator": "yinyue-3d-layers (handwritten GLB)"},
    "scene": 0,
    "scenes": [{"name": "window_and_state_layers", "nodes": list(range(len(nodes)))}],
    "nodes": nodes, "meshes": meshes, "materials": materials,
    "textures": textures, "images": images, "samplers": samplers,
    "accessors": accessors, "bufferViews": views,
    "buffers": [{"byteLength": len(blob)}],
}
js = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
while len(js) % 4: js += b" "
bin_chunk = bytes(blob)
total = 12 + 8 + len(js) + 8 + len(bin_chunk)
glb = bytearray()
glb += struct.pack("<III", 0x46546C67, 2, total)
glb += struct.pack("<II", len(js), 0x4E4F534A) + js
glb += struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk
path = OUT / "yinyue_layers.glb"
path.write_bytes(bytes(glb))
print("wrote", path, len(glb), "bytes; nodes", len(nodes), "images", len(images), "accessors", len(accessors))
for i, (name, x, z, h, roty) in enumerate(LAYERS):
    w_px, h_px = png_size(SRC / ("canvas_%s.png" % name))
    print("  %-9s px=%dx%d  world=%.2f x %.2f  x=%.1f z=%.1f" % (name, w_px, h_px, h * w_px / h_px, h, x, z))