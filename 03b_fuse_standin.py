#!/usr/bin/env python3
"""Step 3b (SYNTHETIC STAND-IN for CUDA-only COLMAP PatchMatchStereo):
Render depth maps from the KNOWN camera poses, add MVS-like noise, fuse
into a dense point cloud, and Poisson-reconstruct a mesh with pymeshlab.

This stands in ONLY for the dense MVS stage, which requires a CUDA GPU
(COLMAP's PatchMatchStereo is CUDA-only; this machine has no GPU).
Everything downstream (crop/repair/scale/export/slice) runs for real.

Output: colmap/dense/meshed_standin.ply  (true mm coordinates)
"""
import math, os
import numpy as np
from PIL import Image
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
DENSE = os.path.join(HERE, "colmap", "dense")
os.makedirs(DENSE, exist_ok=True)
rng = np.random.default_rng(3)

# ---- rebuild the textured scene (same construction as 01_render.py) ----
import importlib.util
spec = importlib.util.spec_from_file_location("_r", os.path.join(HERE, "01_render.py"))

# We can't import 01_render (it renders on import), so rebuild minimally here.
import sys
sys.path.insert(0, HERE)
# replicate texture + part building by exec'ing only the needed top part is messy;
# instead load the ground-truth STL (untextured is fine for DEPTH) and reuse
# the true camera poses. Depth does not need texture.
import pyrender

gt = trimesh.load(os.path.join(HERE, "part", "bracket.stl"), process=False)
ground = trimesh.creation.box((600, 600, 1.0))
ground.apply_translation((0, 0, -0.5))

scene = pyrender.Scene()
scene.add(pyrender.Mesh.from_trimesh(gt, smooth=False))
scene.add(pyrender.Mesh.from_trimesh(ground, smooth=False))

W, H = 640, 480
yfov = 0.42
fx = fy = (H / 2) / math.tan(yfov / 2)
cx, cy = W / 2, H / 2
K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]])
Kinv = np.linalg.inv(K)
cam = pyrender.PerspectiveCamera(yfov=yfov)
cam_node = scene.add(cam, pose=np.eye(4))

def look_pose(eye, target, up=np.array([0., 0., 1.])):
    z = eye - target; z /= np.linalg.norm(z)
    x = np.cross(up, z); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    p = np.eye(4); p[:3, 0] = x; p[:3, 1] = y; p[:3, 2] = z; p[:3, 3] = eye
    return p

# true poses (same math as 01_render.py)
target = np.array([0.0, 0.0, 15.0]); dist = 175.0
poses = []
for el_deg in (18, 40, 65):
    for az_deg in np.linspace(0, 360, 16, endpoint=False):
        el, az = math.radians(el_deg), math.radians(az_deg)
        eye = target + dist * np.array([math.cos(el) * math.cos(az),
                                        math.cos(el) * math.sin(az),
                                        math.sin(el)])
        poses.append(look_pose(eye, target))

