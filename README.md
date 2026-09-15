# phone-to-print-3d

End-to-end open-source pipeline that clones a physical part from cellphone photos into a 3D-printer-ready file: **buy-vs-print check** → photos → photogrammetry → mesh repair & scaling → STL/3MF → G-code.

## Pipeline

| Step | Script | What it does |
|---|---|---|
| 0. Buy-vs-print gate | `00_buy_vs_print.py` | From your part photos + description, searches for buyable equivalents and compares landed price vs true print cost; verdict BUY stops here, PRINT continues |
| 1a. Capture | `01_render.py` | Renders 48 orbit views of a test bracket (synthetic stand-in — replace `images/` with your own phone photos) |
| 1b. Capture QC | `01b_capture_qc.py` | Flags blurry / over-/under-exposed / wrong-resolution / duplicate photos before you waste compute; writes `qc_report.json` |
| 1c. Capture guide | `capture-guide.html` | Phone-friendly single-file page (Chinese): ring shoot plan, tap-to-mark coverage grid, checklist, turntable timer — open in a mobile browser |
| 2a. Sparse reconstruction | `02_colmap.sh sparse` | COLMAP SfM: feature extraction → matching → mapping (CPU) |
| 2b. Dense reconstruction | `02_colmap.sh dense` | COLMAP PatchMatchStereo + fusion — **requires a CUDA GPU** |
| 3. Dense stand-in (no GPU) | `03b_fuse_standin.py` | Labeled fallback: fuses rendered depth + noise into a dense cloud when dense MVS can't run |
| 4. Repair, scale, export | `03_postprocess.py` | PyMeshFix repair → scale to real mm → watertight check → binary STL + 3MF |
| 4b. Watertight gate | `03c_watertight_gate.py` | Final printability gate: hole fill → drop floaters → verify watertight/manifold; writes `part_watertight.stl` + JSON report; blocks slicing on failure |
| 5. Slice | `04_slice.sh` | OrcaSlicer CLI → print-ready G-code (slices the watertight-gate output) |

`REPORT.md` has the full verification log; `result.json` has the machine-readable summary.

## Stage 0: buy before you print

Scanning and printing a part costs real time and filament. Stage 0 answers
first: *can you just buy it, and is buying cheaper?*

```bash
# 1. build a sourcing brief from your photos (contact sheet + search queries)
python 00_buy_vs_print.py --brief --photos images/ \
  --desc "dishwasher lower rack wheel, Bosch SHXM63WS5N" \
  --dims 48x32x20mm --material PETG --qty 4

# 2. search the web / reverse-image-search the contact sheet (Google Lens),
#    paste real listings into sourcing/candidates.json
#    (template: sourcing/candidates.example.json).
#    With SERPAPI_API_KEY or BRAVE_API_KEY set, step 1 also tries an
#    automatic shopping search and pre-fills candidates (review them!).

# 3. decide
python 00_buy_vs_print.py --decide
```

Decision logic (`sourcing/decision.json`):

- **Print cost** is estimated from the bounding box: filament (density × $/kg per
  material) + machine hours (~12 g/h) + electricity + 20% failure/waste markup,
  plus optional labor (`--labor-rate`).
- **Buy cost** is the cheapest candidate's landed price (price + shipping).
- `BUY` if buy ≤ 80% of print cost; `PRINT` if print ≤ 80% of buy cost;
  otherwise `UNCERTAIN` with a tie-break note (lead time vs machine time).
- Hard overrides: no candidates found → `PRINT`; required tolerance < 0.3 mm
  (tighter than phone photogrammetry can hold) → `BUY`/remodel in CAD.

`./run_pipeline.sh` runs the whole flow: on `BUY` it prints where to buy and
stops before any scanning; on `PRINT` it continues through steps 1–5.

```bash
./run_pipeline.sh -- --dims 48x32x20mm --material PETG   # extra args go to stage 0
python 00_buy_vs_print.py --demo   # verify the decision math on 4 synthetic scenarios
```

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
# (optional, recommended) open capture-guide.html on your phone while shooting
python 01b_capture_qc.py images/ --out qc_report.json   # flag bad photos first
./02_colmap.sh sparse
./02_colmap.sh dense          # needs CUDA; skip on CPU-only machines
python 03b_fuse_standin.py    # only if you skipped real dense
python 03_postprocess.py --standin
python 03c_watertight_gate.py # printability gate; blocks slicing on failure
./04_slice.sh                 # slices out/part_watertight.stl
```

`./run_pipeline.sh` runs all of the above in order: Stage 0 decides BUY
(stop) vs PRINT/UNCERTAIN (continue); capture QC warns when the retake ratio
exceeds 25% (`QC_MAX_RETAKE` env, prompts on a TTY); the watertight gate stops
the pipeline before slicing if the mesh is not printable.

## Honest limitations

1. Real COLMAP dense reconstruction was not exercised here — no GPU on the verification host.
2. Capture is synthetic by design; real phone photos are the intended input.
3. Phone photogrammetry yields roughly ±0.1–0.5 mm — fine for cosmetic/functional clones, not press-fit tolerances. For tight fits, measure with calipers and remodel in CAD.
