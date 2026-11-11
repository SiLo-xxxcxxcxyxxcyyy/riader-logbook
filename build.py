#!/usr/bin/env python3
"""Build Raider Logbook from several community sources into one self-contained page.

Sources (downloaded once into a cache, re-fetched with --refresh):
  * RaidTheory/arcraiders-data (MIT, git clone): recipes, recycling, ARC drops, trades, projects,
    map images and the in-game item icons the screen scanner matches against.
  * MetaForge API (metaforge.app): current items, quests, workshop levels and farming tips.
    This is the most up-to-date source and covers Frozen Trail (Pendola Pass).
  * ARC Raiders Wiki (arcraiders.wiki, CC BY-SA 4.0): quest chains, quest maps,
    the 2.0 skill list and the Pendola Pass map.
  * MetaForge map markers: containers, nature spawns, caches, key rooms, extracts and quest spots.
  * Arc Raiders Hub (arcraidershub.com): build orders in sources/skill_builds.json and guide links in
    sources/hub_guides.json, both maintained by hand (the site does not allow scraping).

Usage:  python3 build.py /path/to/arcraiders-data [--refresh] [--offline]
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import html
import io
import json
import os
import re
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
Image.MAX_IMAGE_PIXELS = None
UA = {"User-Agent": "RaiderLogbook-build/2.0 (personal game helper)"}

ap = argparse.ArgumentParser()
ap.add_argument("src", nargs="?", default="data/arcraiders-data")
ap.add_argument("--cache", default=os.path.join(HERE, "data", "cache"))
ap.add_argument("--refresh", action="store_true", help="download the web sources again")
ap.add_argument("--offline", action="store_true", help="use only cached web sources")
A = ap.parse_args()
SRC = A.src
os.makedirs(A.cache, exist_ok=True)


# ---------------------------------------------------------------- helpers
def http(url, binary=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
        b = r.read()
    return b if binary else b.decode("utf-8")


def cached(name, fetch, binary=False):
    path = os.path.join(A.cache, name)
    if os.path.exists(path) and not A.refresh:
        return open(path, "rb").read() if binary else json.load(open(path, encoding="utf-8"))
    if A.offline:
        print(f"  ! {name} not cached and --offline given, skipping")
        return None
    try:
        data = fetch()
    except Exception as e:  # keep building with what we have
        print(f"  ! could not fetch {name}: {e}")
        if os.path.exists(path):
            return open(path, "rb").read() if binary else json.load(open(path, encoding="utf-8"))
        return None
    if binary:
        open(path, "wb").write(data)
    else:
        json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return data


def load(p): return json.load(open(os.path.join(SRC, p), encoding="utf-8"))
def en(x): return x.get("en") if isinstance(x, dict) else x
def reqs(lst): return {r["itemId"]: r["quantity"] for r in (lst or [])}
def norm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())
def uid(s): return (s or "").replace("-", "_")


def strip_html(s, limit=700):
    if not s:
        return None
    s = re.sub(r"</(p|li|h\d)>", "\n", s)
    s = re.sub(r"<li[^>]*>", "• ", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()
    return (s[:limit].rsplit(" ", 1)[0] + "…") if len(s) > limit else s


# ---------------------------------------------------------------- 1. RaidTheory base
items = {}
for f in glob.glob(os.path.join(SRC, "items", "*.json")):
    i = json.load(open(f, encoding="utf-8"))
    items[i["id"]] = {k: v for k, v in {
        "n": en(i["name"]), "t": i.get("type"), "r": i.get("rarity"),
        "v": i.get("value", 0), "st": i.get("stackSize"), "w": i.get("weightKg"),
        "f": i.get("foundIn"), "rc": i.get("recyclesInto"), "sv": i.get("salvagesInto"),
        "rp": i.get("recipe"), "bl": i.get("blueprintLocked"), "q": i.get("questItem"),
        "img": i.get("imageFilename"),
        "vd": [{"tr": v.get("trader"), "c": v.get("cost")} for v in i.get("vendors", [])] or None,
    }.items() if v not in (None, {}, [])}

rt_quests = {}
for f in glob.glob(os.path.join(SRC, "quests", "*.json")):
    q = json.load(open(f, encoding="utf-8"))
    rt_quests[q["id"]] = q

hideout = {}
for f in glob.glob(os.path.join(SRC, "hideout", "*.json")):
    h = json.load(open(f, encoding="utf-8"))
    if h.get("levels"):
        hideout[h["id"]] = {"n": en(h["name"]), "max": h["maxLevel"],
                            "lv": {l["level"]: reqs(l.get("requirementItemIds")) for l in h["levels"]}}

projects = {}
for p in load("projects.json"):
    if p.get("disabled"):
        continue
    projects[p["id"]] = {"n": en(p["name"]), "ph": [
        {"n": en(ph.get("name")), "req": reqs(ph.get("requirementItemIds"))} for ph in p.get("phases", [])]}

bots = [{"id": b["id"], "n": b["name"], "maps": b.get("maps", []), "drops": b.get("drops", []),
         "threat": b.get("threat")} for b in load("bots.json")]
trades = [{"tr": t["trader"], "it": t["itemId"], "q": t["quantity"], "c": t["cost"]} for t in load("trades.json")]
maps = {m["id"]: en(m["name"]) for m in load("maps.json")}
maps["pendola_pass"] = "Pendola Pass"
by_name = {norm(v["n"]): k for k, v in items.items()}

# ---------------------------------------------------------------- 2. MetaForge (current data)
MF = "https://metaforge.app/api/arc-raiders/"


def mf_all(endpoint):
    out, page = [], 1
    while True:
        d = json.loads(http(f"{MF}{endpoint}?limit=100&page={page}"))
        out += d["data"]
        if not d.get("pagination", {}).get("hasNextPage"):
            return out
        page += 1


print("MetaForge …")
mf_items = cached("metaforge_items.json", lambda: mf_all("items")) or []
mf_quests = cached("metaforge_quests.json", lambda: mf_all("quests")) or []
mf_hideout = cached("metaforge_hideout.json", lambda: json.loads(http(MF + "hideout?limit=100"))) or []

SKIP_TYPES = {"Cosmetic", "Outfits", "Furniture", "Design", "Stencil"}
mf_id = {}                                   # MetaForge id -> our id


def resolve(mid, name=None):
    if mid in mf_id:
        return mf_id[mid]
    u = uid(mid)
    if u in items:
        return u
    return by_name.get(norm(name)) or u


added = 0
for it in mf_items:
    if it.get("item_type") in SKIP_TYPES:
        continue
    iid = resolve(it["id"], it.get("name"))
    mf_id[it["id"]] = iid
    sb = it.get("stat_block") or {}
    cur = items.setdefault(iid, {})
    if not cur:
        added += 1
        cur.update({"n": it["name"], "new": 1})
    cur.setdefault("t", it.get("item_type"))
    if it.get("rarity"): cur["r"] = it["rarity"]
    if it.get("value") is not None: cur["v"] = it["value"]
    if sb.get("stackSize"): cur["st"] = sb["stackSize"]
    if sb.get("weight"): cur["w"] = sb["weight"]
    if it.get("loot_area"): cur["f"] = it["loot_area"]
    tip = strip_html(it.get("article"))
    if tip: cur["tip"] = tip
    if it.get("guide_url"): cur["gu"] = urllib.parse.urljoin("https://metaforge.app", it["guide_url"])
by_name = {norm(v["n"]): k for k, v in items.items()}
print(f"  items: {len(items)} ({added} new from MetaForge)")


def mf_req(lst):
    out = {}
    for r in lst or []:
        mid = r.get("item_id") or r.get("id")
        name = (r.get("item") or {}).get("name")
        out[resolve(mid, name)] = out.get(resolve(mid, name), 0) + r.get("quantity", 1)
    return out


if mf_hideout:
    hideout = {}
    for h in mf_hideout:
        lv = {int(k): mf_req(v.get("requiredItems")) for k, v in (h.get("levels") or {}).items()}
        hideout[uid(h["id"])] = {"n": h["name"], "max": h.get("maxLevel", max(lv) if lv else 0), "lv": lv}

# ---------------------------------------------------------------- 3. Wiki: quest chains, skills, Pendola map
WIKI = "https://arcraiders.wiki/w/api.php"


def wiki(**p):
    p["format"] = "json"
    return json.loads(http(WIKI + "?" + urllib.parse.urlencode(p)))


def wiki_quests():
    titles, cont = [], {}
    while True:
        d = wiki(action="query", list="categorymembers", cmtitle="Category:Quests", cmlimit=500, cmnamespace=0, **cont)
        titles += [m["title"] for m in d["query"]["categorymembers"]]
        if "continue" not in d:
            break
        cont = {"cmcontinue": d["continue"]["cmcontinue"]}
    pages = {}
    for i in range(0, len(titles), 50):
        d = wiki(action="query", prop="revisions", rvprop="content", rvslots="main", titles="|".join(titles[i:i + 50]))
        for p in d["query"]["pages"].values():
            if "revisions" in p:
                pages[p["title"]] = p["revisions"][0]["slots"]["main"]["*"]
    return pages


print("Wiki …")
wq = cached("wiki_quests.json", wiki_quests) or {}
skills_wt = cached("wiki_skills.json", lambda: wiki(action="parse", page="Skills", prop="wikitext")["parse"]["wikitext"]["*"])
pendola_png = cached("pendola_pass_map.png", lambda: http(
    "https://arcraiders.wiki/w/images/a/a0/Pendola_Pass_Map_Blank.png", binary=True), binary=True)

MAP_ALIASES = {"dambattlegrounds": "dam_battlegrounds", "spaceport": "the_spaceport", "thespaceport": "the_spaceport",
               "buriedcity": "buried_city", "thebluegate": "the_blue_gate", "bluegate": "the_blue_gate",
               "stellamontis": "stella_montis", "pendolapass": "pendola_pass", "riventides": "riven_tides"}


def infobox(text, key):
    m = re.search(r"\n\|\s*" + key + r"\s*=\s*([^\n]*)", text)
    return m.group(1).strip() if m else ""


wiki_by_name = {}
for title, text in wq.items():
    wiki_by_name[norm(infobox(text, "name") or title)] = text
    wiki_by_name.setdefault(norm(title), text)

quests = {}
qname_to_id = {}
for q in mf_quests:
    qid = uid(q["id"])
    qname_to_id[norm(q["name"])] = qid
for q in mf_quests:
    qid = uid(q["id"])
    rt = rt_quests.get(qid, {})
    w = wiki_by_name.get(norm(q["name"]), "")
    loc = [MAP_ALIASES.get(norm(x)) for x in re.split(r";|,", infobox(w, "location"))]
    loc = sorted({x for x in loc if x}) or ([m for m in rt.get("map", []) if m] if rt else [])
    nx = [qname_to_id.get(norm(x)) for x in re.split(r";", infobox(w, "next"))]
    quests[qid] = {k: v for k, v in {
        "n": q["name"], "tr": q.get("trader_name") or rt.get("trader"), "m": loc or None,
        "o": q.get("objectives") or [en(o) for o in rt.get("objectives", [])],
        "o1": [en(o) for o in rt.get("objectivesOneRound", [])] if isinstance(rt.get("objectivesOneRound"), list) else None,
        "req": mf_req(q.get("required_items")), "rw": mf_req(q.get("rewards")),
        "nx": sorted({x for x in nx if x}) or None, "oth": rt.get("otherRequirements"), "xp": q.get("xp") or None,
        "new": 1 if "Frozen Trail" in infobox(w, "introduced") or qid not in rt_quests else None,
    }.items() if v not in (None, {}, [])}
for qid, q in quests.items():                        # previous = inverse of next
    for n in q.get("nx", []):
        if n in quests:
            quests[n].setdefault("pre", []).append(qid)
missing_chain = [q["n"] for q in quests.values() if "pre" not in q and "nx" not in q]
print(f"  quests: {len(quests)} ({sum(1 for q in quests.values() if q.get('new'))} new), "
      f"{sum(1 for q in quests.values() if 'pre' in q)} with a known previous quest")


def parse_skills(t):
    t = t[t.find("<tabber>"):t.find("</tabber>")]

    def clean(s):
        s = re.sub(r'style="[^"]*"?\s*;?\s*\|', "", s); s = re.sub(r"\[\[File:[^\]]*\]\]", "", s)
        s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s); s = re.sub(r"'''?", "", s)
        s = re.sub(r"<[^>]+>", "", s)
        return re.sub(r"\s+", " ", s).strip().strip("|").strip()
    out = []
    for part in re.split(r"\n\|-\|", t)[1:]:
        branch = part.split("=")[0].strip().upper()
        for r in part.split("\n|-")[1:]:
            cells = []
            for ln in r.strip("\n").split("\n"):
                if ln.startswith("|}"):
                    break
                if ln.startswith("!"):
                    cells = None
                    break
                if ln.startswith("|"):
                    cells += ln[1:].split("||") if "||" in ln else [ln[1:]]
                elif cells:
                    cells[-1] += " " + ln
            if not cells or len(cells) < 5:
                continue
            c = [clean(x) for x in cells] + [""] * 6
            if c[1]:
                out.append({"b": branch, "n": c[1], "d": c[2], "x": c[3], "max": c[4], "type": c[5]})
    return out


# ---------------------------------------------------------------- 4. Skill builds (hand-maintained file)
# Arc Raiders Hub's terms forbid automated scraping, so the build orders live in sources/skill_builds.json
# and are updated by hand from https://arcraidershub.com/guides/recommended-skill-tree-guide
skill_rows = parse_skills(skills_wt) if skills_wt else []
builds = json.load(open(os.path.join(HERE, "sources", "skill_builds.json"), encoding="utf-8"))["builds"]
seen_max = {}
for bd in builds:
    for ph in bd["phases"]:
        for n, to in ph["steps"]:
            seen_max[norm(n)] = max(seen_max.get(norm(n), 0), to)
for sr in skill_rows:
    if not sr["max"].isdigit() and not sr["max"].startswith("Option") and norm(sr["n"]) in seen_max:
        sr["max"] = str(seen_max[norm(sr["n"])])
print(f"  skills: {len(skill_rows)}, builds: {len(builds)} ({', '.join(b['n'] for b in builds)})")
guides = json.load(open(os.path.join(HERE, "sources", "hub_guides.json"), encoding="utf-8"))["guides"]

# ---------------------------------------------------------------- 5. MetaForge map markers + event schedule
MF_MAPS = {"dam": "dam_battlegrounds", "spaceport": "the_spaceport", "buried-city": "buried_city",
           "blue-gate": "the_blue_gate", "stella-montis": "stella_montis", "riven-tides": "riven_tides",
           "pendola-pass": "pendola_pass"}
print("Map markers …")
markers = {}
for mf_map, mid in MF_MAPS.items():
    raw = cached(f"metaforge_markers_{mf_map}.json", lambda m=mf_map: json.loads(http(
        f"https://metaforge.app/api/game-map-data?tableID=arc_map_data&mapID={m}"))["allData"])
    if not raw:
        continue
    subs, areas, pts = [], [], []
    for a in raw:
        cat, sub = a.get("category"), (a.get("subcategory") or "").strip()
        if cat not in ("containers", "nature", "arc", "events", "locations", "quests") or sub in ("player_spawn",):
            continue
        key = f"{cat}:{sub}"
        if key not in subs:
            subs.append(key)
        la = (a.get("lootAreas") or "").strip()
        if la and la not in areas:
            areas.append(la)
        label = (a.get("instanceName") or "").strip()
        if cat not in ("locations", "quests"):
            label = ""
        z = a.get("zlayers")
        pts.append([round(a["lng"]), round(a["lat"]), subs.index(key), areas.index(la) if la else -1,
                    label, 1 if a.get("behindLockedDoor") else 0, z if z in (1, 2) else 0])
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    markers[mid] = {"box": [min(xs), min(ys), max(xs), max(ys)], "subs": subs, "areas": areas, "pts": pts}
print(f"  markers: {sum(len(m['pts']) for m in markers.values())} on {len(markers)} maps")
events = cached("metaforge_events.json", lambda: json.loads(http(MF + "events-schedule"))["data"]) or []
EVMAP = {"dam": "dam_battlegrounds", "spaceport": "the_spaceport", "buriedcity": "buried_city", "bluegate": "the_blue_gate",
         "stellamontis": "stella_montis", "riventides": "riven_tides", "pendolapass": "pendola_pass"}
events = [{"n": e["name"], "m": EVMAP.get(norm(e["map"]), e["map"]), "s": e["startTime"], "e": e["endTime"]} for e in events]

skills = []
for s in skill_rows:
    big = s["max"].startswith("Option") or "Large Node" in s["type"]
    gate = 0
    if big:
        gate = 30 if "30" in s["type"] else 10
    mx = 1 if big else int(s["max"]) if s["max"].isdigit() else 5
    skills.append({"id": norm(s["n"]), "n": s["n"], "b": s["b"], "d": s["d"], "x": s["x"] or None,
                   "max": mx, "root": "Initial" in s["type"] or None, "gate": gate or None,
                   "grp": ("L2" if gate == 30 else "L1") if big else None})

# ---------------------------------------------------------------- maps
MAPFILES = {
    "buried_city": "images/maps/legacy/buried_city.png",
    "dam_battlegrounds": "images/maps/legacy/dam_battlegrounds.png",
    "the_blue_gate": "images/maps/legacy/the_blue_gate.png",
    "the_spaceport": "images/maps/legacy/the_spaceport.png",
    "stella_montis_upper": "images/maps/stella_montis_upper.png",
    "stella_montis_lower": "images/maps/stella_montis_lower.png",
    "riven_tides": "images/maps/riven-tides/riven-tides.png",
}
mapimg = {}


def embed(im):
    im = im.convert("RGB"); im.thumbnail((1600, 1600))
    b = io.BytesIO(); im.save(b, "JPEG", quality=72, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


for mid, rel in MAPFILES.items():
    p = os.path.join(SRC, rel)
    if os.path.exists(p):
        mapimg[mid] = embed(Image.open(p))
if pendola_png:
    mapimg["pendola_pass"] = embed(Image.open(io.BytesIO(pendola_png)))

# ---------------------------------------------------------------- write the page
meta = {"built": dt.date.today().isoformat(),
        "sources": [
            {"n": "MetaForge", "u": "https://metaforge.app/arc-raiders", "what": "items, quests, workshop, farming tips, map markers, event schedule"},
            {"n": "ARC Raiders Wiki", "u": "https://arcraiders.wiki", "what": "quest chains, skill list, Pendola Pass map (CC BY-SA 4.0)"},
            {"n": "Arc Raiders Hub", "u": "https://arcraidershub.com/guides", "what": "skill build order (copied by hand) and guide links"},
            {"n": "RaidTheory arcraiders-data", "u": "https://github.com/RaidTheory/arcraiders-data", "what": "recipes, recycling, ARC drops, trades, projects, maps (MIT)"},
        ]}
data = {"items": items, "quests": quests, "hideout": hideout, "projects": projects, "bots": bots,
        "trades": trades, "maps": maps, "skills": skills, "builds": builds, "meta": meta,
        "markers": markers, "events": events, "guides": guides}
tpl = open(os.path.join(HERE, "web", "app.html"), encoding="utf-8").read()
out = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":"))) \
         .replace("/*__MAPS__*/null", json.dumps(mapimg))
os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
open(os.path.join(HERE, "dist", "artifact.html"), "w", encoding="utf-8").write(out)
local = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>'
         + out + '</body></html>')
open(os.path.join(HERE, "dist", "index.html"), "w", encoding="utf-8").write(local)
print(f"page: maps={len(mapimg)} size={len(out) // 1024} KB")

# ---------------------------------------------------------------- assets for the desktop overlay's scanner
ASSETS = os.path.join(HERE, "overlay", "assets")
os.makedirs(ASSETS, exist_ok=True)
json.dump({"quests": {k: {"n": v["n"], "pre": v.get("pre", [])} for k, v in quests.items()},
           "items": {k: {"n": v["n"], "r": v.get("r"), "st": v.get("st", 1)} for k, v in items.items()}},
          open(os.path.join(ASSETS, "game.json"), "w", encoding="utf-8"), ensure_ascii=False)
REF = 64
ids, imgs = [], []
icon_dir = os.path.join(SRC, "images", "items_ingame")
for fn in sorted(os.listdir(icon_dir)):
    iid = os.path.splitext(fn)[0]
    if iid not in items:
        continue
    im = Image.open(os.path.join(icon_dir, fn)).convert("RGBA")
    bg = Image.new("RGBA", im.size, (20, 22, 26, 255)); bg.alpha_composite(im)
    arr = np.asarray(bg.convert("RGB").resize((REF, REF), Image.Resampling.LANCZOS))[:, :, ::-1]
    ids.append(iid); imgs.append(arr)
np.savez_compressed(os.path.join(ASSETS, "refs.npz"), ids=np.array(ids), imgs=np.stack(imgs))
print(f"scanner refs: {len(ids)} item icons")
