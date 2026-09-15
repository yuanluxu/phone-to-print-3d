#!/usr/bin/env python3
"""Step 3: mesh post-processing.

1. Load COLMAP poisson mesh (arbitrary similarity frame).
2. Align to true mm coordinates via Umeyama fit of COLMAP camera centers
   to the known (synthetic) camera centers. In the real workflow this step
   is replaced by scaling with a physical ruler/reference dimension.
3. Crop to the part bounding box (+margin), drop the ground plane.
4. Keep largest connected component, repair to watertight (pymeshfix),
   fill holes, verify manifold/watertight with trimesh.
5. Fine-tune uniform scale from the known 50.0 mm X-extent reference.
6. Export binary STL and 3MF.

Outputs: out/part_raw.ply, out/part_clean.stl, out/part_clean.3mf,
         out/report.json
"""
import json, os, struct, sys
import numpy as np
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
report = {}
STANDIN = "--standin" in sys.argv
if STANDIN:
    report["dense_source"] = ("SYNTHETIC STAND-IN: depth maps rendered from known "
                              "poses + noise, fused + Poisson (pymeshlab). "
                              "Stands in for CUDA-only COLMAP PatchMatchStereo.")
else:
    report["dense_source"] = "COLMAP poisson_mesher on PatchMatchStereo dense cloud"

# ------------------------------------------------- load COLMAP cameras
# Robust route: convert binary model to TXT with COLMAP itself, parse images.txt
import subprocess
txt_dir = os.path.join(HERE, "colmap/sparse/0/txt")
if not os.path.exists(os.path.join(txt_dir, "images.txt")):
    subprocess.run([os.path.join(HERE, "colmap-env/bin/colmap"), "model_converter",
                    "--input_path", os.path.join(HERE, "colmap/sparse/0"),
                    "--output_path", txt_dir, "--output_type", "TXT"],
                   check=True, capture_output=True)

def read_centers_txt(path):
    cams = {}
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            if " " in line and line.split()[0].replace(".", "", 1).isdigit() is False:
                continue
            p = line.split()
            # data line: IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME
            if len(p) == 10 and p[9].endswith(".jpg"):
                q = np.array([float(p[1]), float(p[2]), float(p[3]), float(p[4])])
                t = np.array([float(p[5]), float(p[6]), float(p[7])])
                R = trimesh.transformations.quaternion_matrix(q)[:3, :3]
                cams[p[9]] = -R.T @ t
    return cams

colmap_centers = read_centers_txt(os.path.join(txt_dir, "images.txt"))
print("COLMAP cameras parsed: %d" % len(colmap_centers))
assert len(colmap_centers) >= 10

# ------------------------------------------------- load known cameras
true_centers = {}
with open(os.path.join(HERE, "part/cameras_known.txt")) as f:
    for line in f:
        if line.startswith("IMG"):
            p = line.split()
            idx = int(p[1])
            q = np.array([float(p[2]), float(p[3]), float(p[4]), float(p[5])])
            t = np.array([float(p[6]), float(p[7]), float(p[8])])
            R = trimesh.transformations.quaternion_matrix(q)[:3, :3]
            true_centers["img_%04d.jpg" % idx] = -R.T @ t

names = sorted(set(colmap_centers) & set(true_centers))
print("cameras in common: %d" % len(names))
assert len(names) >= 10, "too few common cameras for alignment"
A = np.array([colmap_centers[n] for n in names])   # source
B = np.array([true_centers[n] for n in names])     # target (mm)

# ------------------------------------------------- Umeyama similarity
# (skipped in stand-in mode: mesh is already in the true mm frame)
def umeyama(A, B):
    muA, muB = A.mean(0), B.mean(0)
    AA, BB = A - muA, B - muB
    H = AA.T @ BB
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    D = np.diag([1, 1, d])
    R = Vt.T @ D @ U.T
    varA = (AA ** 2).sum() / len(A)
    s = (S * np.diag(D)).sum() / (len(A) * varA)
    t = muB - s * R @ muA
    return s, R, t

if not STANDIN:
    s, R, t = umeyama(A, B)
    resid = np.linalg.norm((s * (A @ R.T) + t) - B, axis=1)
    print("similarity: scale=%.4f  median residual=%.3f mm  max=%.3f mm"
          % (s, np.median(resid), resid.max()))
    report["align_scale"] = float(s)
    report["align_median_residual_mm"] = float(np.median(resid))
    report["align_max_residual_mm"] = float(resid.max())
    assert np.median(resid) < 2.0, "alignment failed (mirror or bad reconstruction?)"

    T = np.eye(4)
    T[:3, :3] = s * R
    T[:3, 3] = t