renderer = pyrender.OffscreenRenderer(W, H)
UU, VV = np.meshgrid(np.arange(W), np.arange(H))
all_pts, all_nor = [], []
# use every 2nd view (24 views) to mimic sparser MVS coverage
for pi, pose in enumerate(poses[::2]):
    scene.set_pose(cam_node, pose)
    _, depth = renderer.render(scene)
    depth = depth[::-1, :]  # pyrender returns depth bottom-row-first; flip to match image (u,v)
    # add MVS-like noise to depth
    noisy = depth + rng.normal(0, 0.25, depth.shape)
    valid = (depth > 0) & np.isfinite(depth)
    # per-pixel view-space points from the CLEAN depth (normals), noisy for positions
    Xc = (UU - cx) * depth / fx
    Yc = (VV - cy) * depth / fy
    Zc = -depth
    # central-difference tangents -> normals (masked to valid 3x3 neighbourhoods)
    ok = (valid &
          np.roll(valid, 1, 0) & np.roll(valid, -1, 0) &
          np.roll(valid, 1, 1) & np.roll(valid, -1, 1))
    dXdu = (np.roll(Xc, -1, 1) - np.roll(Xc, 1, 1)) / 2.0
    dYdu = (np.roll(Yc, -1, 1) - np.roll(Yc, 1, 1)) / 2.0
    dZdu = (np.roll(Zc, -1, 1) - np.roll(Zc, 1, 1)) / 2.0
    dXdv = (np.roll(Xc, -1, 0) - np.roll(Xc, 1, 0)) / 2.0
    dYdv = (np.roll(Yc, -1, 0) - np.roll(Yc, 1, 0)) / 2.0
    dZdv = (np.roll(Zc, -1, 0) - np.roll(Zc, 1, 0)) / 2.0
    tu = np.stack([dXdu, dYdu, dZdu], -1)
    tv = np.stack([dXdv, dYdv, dZdv], -1)
    nv = np.cross(tu, tv)
    nl = np.linalg.norm(nv, axis=-1, keepdims=True)
    nv = nv / np.maximum(nl, 1e-12)
    # visible surfaces face the camera: view-space normal z must be > 0
    flip = nv[..., 2] < 0
    nv[flip] *= -1
    n_world = nv @ pose[:3, :3].T  # row-vector rotation: n_w = R_wc @ n_v
    vs, us = np.nonzero(ok)
    # subsample: keep every 3rd pixel to bound point count
    vs, us = vs[::3], us[::3]
    d = noisy[vs, us]
    # back-project: assume z-depth convention, validate via known geometry
    px = np.stack([us, vs, np.ones_like(us)], axis=1)          # (N,3)
    p_cam = (Kinv @ px.T).T * d[:, None]                       # (N,3)
    p_cam[:, 2] *= -1   # camera looks down -Z: view-space z is negative
    R_wc = pose[:3, :3]; c = pose[:3, 3]
    p_world = p_cam @ R_wc.T + c
    # drop 5% at random (outlier-ish dropout)
    keep = rng.random(len(p_world)) > 0.05
    all_pts.append(p_world[keep])
    all_nor.append(n_world[vs[keep], us[keep]])
renderer.delete()

P = np.vstack(all_pts)
N = np.vstack(all_nor)
print("fused raw points: %d" % len(P))
# voxel downsample 1.0 mm, averaging positions and normals per voxel
keys = np.floor(P / 1.0).astype(np.int64)
ukeys, inv = np.unique(keys, axis=0, return_inverse=True)
Pv = np.zeros((len(ukeys), 3)); Nv = np.zeros((len(ukeys), 3))
np.add.at(Pv, inv, P); np.add.at(Nv, inv, N)
cnt = np.bincount(inv)
P = Pv / cnt[:, None]
N = Nv / np.maximum(np.linalg.norm(Nv, axis=1, keepdims=True), 1e-12)
print("after 1mm voxel downsample: %d" % len(P))
# keep points near the part only (drop the table/ground: a real scan would
# mask the object first; this keeps the stand-in honest about part geometry)
m = (np.abs(P[:, 0]) < 45) & (np.abs(P[:, 1]) < 40) & (P[:, 2] > -2) & (P[:, 2] < 50)
P = P[m]; N = N[m]
print("after part-ROI crop: %d" % len(P))

def write_ply_xyz_nxnynz(path, P, N):
    with open(path, "w") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write("element vertex %d\n" % len(P))
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("property float nx\nproperty float ny\nproperty float nz\n")
        f.write("end_header\n")
        for p, n in zip(P, N):
            f.write("%.4f %.4f %.4f %.5f %.5f %.5f\n" % (p[0], p[1], p[2], n[0], n[1], n[2]))

fused_path = os.path.join(DENSE, "fused_standin.ply")
write_ply_xyz_nxnynz(fused_path, P, N)
print("wrote", fused_path)

# ---- Poisson reconstruction with pymeshlab (normals already oriented) ----
import pymeshlab
ms = pymeshlab.MeshSet()
ms.load_new_mesh(fused_path)
print("screened poisson...")
ms.generate_surface_reconstruction_screened_poisson(depth=9, samplespernode=1.0)
out_path = os.path.join(DENSE, "meshed_standin.ply")
ms.save_current_mesh(out_path)
n_faces = ms.current_mesh().face_number()
print("wrote %s (%d faces)" % (out_path, n_faces))
