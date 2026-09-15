#!/usr/bin/env python3
"""Stage 0: Buy-vs-print decision gate.

Before spending hours photographing, reconstructing and repairing, answer the
obvious question first: can you just BUY this part, and is buying cheaper
than printing it?

Workflow:
    1.  python 00_buy_vs_print.py --brief --photos images/ \
            --desc "dishwasher lower rack wheel, Bosch SHXM63WS5N" \
            --dims "48x32x20mm" --material PETG
        -> writes sourcing/brief.json + sourcing/contact_sheet.jpg
           (photos + suggested search queries for a web / image search,
           e.g. Google Lens, or an AI assistant).

    2.  Fill in sourcing/candidates.json with real listings
        (see sourcing/candidates.example.json). If SERPAPI_API_KEY or
        BRAVE_API_KEY is set, --brief also tries an automatic shopping
        search and pre-fills candidates (always review them).

    3.  python 00_buy_vs_print.py --decide
        -> estimates the true cost of printing (filament + machine time +
           electricity + failure risk + optional labor), compares it with
           the cheapest buyable candidate, and writes sourcing/decision.json:
           {"decision": "BUY" | "PRINT" | "UNCERTAIN", ...}

    4.  ./run_pipeline.sh reads the decision: BUY stops the pipeline and
        shows where to buy; PRINT continues to 01_render.py ...

    python 00_buy_vs_print.py --demo runs the decision math on three
    built-in synthetic scenarios (buy-cheaper / print-cheaper / no listing)
    so the logic itself is verifiable without any network access.

Exit codes: 0 = a decision was written; 2 = candidates are still missing
(run --brief first and fill in candidates.json).
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, "sourcing")
BRIEF_PATH = os.path.join(SRC_DIR, "brief.json")
CAND_PATH = os.path.join(SRC_DIR, "candidates.json")
DECISION_PATH = os.path.join(SRC_DIR, "decision.json")
EXAMPLE_CAND = os.path.join(SRC_DIR, "candidates.example.json")

# material -> (density g/cm^3, filament $/kg typical 2026 US retail)
MATERIALS = {
    "PLA":   (1.24, 25.0),
    "PETG":  (1.27, 28.0),
    "ABS":   (1.04, 26.0),
    "ASA":   (1.07, 45.0),
    "TPU":   (1.21, 40.0),
    "NYLON": (1.14, 60.0),
    "PC":    (1.20, 55.0),
}

DEFAULTS = {
    "fill": 0.35,          # fraction of the bounding box that ends up as
                           # plastic (part solidity x infill x walls). 0.35
                           # fits typical brackets/clips; override with --fill.
    "support_mult": 1.15,  # extra plastic for supports/brim
    "grams_per_hour": 12.0,# typical FDM throughput at 0.2 mm layers
    "printer_kw": 0.12,    # average draw while printing
    "kwh_price": 0.18,     # USD
    "fail_mult": 1.20,     # 20% expected reprint/waste markup
    "setup_hours": 0.5,    # photo/slicer setup when labor is counted
    "post_hours": 0.25,    # support removal / cleanup when labor is counted
    "buy_win": 0.80,       # buy_total <= 0.80 * print_total  -> BUY
    "print_win": 0.80,     # print_total <= 0.80 * buy_total -> PRINT
    "tight_tol_mm": 0.30,  # below this, phone-scan+print is unlikely to hold it
}


# ------------------------------------------------------------------ helpers
def parse_dims(s):
    """'48x32x20mm' / '48 x 32 x 20' / '48,32,20' -> (48.0, 32.0, 20.0) mm."""
    nums = [float(x) for x in re.findall(r"[\d.]+", s or "")]
    if len(nums) < 3:
        raise ValueError(f"need 3 dimensions, got {s!r} (e.g. 48x32x20mm)")
    return tuple(nums[:3])


def money(x):
    return f"${x:,.2f}"


# ------------------------------------------------------------- print costing
def estimate_print_cost(dims_mm, material="PLA", qty=1, labor_rate=0.0,
                        fill=None, kwh_price=None):
    """True-cost estimate of FDM-printing a part from its bounding box."""
    mat = material.upper()
    if mat not in MATERIALS:
        raise ValueError(f"unknown material {material!r}; choose from "
                         + ", ".join(sorted(MATERIALS)))
    density, price_kg = MATERIALS[mat]
    fill = DEFAULTS["fill"] if fill is None else fill
    kwh_price = DEFAULTS["kwh_price"] if kwh_price is None else kwh_price

    bbox_cm3 = (dims_mm[0] / 10) * (dims_mm[1] / 10) * (dims_mm[2] / 10)
    grams_each = bbox_cm3 * fill * density * DEFAULTS["support_mult"]
    grams = grams_each * qty

    filament_cost = grams / 1000 * price_kg
    hours_each = grams_each / DEFAULTS["grams_per_hour"]
    hours = hours_each * qty
    elec_cost = hours * DEFAULTS["printer_kw"] * kwh_price
    labor_cost = (labor_rate or 0.0) * (DEFAULTS["setup_hours"]
                                       + DEFAULTS["post_hours"] * qty)

    subtotal = filament_cost + elec_cost + labor_cost
    total = subtotal * DEFAULTS["fail_mult"]
    return {
        "material": mat,
        "dims_mm": list(dims_mm),
        "qty": qty,
        "bbox_cm3": round(bbox_cm3, 1),
        "fill_factor": fill,
        "grams_total": round(grams, 1),
        "hours_total": round(hours, 1),
        "filament_cost": round(filament_cost, 2),
        "electricity_cost": round(elec_cost, 2),
        "labor_cost": round(labor_cost, 2),
        "failure_markup": f"{int((DEFAULTS['fail_mult'] - 1) * 100)}%",
        "total_usd": round(total, 2),
    }


# ------------------------------------------------------------ buy candidates
def load_candidates(path=CAND_PATH):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    cands = data.get("candidates", data) if isinstance(data, dict) else data
    norm = []
    for c in cands:
        price = float(c.get("price_usd", 0) or 0)
        ship = float(c.get("shipping_usd", 0) or 0)
        norm.append({
            "title": c.get("title", "untitled"),
            "url": c.get("url", ""),
            "source": c.get("source", ""),
            "price_usd": price,
            "shipping_usd": ship,
            "landed_usd": round(price + ship, 2),
            "lead_time_days": c.get("lead_time_days"),
            "oem": bool(c.get("oem", False)),
            "notes": c.get("notes", ""),
            "auto": bool(c.get("auto", False)),
        })
    norm.sort(key=lambda c: c["landed_usd"])
    return norm


# ------------------------------------------------------------------ decide
def decide(buy_cands, print_cost, tolerance_mm=None, qty=1):
    """Return the decision dict. Pure function -> easy to unit-verify."""
    p_total = print_cost["total_usd"]
    if not buy_cands:
        return {
            "decision": "PRINT",
            "reason": ("No buyable equivalent found in candidates.json. "
                       "Printing is the only path short of remodeling the "
                       "part in CAD."),
            "buy": None,
            "print": print_cost,
        }

    best = buy_cands[0]
    b_total = best["landed_usd"]
    reasons = []

    # Hard override: functional tolerance tighter than phone-scan can hold.
    if tolerance_mm is not None and tolerance_mm < DEFAULTS["tight_tol_mm"]:
        return {
            "decision": "BUY",
            "reason": (f"Required tolerance ({tolerance_mm} mm) is tighter "
                       f"than phone photogrammetry can reliably hold "
                       f"(~0.3-0.5 mm at best). Buy the OEM/part or remodel "
                       f"in CAD instead of scan-printing."),
            "buy": best,
            "print": print_cost,
            "all_candidates": buy_cands,
        }

    if b_total <= DEFAULTS["buy_win"] * p_total:
        verdict, why = "BUY", (f"Cheapest listing {money(b_total)} landed is "
                               f"clearly below estimated print cost "
                               f"{money(p_total)} — buying wins on price.")
    elif p_total <= DEFAULTS["print_win"] * b_total:
        verdict, why = "PRINT", (f"Estimated print cost {money(p_total)} is "
                                f"clearly below cheapest listing "
                                f"{money(b_total)} landed — printing wins.")
    else:
        verdict = "UNCERTAIN"
        why = (f"Too close to call: buy {money(b_total)} vs print "
               f"{money(p_total)} (within 20%). ")
        if best.get("lead_time_days"):
            why += (f"The listing ships in ~{best['lead_time_days']}d; "
                    f"printing takes ~{print_cost['hours_total']}h of machine "
                    f"time. ")
        why += ("Decide on urgency and fit risk: need it fast or a guaranteed "
                "fit -> buy; enjoy the process or part is cosmetic -> print.")
    if best.get("auto"):
        why += (" NOTE: this listing came from an automatic search — verify "
                "it is actually the right part before ordering.")
    return {
        "decision": verdict,
        "reason": why,
        "buy": best,
        "print": print_cost,
        "all_candidates": buy_cands,
    }


def print_report(d):
    print("=" * 64)
    print(f"DECISION: {d['decision']}")
    print("=" * 64)
    print(f"Why: {d['reason']}")
    p = d["print"]
    print(f"\nPrint cost estimate ({p['material']}, "
          f"{'x'.join(str(x) for x in p['dims_mm'])} mm, qty {p['qty']}):")
    print(f"  filament : {money(p['filament_cost'])} ({p['grams_total']} g)")
    print(f"  machine  : ~{p['hours_total']} h")
    print(f"  elec.    : {money(p['electricity_cost'])}")
    if p["labor_cost"]:
        print(f"  labor    : {money(p['labor_cost'])}")
    print(f"  +{p['failure_markup']} failure/waste markup")
    print(f"  TOTAL    : {money(p['total_usd'])}")
    if d["buy"]:
        b = d["buy"]
        print(f"\nCheapest buyable candidate:")
        print(f"  {b['title']}")
        print(f"  {money(b['price_usd'])} + {money(b['shipping_usd'])} ship "
              f"= {money(b['landed_usd'])} landed  [{b['source']}]")
        if b["url"]:
            print(f"  {b['url']}")
        rest = d.get("all_candidates", [])[1:4]
        for c in rest:
            print(f"  alt: {c['title'][:60]} — {money(c['landed_usd'])} "
                  f"[{c['source']}]")


# ------------------------------------------------------------------- brief
def suggest_queries(desc, dims_mm):
    d = (desc or "").strip()
    dim_s = "x".join(str(int(x)) for x in dims_mm) + "mm" if dims_mm else ""
    base = [w for w in re.split(r"\s+", d) if w]
    core = " ".join(base[:12])
    queries = []
    if core:
        queries += [f"{core} replacement part",
                    f"{core} buy",
                    f"{core} OEM part"]
    if dim_s and core:
        queries.append(f"{core} {dim_s}")
    queries.append("3d printed replacement vs buying spare part cost")
    return queries


def build_brief(args):
    os.makedirs(SRC_DIR, exist_ok=True)
    dims = parse_dims(args.dims) if args.dims else None
    photos = []
    sheet = None
    if args.photos and os.path.isdir(args.photos):
        exts = (".jpg", ".jpeg", ".png", ".webp")
        photos = sorted(os.path.join(args.photos, f) for f in
                        os.listdir(args.photos)
                        if f.lower().endswith(exts))
        if photos:
            sheet = make_contact_sheet(photos,
                                       os.path.join(SRC_DIR, "contact_sheet.jpg"))
    brief = {
        "description": args.desc or "",
        "dims_mm": list(dims) if dims else None,
        "material": (args.material or "PLA").upper(),
        "qty": args.qty or 1,
        "country": args.country,
        "tolerance_mm": args.tolerance,
        "photos": photos,
        "contact_sheet": sheet,
        "suggested_queries": suggest_queries(args.desc, dims),
        "how_to_search": ("Run the suggested queries on Google/Bing shopping, "
                          "Amazon, eBay, AliExpress, or the device maker's "
                          "parts store; or reverse-image-search the contact "
                          "sheet with Google Lens. Paste real listings into "
                          "sourcing/candidates.json (see "
                          "sourcing/candidates.example.json), then run "
                          "--decide."),
    }
    # Optional: automatic shopping search when an API key is available.
    auto = try_auto_search(brief["suggested_queries"][:3], args.country)
    if auto:
        with open(CAND_PATH, "w") as f:
            json.dump({"candidates": auto}, f, indent=2)
        brief["auto_candidates"] = (f"{len(auto)} listings pre-filled into "
                                    "sourcing/candidates.json from an "
                                    "automatic search — REVIEW before trusting.")
    with open(BRIEF_PATH, "w") as f:
        json.dump(brief, f, indent=2)
    print(f"brief -> {BRIEF_PATH}")
    if sheet:
        print(f"contact sheet -> {sheet}")
    if auto:
        print(brief["auto_candidates"])
    else:
        print("No search API key found (SERPAPI_API_KEY / BRAVE_API_KEY).")
        print("Fill sourcing/candidates.json manually, then run --decide.")


def make_contact_sheet(photos, out_path, cols=3, thumb=480):
    try:
        from PIL import Image
    except ImportError:
        return None
    picks = photos[:6]
    thumbs = []
    for p in picks:
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((thumb, thumb))
            thumbs.append(im)
        except Exception:
            continue
    if not thumbs:
        return None
    rows = math.ceil(len(thumbs) / cols)
    w = max(t.width for t in thumbs)
    h = max(t.height for t in thumbs)
    sheet = Image.new("RGB", (cols * w, rows * h), "white")
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % cols) * w, (i // cols) * h))
    sheet.save(out_path, quality=88)
    return out_path


def try_auto_search(queries, country):
    """Best-effort automatic shopping search. Returns candidates or []."""
    key = os.environ.get("SERPAPI_API_KEY")
    if key:
        return serpapi_shopping(queries, key, country)
    key = os.environ.get("BRAVE_API_KEY")
    if key:
        return brave_search(queries, key)
    return []


def serpapi_shopping(queries, key, country):
    out = []
    for q in queries:
        params = {"engine": "google_shopping", "q": q,
                  "api_key": key, "gl": (country or "us").lower(), "num": 8}
        url = "https://serpapi.com/search?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                data = json.load(r)
        except Exception as e:
            print(f"  [serpapi] query failed: {e}", file=sys.stderr)
            continue
        for it in data.get("shopping_results", [])[:8]:
            price = re.findall(r"[\d.]+", it.get("price", ""))
            out.append({
                "title": it.get("title", ""),
                "url": it.get("link", "") or it.get("product_link", ""),
                "source": it.get("source", "google shopping"),
                "price_usd": float(price[0]) if price else 0.0,
                "shipping_usd": 0.0,
                "notes": "auto-filled; verify part match + shipping",
                "auto": True,
            })
    # de-dupe by url
    seen, uniq = set(), []
    for c in out:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"])
            uniq.append(c)
    return uniq[:12]


def brave_search(queries, key):
    out = []
    for q in queries:
        req = urllib.request.Request(
            "https://api.search.brave.com/res/v1/web/search?" +
            urllib.parse.urlencode({"q": q + " buy price", "count": 8}),
            headers={"X-Subscription-Token": key})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
        except Exception as e:
            print(f"  [brave] query failed: {e}", file=sys.stderr)
            continue
        for it in data.get("web", {}).get("results", [])[:8]:
            out.append({"title": it.get("title", ""),
                        "url": it.get("url", ""), "source": "brave web",
                        "price_usd": 0.0, "shipping_usd": 0.0,
                        "notes": "auto-filled; price unknown, verify",
                        "auto": True})
    seen, uniq = set(), []
    for c in out:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"])
            uniq.append(c)
    return uniq[:12]


# -------------------------------------------------------------------- demo
def run_demo():
    print("Scenario A — $1.50 generic clip 4-pack vs printing it")
    d = decide(
        [{"title": "Dishwasher rack wheel 4-pack, generic (Amazon)", "url": "",
          "source": "amazon", "price_usd": 1.50, "shipping_usd": 0.0,
          "landed_usd": 1.50, "lead_time_days": 2, "oem": False,
          "notes": "", "auto": False}],
        estimate_print_cost((48, 32, 20), "PETG", qty=4),
        None, 4)
    print_report(d)
    assert d["decision"] == "BUY", d["decision"]

    print("\nScenario B — obsolete $45 gear, no stock vs printing it")
    d = decide(
        [{"title": "Vintage mixer gear (eBay, used)", "url": "",
          "source": "ebay", "price_usd": 45.0, "shipping_usd": 8.0,
          "landed_usd": 53.0, "lead_time_days": 9, "oem": True,
          "notes": "", "auto": False}],
        estimate_print_cost((60, 60, 18), "ASA", qty=1),
        None, 1)
    print_report(d)
    assert d["decision"] == "PRINT", d["decision"]

    print("\nScenario C — nothing buyable found")
    d = decide([], estimate_print_cost((50, 30, 12), "PLA", qty=1), None, 1)
    print_report(d)
    assert d["decision"] == "PRINT", d["decision"]

    print("\nScenario D — tight tolerance forces BUY even though print is cheaper")
    d = decide(
        [{"title": "OEM bearing bushing", "url": "", "source": "oem",
          "price_usd": 12.0, "shipping_usd": 5.0, "landed_usd": 17.0,
          "lead_time_days": 5, "oem": True, "notes": "", "auto": False}],
        estimate_print_cost((25, 25, 15), "PLA", qty=1),
        tolerance_mm=0.1, qty=1)
    print_report(d)
    assert d["decision"] == "BUY", d["decision"]
    print("\nAll demo scenarios decided as expected.")


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Stage 0: buy-vs-print decision")
    ap.add_argument("--brief", action="store_true",
                    help="build sourcing/brief.json from photos+description")
    ap.add_argument("--decide", action="store_true",
                    help="compare candidates vs print cost, write decision")
    ap.add_argument("--demo", action="store_true",
                    help="verify decision logic on synthetic scenarios")
    ap.add_argument("--photos", default="images",
                    help="dir with the user's part photos")
    ap.add_argument("--desc", default="",
                    help="what the part is, incl. device/appliance model")
    ap.add_argument("--dims", default="",
                    help="bounding box, e.g. 48x32x20mm (measure with calipers)")
    ap.add_argument("--material", default=None,
                    help="intended filament: PLA PETG ABS ASA TPU NYLON PC")
    ap.add_argument("--qty", type=int, default=None)
    ap.add_argument("--country", default="us")
    ap.add_argument("--tolerance", type=float, default=None,
                    help="required fit tolerance in mm, if any")
    ap.add_argument("--labor-rate", type=float, default=0.0,
                    help="$/hour for your time; 0 = hobby time is free")
    ap.add_argument("--fill", type=float, default=None,
                    help="override bbox fill factor (default 0.35)")
    args = ap.parse_args()

    if args.demo:
        run_demo()
        return 0
    if args.brief:
        build_brief(args)
        return 0
    if args.decide:
        cands = load_candidates()
        if cands is None:
            print("sourcing/candidates.json not found.")
            print("Run --brief first, then fill in candidates "
                  "(see sourcing/candidates.example.json).")
            return 2
        dims = parse_dims(args.dims) if args.dims else None
        brief = {}
        if os.path.exists(BRIEF_PATH):
            brief = json.load(open(BRIEF_PATH))
            if dims is None and brief.get("dims_mm"):
                dims = tuple(brief["dims_mm"])
        if dims is None:
            print("Need --dims (e.g. 48x32x20mm) to estimate print cost.")
            return 2
        mat = (args.material or brief.get("material") or "PLA").upper()
        qty = args.qty or brief.get("qty") or 1
        cost = estimate_print_cost(dims, mat, qty, args.labor_rate,
                                   args.fill)
        d = decide(cands, cost, args.tolerance or brief.get("tolerance_mm"),
                   qty)
        os.makedirs(SRC_DIR, exist_ok=True)
        with open(DECISION_PATH, "w") as f:
            json.dump(d, f, indent=2)
        print_report(d)
        print(f"\ndecision -> {DECISION_PATH}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
