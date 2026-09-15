#!/bin/bash
# Step 2: COLMAP SfM (sparse) then MVS (dense) + meshing.
# Usage: 02_colmap.sh [sparse|dense|mesh|all]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
COLMAP="$HERE/colmap-env/bin/colmap"
MODE="${1:-sparse}"

run_sparse() {
  mkdir -p "$HERE/colmap/sparse"
  rm -f "$HERE/colmap/db.db"
  echo "=== feature extraction ==="
  "$COLMAP" feature_extractor \
    --database_path "$HERE/colmap/db.db" \
    --image_path "$HERE/images" \
    --ImageReader.camera_model PINHOLE \
    --ImageReader.single_camera 1 \
    --SiftExtraction.max_num_features 4096 \
    --FeatureExtraction.use_gpu 0
  echo "=== sequential matching ==="
  "$COLMAP" sequential_matcher \
    --database_path "$HERE/colmap/db.db" \
    --SequentialMatching.overlap 12 \
    --FeatureMatching.use_gpu 0
  echo "=== mapper ==="
  "$COLMAP" mapper \
    --database_path "$HERE/colmap/db.db" \
    --image_path "$HERE/images" \
    --output_path "$HERE/colmap/sparse"
  echo "=== model stats ==="
  "$COLMAP" model_analyzer --path "$HERE/colmap/sparse/0" 2>/dev/null | head -12
}

run_dense() {
  echo "=== undistort ==="
  "$COLMAP" image_undistorter \
    --image_path "$HERE/images" \
    --input_path "$HERE/colmap/sparse/0" \
    --output_path "$HERE/colmap/dense" \
    --output_type COLMAP
  echo "=== patch match stereo ==="
  "$COLMAP" patch_match_stereo \
    --workspace_path "$HERE/colmap/dense" \
    --PatchMatchStereo.max_image_size 640 \
    --PatchMatchStereo.geom_consistency true
  echo "=== stereo fusion ==="
  "$COLMAP" stereo_fusion \
    --workspace_path "$HERE/colmap/dense" \
    --output_path "$HERE/colmap/dense/fused.ply" \
    --StereoFusion.min_num_pixels 3
}

run_mesh() {
  echo "=== poisson meshing ==="
  "$COLMAP" poisson_mesher \
    --input_path "$HERE/colmap/dense/fused.ply" \
    --output_path "$HERE/colmap/dense/meshed.ply"
  ls -la "$HERE/colmap/dense/"*.ply
}

case "$MODE" in
  sparse) run_sparse ;;
  dense) run_dense ;;
  mesh) run_mesh ;;
  all) run_sparse; run_dense; run_mesh ;;
esac