else:
    T = np.eye(4)

# ------------------------------------------------- load + transform mesh
if STANDIN:
    mesh_path = os.path.join(HERE, "colmap/dense/meshed_standin.ply")
    assert os.path.exists(mesh_path), "missing " + mesh_path
    mesh = trimesh.load(mesh_path, process=True)
    print("stand-in poisson mesh (already mm): %d verts, %d faces"
          % (len(mesh.vertices), len(mesh.faces)))
    report["align_note"] = "skipped (stand-in mesh already in true mm frame)"
else:
    mesh_path = os.path.join(HERE, "colmap/dense/meshed.ply")
    assert os.path.exists(mesh_path), "missing " + mesh_path
    mesh = trimesh.load(mesh_path, process=True)
    print("poisson mesh: %d verts, %d faces" % (len(mesh.vertices), len(mesh.faces)))
    mesh.apply_transform(T)
mesh.export(os.path.join(OUT, "part_raw.ply"))
report["poisson_faces"] = int(len(mesh.faces))

# ------------------------------------------------- crop to part bbox
ref = json.load(open(os.path.join(HERE, "part/reference.json")))
bmin = np.array(ref["bbox_min"]) - 4.0
bmax = np.array(ref["bbox_max"]) + 4.0
mask = np.all(mesh.vertices >= bmin, axis=1) & np.all(mesh.vertices <= bmax, axis=1)
mesh.update_faces(np.all(mask[mesh.faces], axis=1))
mesh.remove_unreferenced_vertices()
print("after crop: %d faces" % len(mesh.faces))
report["cropped_faces"] = int(len(mesh.faces))

# largest connected component
comps = mesh.split(only_watertight=False)
comps.sort(key=lambda m: len(m.faces), reverse=True)
mesh = comps[0]
print("largest component: %d faces (of %d)" % (len(mesh.faces), len(comps)))
report["components"] = len(comps)
report["largest_component_faces"] = int(len(mesh.faces))

# ------------------------------------------------- repair to watertight
from pymeshfix import MeshFix
mf = MeshFix(mesh.vertices, mesh.faces)
mf.repair()
mesh = trimesh.Trimesh(vertices=mf.points, faces=mf.faces, process=True)
print("after MeshFix repair: watertight=%s faces=%d"
      % (mesh.is_watertight, len(mesh.faces)))
if not mesh.is_watertight:
    mesh.fill_holes()
    mesh.process(validate=True)
    print("after fill_holes: watertight=%s" % mesh.is_watertight)
if mesh.is_watertight and mesh.volume < 0:
    mesh.invert()  # fix inward winding so volume/3MF/slicer see a solid
    print("inverted winding (volume was negative)")
report["watertight"] = bool(mesh.is_watertight)
report["is_volume"] = bool(mesh.is_volume)

# ------------------------------------------------- scale fine-tune on 50mm reference
x_extent = mesh.bounds[1][0] - mesh.bounds[0][0]
corr = ref["x_extent_mm"] / x_extent
mesh.apply_scale(corr)
print("x extent before corr: %.3f mm -> scale correction x%.4f" % (x_extent, corr))
report["scale_correction"] = float(corr)
report["final_bbox_min"] = mesh.bounds[0].tolist()
report["final_bbox_max"] = mesh.bounds[1].tolist()
report["final_volume_mm3"] = float(mesh.volume)

# final validation
assert mesh.is_watertight, "mesh not watertight after repair"
euler = len(mesh.vertices) - len(mesh.edges_unique) + len(mesh.faces)
report["euler_characteristic"] = int(euler)
print("euler characteristic: %d (2 = closed manifold)" % euler)

# ------------------------------------------------- export
stl_path = os.path.join(OUT, "part_clean.stl")
mesh.export(stl_path)   # binary STL
print("wrote", stl_path, os.path.getsize(stl_path), "bytes")
report["stl"] = stl_path

mf3_path = os.path.join(OUT, "part_clean.3mf")
try:
    mesh.export(mf3_path)
    report["3mf"] = mf3_path
    print("wrote", mf3_path, os.path.getsize(mf3_path), "bytes")
except Exception as e:
    report["3mf_error"] = str(e)
    print("3MF export failed:", e)

json.dump(report, open(os.path.join(OUT, "report.json"), "w"), indent=2)
print("REPORT:", json.dumps(report, indent=1))
