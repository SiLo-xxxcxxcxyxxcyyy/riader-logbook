#!/usr/bin/env python3
"""Sestaví arc-helper: načte komunitní data (RaidTheory/arcraiders-data),
zmenší mapy a vloží vše do jediného souboru dist/index.html.

Použití:  python3 build.py /cesta/k/arcraiders-data
"""
import base64, glob, io, json, os, sys
from PIL import Image

SRC = sys.argv[1] if len(sys.argv) > 1 else "arcraiders-data"
HERE = os.path.dirname(os.path.abspath(__file__))
Image.MAX_IMAGE_PIXELS = None

def load(p): return json.load(open(os.path.join(SRC, p), encoding="utf-8"))
def en(x): return x.get("en") if isinstance(x, dict) else x
def reqs(lst): return {r["itemId"]: r["quantity"] for r in (lst or [])}

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

quests = {}
for f in glob.glob(os.path.join(SRC, "quests", "*.json")):
    q = json.load(open(f, encoding="utf-8"))
    quests[q["id"]] = {k: v for k, v in {
        "n": en(q["name"]), "tr": q.get("trader"), "m": q.get("map"),
        "o": [en(o) for o in q.get("objectives", [])],
        "o1": [en(o) for o in q.get("objectivesOneRound", [])] if isinstance(q.get("objectivesOneRound"), list) else None,
        "req": reqs(q.get("requiredItemIds")), "rw": reqs(q.get("rewardItemIds")),
        "pre": q.get("previousQuestIds"), "nx": q.get("nextQuestIds"),
        "oth": q.get("otherRequirements"),
    }.items() if v not in (None, {}, [])}

hideout = {}
for f in glob.glob(os.path.join(SRC, "hideout", "*.json")):
    h = json.load(open(f, encoding="utf-8"))
    if h.get("levels"):
        hideout[h["id"]] = {"n": en(h["name"]), "max": h["maxLevel"],
                            "lv": {l["level"]: reqs(l.get("requirementItemIds")) for l in h["levels"]}}

projects = {}
for p in load("projects.json"):
    if p.get("disabled"): continue
    projects[p["id"]] = {"n": en(p["name"]), "ph": [
        {"n": en(ph.get("name")), "req": reqs(ph.get("requirementItemIds"))} for ph in p.get("phases", [])]}

bots = [{"id": b["id"], "n": b["name"], "maps": b.get("maps", []), "drops": b.get("drops", []),
         "threat": b.get("threat")} for b in load("bots.json")]
trades = [{"tr": t["trader"], "it": t["itemId"], "q": t["quantity"], "c": t["cost"]} for t in load("trades.json")]
maps = {m["id"]: en(m["name"]) for m in load("maps.json")}

# --- mapy: zmenšit na 1600px a vložit jako JPEG ---
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
for mid, rel in MAPFILES.items():
    p = os.path.join(SRC, rel)
    if not os.path.exists(p): continue
    im = Image.open(p).convert("RGB"); im.thumbnail((1600, 1600))
    b = io.BytesIO(); im.save(b, "JPEG", quality=72, optimize=True)
    mapimg[mid] = "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()

data = {"items": items, "quests": quests, "hideout": hideout, "projects": projects,
        "bots": bots, "trades": trades, "maps": maps}
tpl = open(os.path.join(HERE, "web", "app.html"), encoding="utf-8").read()
out = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":"))) \
         .replace("/*__MAPS__*/null", json.dumps(mapimg))
os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
open(os.path.join(HERE, "dist", "artifact.html"), "w", encoding="utf-8").write(out)
local = ('<!doctype html><html lang="cs"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>'
         + out + '</body></html>')
open(os.path.join(HERE, "dist", "index.html"), "w", encoding="utf-8").write(local)
print(f"items={len(items)} quests={len(quests)} hideout={len(hideout)} projects={len(projects)} "
      f"maps={len(mapimg)} size={len(out)//1024} KB")

# --- assets for the desktop overlay's screen scanner ---
import numpy as np
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
    arr = np.asarray(bg.convert("RGB").resize((REF, REF), Image.Resampling.LANCZOS))[:, :, ::-1]  # BGR like OpenCV
    ids.append(iid); imgs.append(arr)
np.savez_compressed(os.path.join(ASSETS, "refs.npz"), ids=np.array(ids), imgs=np.stack(imgs))
print(f"scanner refs: {len(ids)} item icons")
