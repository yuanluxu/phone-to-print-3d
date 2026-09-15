#!/usr/bin/env bash
# Full pipeline with the Stage-0 buy-vs-print gate.
#
#   ./run_pipeline.sh [--stop-after N] [-- <extra args for 00 --decide>]
#
# Stage 0 decides BUY -> prints where to buy and stops (no scanning).
# Stage 0 decides PRINT/UNCERTAIN -> continues through 01..04.
set -euo pipefail
cd "$(dirname "$0")"

STOP_AFTER="${STOP_AFTER:-99}"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --stop-after) STOP_AFTER="$2"; shift 2 ;;
    --) shift; ARGS+=("$@"); break ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

stage() { # $1 = number, $2 = description
  if [ "$STOP_AFTER" -lt "$1" ]; then
    echo "Stopping after stage $STOP_AFTER (requested)."
    exit 0
  fi
  echo "### Stage $1: $2"
}

# ---------------------------------------------------------------- Stage 0
stage 0 "buy-vs-print decision"
set +e
python3 00_buy_vs_print.py --decide "${ARGS[@]}"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -eq 2 ]; then
    echo "--- no candidates yet; building a sourcing brief ---"
    python3 00_buy_vs_print.py --brief "${ARGS[@]}" || true
    echo "Fill sourcing/candidates.json (see sourcing/candidates.example.json),"
    echo "then re-run ./run_pipeline.sh"
  fi
  exit "$rc"
fi

DECISION="$(python3 -c "import json;print(json.load(open('sourcing/decision.json'))['decision'])")"
if [ "$DECISION" = "BUY" ]; then
  echo "--- verdict: BUY. Buy the part instead of printing it. Pipeline stops here. ---"
  python3 -c "
import json
d = json.load(open('sourcing/decision.json'))
b = d['buy']
print('Buy:', b['title'])
print('Landed cost: \$%.2f [%s]' % (b['landed_usd'], b['source']))
print('Link:', b['url'] or '(no url recorded)')
print('Why:', d['reason'])
"
  exit 0
fi
echo "--- verdict: $DECISION. Continuing to the scan-print pipeline. ---"

# ---------------------------------------------------------------- Stage 1+
stage 1 "capture (renders synthetic orbit views; replace images/ with phone photos)"
python3 01_render.py
echo "--- tip: open capture-guide.html on your phone for a guided shoot plan ---"

stage 2 "capture QC (blur/exposure/resolution/duplicates)"
if ls images/*.jpg images/*.jpeg images/*.png >/dev/null 2>&1; then
  python3 01b_capture_qc.py images/ --out out/qc_report.json
  RETAKE_RATIO="$(python3 -c "import json;print(json.load(open('out/qc_report.json'))['summary']['retake_ratio'])")"
  QC_MAX_RETAKE="${QC_MAX_RETAKE:-0.25}"
  if awk "BEGIN{exit !( $RETAKE_RATIO > $QC_MAX_RETAKE )}"; then
    echo "!!! capture QC: retake ratio $RETAKE_RATIO exceeds limit $QC_MAX_RETAKE"
    echo "    see out/qc_report.json; re-shoot the flagged photos for best results"
    if [ -t 0 ]; then
      read -r -p "Continue anyway? [y/N] " ans
      case "$ans" in
        [Yy]*) echo "--- continuing with $RETAKE_RATIO retake ratio ---" ;;
        *) echo "aborted by user"; exit 1 ;;
      esac
    else
      echo "--- non-interactive shell: continuing with warning ---"
    fi
  fi
else
  echo "--- no images found; skipping capture QC ---"
fi

stage 3 "COLMAP sparse reconstruction (CPU)"
./02_colmap.sh sparse

stage 4 "COLMAP dense reconstruction (needs CUDA; skip on CPU-only hosts)"
if ./02_colmap.sh dense; then
  echo "dense MVS ok"
else
  echo "dense MVS unavailable -> synthetic dense stand-in (labeled fallback)"
  LD_LIBRARY_PATH="$PWD/syslib" xvfb-run -a python3 03b_fuse_standin.py
fi

stage 5 "mesh repair, scale to mm, export STL/3MF"
python3 03_postprocess.py --standin

stage 6 "watertight gate (blocks slicing if the mesh is not printable)"
if ! python3 03c_watertight_gate.py \
    --in out/part_clean.stl \
    --out out/part_watertight.stl \
    --report out/watertight_report.json; then
  echo "!!! watertight gate FAILED: mesh is not printable."
  echo "    Inspect out/watertight_report.json, then fix the mesh"
  echo "    (more/better photos, manual repair) and re-run."
  exit 1
fi

stage 7 "slice to G-code"
./04_slice.sh

echo "=== pipeline complete ==="
