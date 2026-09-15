#!/usr/bin/env python3
"""Step 1: Build a synthetic textured test part (L-bracket, 50mm reference)
and render N orbit views with pyrender (headless via Xvfb).

Outputs:
  part/bracket.stl        - ground-truth mesh (for comparison only)
  part/reference.json     - known dimensions
  images/img_XXXX.jpg     - N phone-like photos
  part/cameras_known.txt  - true intrinsics/extrinsics (reference only)
"""
import json, math, os
import numpy as np
from PIL import Image, ImageDraw
import trimesh
from trimesh.visual import TextureVisuals
from trimesh.visual.material import SimpleMaterial
import pyrender

OUT = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(OUT, "images")
PART_DIR = os.path.join(OUT, "part")
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(PART_DIR, exist_ok=True)

rng = np.random.default_rng(7)

# ---------------------------------------------------------------- texture
def make_part_texture(size=1024):
    """High-frequency procedural texture: great SIFT food."""
    img = np.full((size, size, 3), 128, np.uint8)
    # soft blobs, random gray levels
    for _ in range(900):
        x, y = rng.integers(0, size, 2)
        r = int(rng.integers(8, 60))
        v = int(rng.integers(15, 240))
        yy, xx = np.ogrid[:size, :size]
        m = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
        img[m] = (img[m].astype(int) * 0.25 + v * 0.75).astype(np.uint8)
    # strong fine noise
    img = np.clip(img.astype(int) + rng.integers(-45, 45, img.shape), 0, 255).astype(np.uint8)
    # dark speckles
    sp = rng.random((size, size)) < 0.02
    img[sp] = (img[sp].astype(int) * 0.3).astype(np.uint8)
    # faint grid every 64 px (helps orientation disambiguation)
    g = Image.fromarray(img); d = ImageDraw.Draw(g)
    for k in range(0, size, 64):
        d.line([(k, 0), (k, size)], fill=(70, 70, 70), width=2)
        d.line([(0, k), (size, k)], fill=(70, 70, 70), width=2)
    return g

