# 3D Pipeline Verification Report — 2026-09-14

End-to-end check of: synthetic phone-photo orbit → COLMAP photogrammetry → mesh repair/scale → STL/3MF → G-code.
Workspace: `~/workspace/verify-3d-pipeline/`

**Bottom line:** the full pipeline runs and produces a valid, watertight, correctly scaled STL/3MF and real G-code.
One stage is genuinely blocked: COLMAP's dense PatchMatchStereo requires CUDA (no GPU on this host), so dense
reconstruction was exercised through a clearly labeled synthetic stand-in. The real sparse reconstruction and all
downstream stages (repair, scale, export, slice) ran for real.

## Stage table

| stage | tool used | result | output path | notes |
|---|---|---|---|---|
| 1. synthetic capture (48 orbit views) | `01_render.py` (pyrender, headless OpenGL) | ✅ PASS | `images/` (48 JPEGs), `part/bracket.stl`, `part/cameras_known.txt` | 3 elevation rings × 16 azimuths; known X reference 50.0 mm; GT bounds [-25,-18,0.3]–[25,18,38.3] mm |
| 2. sparse reconstruction | COLMAP 3.13.0 (conda-forge, CPU) | ✅ PASS | `colmap/sparse/0/` | 48/48 images registered; 3,849 points; 25,051 obs; mean track 6.508; mean reproj error 0.3725 px |
| 3. dense reconstruction | COLMAP PatchMatchStereo + stereo_fusion | ❌ BLOCKED | `colmap/dense/fused.ply` (0 points) | PatchMatchStereo is CUDA-only in this build; host has no GPU ("CUDA driver version is insufficient"). Log: `colmap_dense.log` |
| 3b. dense STAND-IN (labeled synthetic) | `03b_fuse_standin.py` (pyrender depth + noise, voxel fuse, pymeshlab screened Poisson) | ⚠️ STAND-IN, ran OK | `colmap/dense/fused_standin.ply`, `colmap/dense/meshed_standin.ply` | 2,264,874 raw → 224,404 (1 mm voxel) → 25,301 (ROI) → 192,092-face Poisson. Stands in ONLY for the CUDA-blocked dense MVS |
| 4. repair + scale + export | `03_postprocess.py --standin` (trimesh, pymeshfix) | ✅ PASS | `out/part_raw.ply`, `out/part_clean.stl` (5.79 MB), `out/part_clean.3mf` (1.65 MB), `out/report.json` | watertight=true, is_volume=true, Euler=2; 1 component; scale correction 0.97384; final X extent exactly 50.0 mm; bounds ±25/±18, z −0.02–37.45; volume 26,168.8 mm³ |
| 5. surface accuracy vs GT | trimesh sampling | ✅ measured | — | recon→GT: median 0.46 mm, p90 2.37 mm, max 9.86 mm; GT→recon: median 0.47 mm, p90 2.03 mm, max 8.08 mm |
| 6. slicing | OrcaSlicer 2.4.2 (Ubuntu 24.04 AppImage) | ✅ PASS | `out/part_clean.gcode` (3.41 MB, 130,803 lines) | 102,701 extrusion moves; Generic PLA; Bambu Lab X1 Carbon 0.4 nozzle; "0.20mm Standard @BBL X1C"; model time 1h 40m 10s, total est 1h 49m 49s |

## Timings (measured 2026-09-14)

| step | time |
|---|---|
| render 48 views | ~6.9 s |
| COLMAP sparse (feature extract + match + map) | 66.0 s |
| COLMAP dense attempt (failed on CUDA) | ~1 min before abort |
| dense stand-in (fuse + Poisson) | 15.7 s |
| postprocess (repair/scale/export) | 10.5 s |
| OrcaSlicer AppImage download (131 MB) | ~43 s |
| slicing | ~11 s |

## Exact rerun commands

```bash
cd ~/workspace/verify-3d-pipeline
./venv/bin/python 01_render.py                                   # synthetic capture
./02_colmap.sh sparse                                           # real sparse (COLMAP)
./02_colmap.sh dense                                            # EXPECTED TO FAIL: CUDA-only PatchMatchStereo
LD_LIBRARY_PATH=$PWD/syslib xvfb-run -a -s "-screen 0 1280x1024x24" \
  ./venv/bin/python 03b_fuse_standin.py                        # labeled dense stand-in
./venv/bin/python 03_postprocess.py --standin                   # repair/scale/STL/3MF
./04_slice.sh                                                   # OrcaSlicer -> out/part_clean.gcode
```

## Environment notes

- Ubuntu 24.04.5 LTS, Python 3.12.3, 2 CPU, ~7 GB RAM, no GPU, no /dev/dri. Xvfb + headless OpenGL work.
- venv `venv/`: trimesh 5.1.0, numpy 2.5.3, scipy 1.18.1, pillow 12.3.0, pyrender 0.1.45,
  opencv-python-headless 5.0.0, pymeshfix 0.18.1, pymeshlab 2025.7.post1, lxml; PyOpenGL upgraded to 3.1.10
  (pyrender's pinned 3.1.0 breaks with NumPy 2.x).
- pymeshlab needs `LD_LIBRARY_PATH=$PWD/syslib` with `syslib/libOpenGL.so.0 -> /lib/x86_64-linux-gnu/libGL.so.1`.
- COLMAP 3.13.0 from conda-forge (`colmap-env/bin/colmap`); uses `SiftExtraction.*` / `FeatureExtraction.use_gpu`
  / `FeatureMatching.use_gpu` option names (older names were removed).
- OrcaSlicer binary needs several Ubuntu system libs not present on this minimal host; `04_slice.sh` pulls them
  from `syslib/` (libEGL.so.1, libGLU.so.1 extracted from Ubuntu .debs; webkit2gtk stack etc.).
- Correct OrcaSlicer CLI: `orca-slicer --slice 0 --load-settings "machine.json;process.json" --load-filaments "filament.json" --outputdir <dir> model.stl`
  (bare `--slice` and `--printer/--filament/--process` flags do not exist in 2.4.2).

## Honest limitations

1. **Real COLMAP dense reconstruction did NOT run** — no GPU on this host and this COLMAP build has no CPU
   PatchMatch mode. The dense stage was validated with a synthetic stand-in (rendered depth from known poses +
   0.25 mm noise), which is realistic but not a substitute for real MVS on real phone photos.
2. The sparse→known-camera alignment path was attempted but produced poor residuals (median ~30.6 mm) and is
   NOT claimed as validated; the stand-in path skips alignment (already in the true mm frame).
3. Nothing here used real phone photos — the capture stage is synthetic by design (40–80 view requirement met
   with 48 rendered views).
