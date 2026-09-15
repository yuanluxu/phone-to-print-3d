# phone-to-print-3d

End-to-end open-source pipeline that clones a physical part from cellphone photos into a 3D-printer-ready file: photos → photogrammetry → mesh repair & scaling → STL/3MF → G-code.

## Pipeline

| Step | Script | What it does |
|---|---|---|
| 1. Capture | `01_render.py` | Renders 48 orbit views of a test bracket (synthetic stand-in — replace `images/` with your own phone photos) |
| 2a. Sparse reconstruction | `02_colmap.sh sparse` | COLMAP SfM: feature extraction → matching → mapping (CPU) |
| 2b. Dense reconstruction | `02_colmap.sh dense` | COLMAP PatchMatchStereo + fusion — **requires a CUDA GPU** |
| 3. Dense stand-in (no GPU) | `03b_fuse_standin.py` | Labeled fallback: fuses rendered depth + noise into a dense cloud when dense MVS can't run |
| 4. Repair, scale, export | `03_postprocess.py` | PyMeshFix repair → scale to real mm → watertight check → binary STL + 3MF |
| 5. Slice | `04_slice.sh` | OrcaSlicer CLI → print-ready G-code |

`REPORT.md` has the full verification log; `result.json` has the machine-readable summary.

## Verified results (2026-09-14, Ubuntu 24.04, no GPU)

- Sparse: 48/48 images registered, 3,849 points, 0.37 px mean reprojection error (COLMAP 3.13, CPU)
- Post: watertight STL (5.79 MB) + 3MF, X extent exactly 50.0 mm, volume 26,168.8 mm³
- Accuracy vs ground truth: median 0.46 mm surface error
- Slice: 3.41 MB G-code, 102,701 extrusion moves (OrcaSlicer 2.4.2, 0.20 mm, PLA)

Dense MVS did **not** run on the verification host (COLMAP's PatchMatchStereo is CUDA-only there); the dense stage was exercised through the labeled synthetic stand-in. On a CUDA machine, or with phone-app reconstruction (KIRI Engine, Polycam), use the real dense path.

## Requirements

- Python 3.12 (`pip install -r requirements.txt`)
- COLMAP ≥ 3.8 (3.13 used here; `conda install -c conda-forge colmap`)
- OrcaSlicer 2.4.2 AppImage (for `04_slice.sh`)
- CUDA GPU for the real dense step (optional — see stand-in)

## Quick start

```bash
python 01_render.py            # or drop your phone photos into images/
./02_colmap.sh sparse
./02_colmap.sh dense          # needs CUDA; skip on CPU-only machines
python 03b_fuse_standin.py    # only if you skipped real dense
python 03_postprocess.py --standin
./04_slice.sh
```

## Honest limitations

1. Real COLMAP dense reconstruction was not exercised here — no GPU on the verification host.
2. Capture is synthetic by design; real phone photos are the intended input.
3. Phone photogrammetry yields roughly ±0.1–0.5 mm — fine for cosmetic/functional clones, not press-fit tolerances. For tight fits, measure with calipers and remodel in CAD.
