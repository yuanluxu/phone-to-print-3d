#!/usr/bin/env python3
"""Step 1b: capture quality check for phone photos, before reconstruction.

Mirrors the pre-flight checks in RealityScan (blur / unconnected flags):
every photo gets checked for

  - blur ............ OpenCV Laplacian variance below --blur-thresh
  - exposure ........ fraction of clipped-black / clipped-white pixels
  - resolution ...... differs from the modal (most common) resolution
  - duplicates ...... near-duplicate of an earlier photo (average hash,
                      Hamming distance <= --dup-dist)

Each photo gets a verdict: "ok" or "retake" (with reasons).
Writes qc_report.json and prints a console summary.
Exit 0 on success (even when some photos need retakes);
exit 1 when no readable images were found at all.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("ERROR: opencv is required (pip install -r requirements.txt)")

IMG_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp")


def laplacian_variance(gray):
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure_stats(gray):
    """Fraction of clipped-black (<8) and clipped-white (>247) pixels."""
    black = float(np.mean(gray < 8))
    white = float(np.mean(gray > 247))
    return black, white


def ahash(gray):
    """64-bit average hash."""
    small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
    bits = (small > small.mean()).astype(np.uint64).ravel()
    h = np.uint64(0)
    for b in bits:
        h = (h << np.uint64(1)) | b
    return h


def hamming(a, b):
    return bin(int(a ^ b)).count("1")


def find_images(img_dir):
    files = []
    for ext in IMG_EXTS:
        files.extend(glob.glob(os.path.join(img_dir, "*" + ext)))
        files.extend(glob.glob(os.path.join(img_dir, "*" + ext.upper())))
    return sorted(set(files))


def main():
    ap = argparse.ArgumentParser(description="Capture QC for photogrammetry photos")
    ap.add_argument("images", help="directory with input photos")
    ap.add_argument("--out", default="qc_report.json", help="JSON report path")
    ap.add_argument("--blur-thresh", type=float, default=100.0,
                    help="Laplacian variance below this => blurry (default 100)")
    ap.add_argument("--over-frac", type=float, default=0.25,
                    help="clipped-white fraction above this => overexposed")
    ap.add_argument("--under-frac", type=float, default=0.50,
                    help="clipped-black fraction above this => underexposed")
    ap.add_argument("--dup-dist", type=int, default=5,
                    help="average-hash Hamming distance <= this => near-duplicate")
    ap.add_argument("--no-dup", action="store_true", help="skip duplicate detection")
    args = ap.parse_args()

    files = find_images(args.images)
    if not files:
        print("ERROR: no images found in %s" % args.images)
        return 1

    photos = []
    for f in files:
        rec = {"file": os.path.basename(f), "reasons": [], "verdict": "ok"}
        img = cv2.imread(f, cv2.IMREAD_COLOR)
        if img is None:
            rec["reasons"].append("unreadable")
            rec["verdict"] = "retake"
            photos.append(rec)
            continue
        h, w = img.shape[:2]
        rec["width"], rec["height"] = int(w), int(h)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        blur = laplacian_variance(gray)
        rec["blur_score"] = round(blur, 1)
        if blur < args.blur_thresh:
            rec["reasons"].append("blurry (laplacian var %.1f < %.0f)"
                                 % (blur, args.blur_thresh))

        black, white = exposure_stats(gray)
        rec["black_frac"] = round(black, 4)
        rec["white_frac"] = round(white, 4)
        if white > args.over_frac:
            rec["reasons"].append("overexposed (%.1f%% clipped white)"
                                 % (white * 100))
        if black > args.under_frac:
            rec["reasons"].append("underexposed (%.1f%% clipped black)"
                                 % (black * 100))

        rec["ahash"] = "%016x" % ahash(gray)
        if rec["reasons"]:
            rec["verdict"] = "retake"
        photos.append(rec)

    # resolution consistency: flag anything off the modal resolution
    sizes = [(p.get("width"), p.get("height")) for p in photos
             if "width" in p]
    if sizes:
        mode = max(set(sizes), key=sizes.count)
        for p in photos:
            if "width" in p and (p["width"], p["height"]) != mode:
                p["reasons"].append(
                    "resolution %dx%d differs from modal %dx%d"
                    % (p["width"], p["height"], mode[0], mode[1]))
                p["verdict"] = "retake"

    # near-duplicates (pairwise average hash).
    # Only compare photos that are otherwise clean: hashes of blurry or
    # clipped images are unstable and cause false positives.
    if not args.no_dup:
        clean = [p for p in photos if p["verdict"] == "ok" and "ahash" in p]
        hashes = [(p, int(p["ahash"], 16)) for p in clean]
        for i in range(len(hashes)):
            for j in range(i):
                if hamming(hashes[i][1], hashes[j][1]) <= args.dup_dist:
                    hashes[i][0]["reasons"].append(
                        "near-duplicate of %s" % hashes[j][0]["file"])
                    hashes[i][0]["duplicate_of"] = hashes[j][0]["file"]
                    hashes[i][0]["verdict"] = "retake"
                    break

    for p in photos:
        p.pop("ahash", None)

    n_retake = sum(1 for p in photos if p["verdict"] == "retake")
    by_reason = {}
    for p in photos:
        for r in p["reasons"]:
            key = r.split(" (")[0]
            by_reason[key] = by_reason.get(key, 0) + 1

    report = {
        "images_dir": os.path.abspath(args.images),
        "thresholds": {
            "blur_laplacian_var": args.blur_thresh,
            "overexposed_white_frac": args.over_frac,
            "underexposed_black_frac": args.under_frac,
            "dup_hamming_dist": args.dup_dist,
        },
        "photos": photos,
        "summary": {
            "total": len(photos),
            "ok": len(photos) - n_retake,
            "retake": n_retake,
            "retake_ratio": round(n_retake / len(photos), 4),
            "by_reason": by_reason,
        },
    }
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    # console summary
    print("capture QC: %d photos (%d ok, %d retake, ratio %.1f%%)" % (
        len(photos), len(photos) - n_retake, n_retake,
        100.0 * n_retake / len(photos)))
    for r, c in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print("  - %s: %d" % (r, c))
    for p in photos:
        if p["verdict"] == "retake":
            print("  RETAKE %s: %s" % (p["file"], "; ".join(p["reasons"])))
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
