#!/usr/bin/env bash
# Step 4: slice the repaired mesh with OrcaSlicer (AppImage, prebuilt).
# Usage: ./04_slice.sh   (run from this directory)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# System lacks libEGL.so.1 / libGLU.so.1 (needed at load time); use copies
# extracted from Ubuntu's .debs in syslib/.
export LD_LIBRARY_PATH="$HERE/syslib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
OUT="$HERE/out"
APP="$HERE/orca.AppImage"
STL="$OUT/part_clean.stl"
GCODE="$OUT/part_clean.gcode"

[ -x "$APP" ] || { echo "missing $APP"; exit 1; }
[ -f "$STL" ] || { echo "missing $STL (run 03_postprocess.py --standin first)"; exit 1; }

# Extract once so we can run the slicer binary directly (faster + more robust
# headless than executing the AppImage on every run).
if [ ! -d "$HERE/orca-squashfs" ]; then
  echo "extracting AppImage..."
  (cd "$HERE" && "$APP" --appimage-extract >/dev/null)
  mv "$HERE/squashfs-root" "$HERE/orca-squashfs"
fi
BIN="$HERE/orca-squashfs/bin/orca-slicer"
[ -x "$BIN" ] || BIN="$HERE/orca-squashfs/usr/bin/orca-slicer"
echo "slicer binary: $BIN"
"$BIN" --version 2>&1 | head -2 || true

# Show a few bundled printer profiles so the log records what was available.
echo "--- sample bundled printer profiles ---"
ls "$HERE/orca-squashfs/resources/profiles/BBL/machine/" 2>/dev/null | head -5 || true

# Slice with a generic 0.2mm Standard profile on a Bambu Lab X1 Carbon
# (0.4 nozzle) + Generic PLA. xvfb provides the display the GUI toolkit needs.
echo "--- slicing ---"
MACHINE_JSON="$HERE/orca-squashfs/resources/profiles/BBL/machine/Bambu Lab X1 Carbon 0.4 nozzle.json"
PROCESS_JSON="$HERE/orca-squashfs/resources/profiles/BBL/process/0.20mm Standard @BBL X1C.json"
FILAMENT_JSON="$HERE/orca-squashfs/resources/profiles/BBL/filament/Generic PLA.json"
time xvfb-run -a "$BIN" --slice 0 \
  --load-settings "$MACHINE_JSON;$PROCESS_JSON" \
  --load-filaments "$FILAMENT_JSON" \
  --outputdir "$OUT" \
  "$STL" 2>&1 | tail -8
# normalize the generated gcode name
GCODE_GEN="$(ls -t "$OUT"/*.gcode 2>/dev/null | head -1 || true)"
if [ -n "$GCODE_GEN" ]; then mv -f "$GCODE_GEN" "$GCODE"; fi

echo "--- gcode validation ---"
ls -la "$GCODE"
grep -c "^G1 .*E" "$GCODE" | xargs echo "extrusion moves:"
grep -m1 "; total filament used" "$GCODE" || true
grep -m1 "; estimated printing time" "$GCODE" || true
head -3 "$GCODE"
