#!/usr/bin/env python3
"""Step 3c: watertight gate between mesh post-processing and slicing.

Replicates the function of RealityScan's "watertight mesh for 3D printing"
toggle as an explicit, inspectable gate:

  1. stats before repair (watertight?, boundary edges, euler, volume)
  2. hole filling (PyMeshFix repair + trimesh fill_holes)
  3. drop floaters (keep largest connected component)
  4. verify again: watertight, manifold, positive volume

Verdict: pass (already clean) / warn (repaired, now printable) /
         fail (still open after repair).
Writes <out_stl> + JSON report.
Exit 0 on pass/warn, exit 1 on fail so run_pipeline.sh stops before slicing.
"""
import argparse
import json
import os
import sys

import numpy as np
import trimesh


def boundary_edge_count(mesh):
    """Edges referenced by exactly one face."""
    _, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    return int((counts == 1).sum())


def stats(mesh):
    return {
        "watertight": bool(mesh.is_watertight),
        "is_volume": bool(mesh.is_volume),
        "boundary_edges": boundary_edge_count(mesh),
        "euler_number": int(mesh.euler_number),
        "faces": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "volume_mm3": float(mesh.volume) if mesh.is_volume else 0.0,
    }


def main():
    ap = argparse.ArgumentParser(description="Watertight gate before slicing")
    ap.add_argument("--in", dest="inp", default="out/part_clean.stl",
                    help="input STL from 03_postprocess.py")
    ap.add_argument("--out", dest="outp", default="out/part_watertight.stl",
                    help="repaired output STL")
    ap.add_argument("--report", default="out/watertight_report.json",
                    help="JSON report path")
    args = ap.parse_args()

    if not os.path.exists(args.inp):
        print("ERROR: missing input mesh %s (run 03_postprocess.py first)"
              % args.inp)
        return 1

    mesh = trimesh.load(args.inp, process=True)
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        print("ERROR: could not load a triangle mesh from %s" % args.inp)
        return 1
    mesh.remove_infinite_values()

    report = {"input": os.path.abspath(args.inp)}
    before = stats(mesh)
    report["before"] = before
    print("before: watertight=%s boundary_edges=%d euler=%d faces=%d" % (
        before["watertight"], before["boundary_edges"],
        before["euler_number"], before["faces"]))

    repaired = False

    # drop floaters FIRST: keep largest connected component by surface area.
    # (MeshFix misbehaves on multi-component input, so split before repair.)
    comps = mesh.split(only_watertight=False)
    report["components_found"] = len(comps)
    if len(comps) > 1:
        comps.sort(key=lambda m: m.area, reverse=True)
        dropped = sum(len(m.faces) for m in comps[1:])
        mesh = comps[0]
        repaired = True
        print("dropped %d floater component(s) (%d faces); kept %.0f mm^2" %
              (len(comps) - 1, dropped, mesh.area))

    # hole filling: PyMeshFix repair first, trimesh fill_holes as backup
    if not mesh.is_watertight:
        try:
            from pymeshfix import MeshFix
            mf = MeshFix(mesh.vertices, mesh.faces)
            mf.repair()
            mesh = trimesh.Trimesh(vertices=mf.points, faces=mf.faces,
                                   process=True)
            repaired = True
            print("MeshFix repair: watertight=%s" % mesh.is_watertight)
        except Exception as e:
            print("MeshFix repair failed: %s" % e)
        if not mesh.is_watertight:
            mesh.fill_holes()
            mesh.process(validate=True)
            repaired = True
            print("fill_holes: watertight=%s" % mesh.is_watertight)

    # fix inward winding so slicers see a solid
    if mesh.is_watertight and mesh.volume < 0:
        mesh.invert()
        print("inverted winding (volume was negative)")

    after = stats(mesh)
    report["after"] = after
    print("after:  watertight=%s boundary_edges=%d euler=%d faces=%d "
          "volume=%.1f mm^3" % (
              after["watertight"], after["boundary_edges"],
              after["euler_number"], after["faces"], after["volume_mm3"]))

    os.makedirs(os.path.dirname(os.path.abspath(args.outp)), exist_ok=True)
    mesh.export(args.outp)
    report["output"] = os.path.abspath(args.outp)
    report["output_bytes"] = os.path.getsize(args.outp)

    if after["watertight"] and after["volume_mm3"] > 0:
        report["verdict"] = "warn" if repaired else "pass"
        print("verdict: %s (%s)" % (
            report["verdict"],
            "mesh was repaired" if repaired else "already printable"))
    else:
        report["verdict"] = "fail"
        print("verdict: FAIL - mesh still not printable "
              "(watertight=%s, volume=%.3f)" %
              (after["watertight"], after["volume_mm3"]))

    report_dir = os.path.dirname(os.path.abspath(args.report))
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
    with open(args.report, "w") as f:
        json.dump(report, f, indent=2)
    print("wrote %s and %s" % (args.outp, args.report))
    return 0 if report["verdict"] in ("pass", "warn") else 1


if __name__ == "__main__":
    sys.exit(main())
