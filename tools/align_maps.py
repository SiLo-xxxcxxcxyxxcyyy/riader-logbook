#!/usr/bin/env python3
"""Align MetaForge marker coordinates to the labelled map images on the ARC Raiders Wiki.

For each map it downloads the labelled map image, reads the place names on it with OCR,
pairs them with MetaForge's named markers (extracts, hatches, key rooms, camps), and fits a
scale + offset with RANSAC. The result goes to sources/map_align.json, which build.py uses
to place markers on the real map image. Re-run when the wiki or MetaForge changes a map.

Usage: python3 tools/align_maps.py [--cache data/cache]
"""
import argparse, itertools, json, os, re, sys, urllib.parse, urllib.request
import cv2, numpy as np
from rapidfuzz import fuzz

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "RaiderLogbook-build/2.0 (personal game helper)"}
MAPS = {  # our map id: (MetaForge map id, wiki file, floor filter)
    "dam_battlegrounds": ("dam", "Dam_Battlegrounds_Map.jpg", 0),
    "the_spaceport": ("spaceport", "Acerra_Spaceport_Map.jpg", 0),
    "buried_city": ("buried-city", "Buried_City_Map.jpg", 0),
    "the_blue_gate": ("blue-gate", "Blue_Gate_Map.jpg", 0),
    "riven_tides": ("riven-tides", "Riven_Tides_Map.jpg", 0),
    "stella_montis_upper": ("stella-montis", "Stella_Montis_Upper_Level_Map.jpg", 0),
    "stella_montis_lower": ("stella-montis", "Stella_Montis_Lower_Level_Map.jpg", 0),
}


def http(url, binary=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        b = r.read()
    return b if binary else b.decode()


def wiki_file(name):
    q = urllib.parse.urlencode({"action": "query", "titles": "File:" + name, "prop": "imageinfo",
                                "iiprop": "url|size|extmetadata", "format": "json"})
    p = list(json.loads(http("https://arcraiders.wiki/w/api.php?" + q))["query"]["pages"].values())[0]
    ii = p["imageinfo"][0]
    md = ii.get("extmetadata", {})
    clean = lambda k: re.sub(r"<[^>]+>", "", md.get(k, {}).get("value", "")).strip()
    return ii["url"], ii["width"], ii["height"], {"artist": clean("Artist"), "license": clean("LicenseShortName"),
                                                 "page": ii.get("descriptionurl", "")}


def ocr_labels(img):
    from rapidocr_onnxruntime import RapidOCR
    o, out, (H, W), T = RapidOCR(), [], img.shape[:2], 1600
    for y in range(0, H, T - 200):
        for x in range(0, W, T - 200):
            res, _ = o(img[y:y + T, x:x + T])
            for box, t, c in res or []:
                out.append([t, sum(p[0] for p in box) / 4 + x, sum(p[1] for p in box) / 4 + y])
    return out


def n(s): return re.sub(r"[^a-z]", "", s.lower())


def fit(labels, markers):
    named = [a for a in markers if a["category"] == "locations" and (a.get("instanceName") or "").strip()]
    pairs = []
    for a in named:
        mn = n(a["instanceName"])
        for t, x, y in labels:
            ln = n(t)
            if len(ln) < 6:
                continue
            sc = max(fuzz.ratio(mn, ln), fuzz.partial_ratio(ln, mn) if len(ln) >= 10 else 0)
            if sc >= 88:
                pairs.append((a["lng"], a["lat"], x, y))
    P = np.array(pairs, float)
    best = (0, None)
    for i, j in itertools.combinations(range(len(P)), 2):
        (u1, v1, x1, y1), (u2, v2, x2, y2) = P[i], P[j]
        dm = np.hypot(u2 - u1, v2 - v1)
        if dm < 300:
            continue
        s = np.hypot(x2 - x1, y2 - y1) / dm
        if not .15 < s < 3:
            continue
        tx, ty = x1 - s * u1, y1 - s * v1
        inl = np.hypot(P[:, 0] * s + tx - P[:, 2], P[:, 1] * s + ty - P[:, 3]) < 130
        if inl.sum() > best[0]:
            best = (int(inl.sum()), inl)
    if best[1] is None:
        return None
    Q = P[best[1]]
    A = np.zeros((2 * len(Q), 3)); b = np.zeros(2 * len(Q))
    A[0::2, 0] = Q[:, 0]; A[0::2, 1] = 1; b[0::2] = Q[:, 2]
    A[1::2, 0] = Q[:, 1]; A[1::2, 2] = 1; b[1::2] = Q[:, 3]
    s, tx, ty = np.linalg.lstsq(A, b, rcond=None)[0]
    res = np.hypot(Q[:, 0] * s + tx - Q[:, 2], Q[:, 1] * s + ty - Q[:, 3])
    return {"s": float(s), "tx": float(tx), "ty": float(ty), "matches": len(Q), "candidates": len(P),
            "median_error_px": round(float(np.median(res)), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(HERE, "data", "cache"))
    ap.add_argument("--debug", help="folder for overlay previews")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    out = {}
    for mid, (mf, fname, _) in MAPS.items():
        url, W, H, meta = wiki_file(fname)
        path = os.path.join(a.cache, "wikimap_" + fname)
        if not os.path.exists(path):
            open(path, "wb").write(http(url, binary=True))
        mk_path = os.path.join(a.cache, f"metaforge_markers_{mf}.json")
        markers = json.load(open(mk_path)) if os.path.exists(mk_path) else json.loads(http(
            f"https://metaforge.app/api/game-map-data?tableID=arc_map_data&mapID={mf}"))["allData"]
        img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
        r = fit(ocr_labels(img), markers)
        print(mid, fname, r)
        if not r:
            continue
        out[mid] = {"file": fname, "url": url, "w": W, "h": H, **meta, **r}
        if a.debug:
            os.makedirs(a.debug, exist_ok=True)
            for m in markers:
                if m["category"] in ("containers", "locations"):
                    x, y = int(m["lng"] * r["s"] + r["tx"]), int(m["lat"] * r["s"] + r["ty"])
                    cv2.circle(img, (x, y), 6 if m["category"] == "locations" else 3,
                               (60, 60, 255) if m["category"] == "locations" else (0, 220, 255), -1)
            cv2.imwrite(os.path.join(a.debug, mid + ".jpg"), cv2.resize(img, None, fx=1200 / W, fy=1200 / W))
    json.dump(out, open(os.path.join(HERE, "sources", "map_align.json"), "w"), indent=1)
    print("wrote sources/map_align.json")


if __name__ == "__main__":
    sys.exit(main())