def make_ground_texture(size=512):
    img = np.full((size, size, 3), 105, np.uint8)
    for _ in range(150):
        x, y = rng.integers(0, size, 2); r = int(rng.integers(4, 26))
        v = int(rng.integers(60, 150))
        yy, xx = np.ogrid[:size, :size]
        img[(xx - x) ** 2 + (yy - y) ** 2 <= r * r] = v
    img = np.clip(img.astype(int) + rng.integers(-18, 18, img.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(img)

TEX_SCALE = 23.0  # mm per texture tile (must NOT evenly divide part dims)

def planar_uv(mesh):
    """Per-face planar UVs by dominant normal axis (unwelded)."""
    v = mesh.vertices[mesh.faces.reshape(-1)]          # (3F, 3)
    n = trimesh.triangles.normals(mesh.triangles)[0]  # (F, 3)
    n = np.repeat(n, 3, axis=0)
    ax = np.argmax(np.abs(n), axis=1)
    uv = np.zeros((len(v), 2))
    for a, (u, w) in {0: (1, 2), 1: (0, 2), 2: (0, 1)}.items():
        m = ax == a
        uv[m, 0] = v[m, u] / TEX_SCALE
        uv[m, 1] = v[m, w] / TEX_SCALE
    faces = np.arange(len(v)).reshape(-1, 3)
    return v, faces, uv % 1.0

def textured(mesh, image):
    v, faces, uv = planar_uv(mesh)
    # NOTE: pyrender's from_trimesh only transfers textures from SimpleMaterial,
    # and needs PyOpenGL>=3.1.10 (3.1.0 crashes in glGenTextures w/ numpy 2.x)
    mat = SimpleMaterial(image=image)
    return trimesh.Trimesh(vertices=v, faces=faces,
                           visual=TextureVisuals(uv=uv, material=mat), process=False)

# ------------------------------------------------------------------ part
# L-bracket: base 50 x 36 x 8, upright 50 x 8 x 30, boss cylinder r6 h10.
# Known reference: overall X extent = 50.0 mm.
base = trimesh.creation.box((50.0, 36.0, 8.0))
base.apply_translation((0, 0, 4.0 + 0.3))
upright = trimesh.creation.box((50.0, 8.0, 30.0))
upright.apply_translation((0, -14.0, 8.0 + 15.0 + 0.3))
boss = trimesh.creation.cylinder(radius=6.0, height=10.0, sections=24)
boss.apply_translation((13.0, 7.0, 8.0 + 5.0 + 0.3))
rib = trimesh.creation.box((6.0, 12.0, 14.0))
rib.apply_translation((-15.0, -8.0, 8.0 + 7.0 + 0.3))

tex = make_part_texture()
meshes = [textured(m, tex) for m in (base, upright, boss, rib)]

# ground truth mesh for comparison (untextured concat, mm units)
gt = trimesh.util.concatenate([base, upright, boss, rib])
gt.export(os.path.join(PART_DIR, "bracket.stl"))
ref = {"x_extent_mm": 50.0, "bbox_min": gt.bounds[0].tolist(),
       "bbox_max": gt.bounds[1].tolist(), "units": "mm"}
json.dump(ref, open(os.path.join(PART_DIR, "reference.json"), "w"), indent=2)
print("ground truth bbox:", gt.bounds, "volume: %.1f mm^3" % gt.volume)

# ground plane
gtex = make_ground_texture()
gv = np.array([[-300, -300, 0], [300, -300, 0], [300, 300, 0], [-300, 300, 0.0]])
gf = np.array([[0, 1, 2], [0, 2, 3]])
guv = (gv[:, :2] / 47.0) % 1.0
gmat = SimpleMaterial(image=gtex)
ground = trimesh.Trimesh(vertices=gv, faces=gf,
                         visual=TextureVisuals(uv=guv, material=gmat), process=False)

# ---------------------------------------------------------------- cameras
W, H = 640, 480
yfov = 0.42                      # ~24 deg
f_px = (H / 2) / math.tan(yfov / 2)
print("focal px ~ %.0f" % f_px)
target = np.array([0.0, 0.0, 15.0])
dist = 175.0
views = []
for el_deg in (18, 40, 65):
    for az_deg in np.linspace(0, 360, 16, endpoint=False):
        views.append((math.radians(el_deg), math.radians(az_deg)))
print("rendering %d views" % len(views))

scene = pyrender.Scene(bg_color=[38, 38, 44, 255],
                       ambient_light=[0.55, 0.55, 0.55])
for m in meshes:
    scene.add(pyrender.Mesh.from_trimesh(m, smooth=False))
scene.add(pyrender.Mesh.from_trimesh(ground, smooth=False))
cam = pyrender.PerspectiveCamera(yfov=yfov)

def look_pose(eye, target, up=np.array([0., 0., 1.])):
    z = eye - target; z /= np.linalg.norm(z)
    x = np.cross(up, z); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    p = np.eye(4); p[:3, 0] = x; p[:3, 1] = y; p[:3, 2] = z; p[:3, 3] = eye
    return p

cam_node = scene.add(cam, pose=np.eye(4))
key = scene.add(pyrender.DirectionalLight(color=[1, 1, 1], intensity=2.4),
                pose=look_pose(np.array([140., -90., 190.]), target))
fill = scene.add(pyrender.DirectionalLight(color=[1.0, 1.0, 1.0], intensity=0.9),
                 pose=look_pose(np.array([-120., 110., 60.]), target))

renderer = pyrender.OffscreenRenderer(W, H)
cam_lines = []
for i, (el, az) in enumerate(views):
    eye = target + dist * np.array([math.cos(el) * math.cos(az),
                                    math.cos(el) * math.sin(az),
                                    math.sin(el)])
    scene.set_pose(cam_node, look_pose(eye, target))
    img, _ = renderer.render(scene)
    Image.fromarray(img).save(os.path.join(IMG_DIR, "img_%04d.jpg" % i), quality=92)
    # store true extrinsics (world->cam) for reference
    pose = look_pose(eye, target)
    R, t = pose[:3, :3].T, -pose[:3, :3].T @ pose[:3, 3]
    q = trimesh.transformations.quaternion_from_matrix(
        np.vstack([np.hstack([R, t[:, None]]), [0, 0, 0, 1]]))
    cam_lines.append((i, q, t))
renderer.delete()

with open(os.path.join(PART_DIR, "cameras_known.txt"), "w") as f:
    f.write("# PINHOLE %d %d fx fy cx cy ; per-image: id qw qx qy qz tx ty tz\n" % (W, H))
    f.write("INTRINSICS %.2f %.2f %.1f %.1f\n" % (f_px, f_px, W / 2, H / 2))
    for i, q, t in cam_lines:
        f.write("IMG %04d %.6f %.6f %.6f %.6f %.3f %.3f %.3f\n" %
                (i, q[0], q[1], q[2], q[3], t[0], t[1], t[2]))
print("wrote %d images to %s" % (len(views), IMG_DIR))
