#!/usr/bin/env python3
"""Build Raider Logbook from several community sources into one self-contained page.

Sources:
  * RaidTheory/arcraiders-data (MIT, git clone): recipes, recycling, ARC drops, trades, projects and
    the in-game item icons the screen scanner matches against. Read once at build time and saved as
    overlay/assets/base.json so the desktop app can rebuild without it.
  * MetaForge API (metaforge.app): current items, quests, workshop levels, farming tips, ~43,000 map
    markers and the event schedule. Attribution required; data cached locally as they ask.
  * ARC Raiders Wiki (arcraiders.wiki, CC BY-SA 4.0): quest chains and maps, quest walkthroughs
    (Guide/Notes sections), the 2.0 skill list, and the labelled map images (maps by Cartotect).
    Marker-to-image alignment comes from tools/align_maps.py (sources/map_align.json).
  * Arc Raiders Hub (arcraidershub.com): build orders in sources/skill_builds.json and guide links in
    sources/hub_guides.json, both maintained by hand (the site's terms forbid scraping).

Usage:
  python3 build.py /path/to/arcraiders-data [--refresh] [--offline]   full build (writes dist/ and overlay/assets/)
  python3 build.py --base overlay/assets/base.json --out DIR           rebuild the page from base.json (desktop app)
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import glob
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# web/ and sources/: downloaded code folder (set by the updater), else the bundle, else this checkout
RES = os.environ.get("RAIDER_RES") or getattr(sys, "_MEIPASS", HERE)
UA = {"User-Agent": "RaiderLogbook/2.0 (personal game helper)"}
HOUR = 3600
MF = "https://metaforge.app/api/arc-raiders/"
WIKI = "https://arcraiders.wiki/w/api.php"


# ================================================================ helpers
def http(url, binary=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
        b = r.read()
    return b if binary else b.decode("utf-8")


class Fetcher:
    """Downloads with a local cache. Entries younger than max_age are reused."""

    def __init__(self, cache, refresh=False, offline=False, log=print):
        self.cache, self.refresh, self.offline, self.log = cache, refresh, offline, log
        os.makedirs(cache, exist_ok=True)

    def _load(self, path, binary):
        return open(path, "rb").read() if binary else json.load(open(path, encoding="utf-8"))

    def get(self, name, fetch, binary=False, max_age=None):
        path = os.path.join(self.cache, name)
        exists = os.path.exists(path)
        fresh = exists and not self.refresh and (max_age is None or time.time() - os.path.getmtime(path) < max_age)
        if fresh or (self.offline and exists):
            return self._load(path, binary)
        if self.offline:
            self.log(f"  ! {name} not cached (offline)")
            return None
        try:
            data = fetch()
        except Exception as e:
            self.log(f"  ! could not fetch {name}: {e}")
            return self._load(path, binary) if exists else None
        if binary:
            open(path, "wb").write(data)
        else:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        return data


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


def wiki_plain(s):
    """Wikitext to short plain text."""
    s = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^/]*/>", "", s, flags=re.S)
    s = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", s)
    s = re.sub(r"\{\{Price\|([^}|]*)\|?[^}]*\}\}", r"\1", s)
    for _ in range(3):
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[https?://\S+ ([^\]]+)\]", r"\1", s)
    s = re.sub(r"'''?|<[^>]+>", "", s)
    s = re.sub(r"^\*+\s*", "• ", s, flags=re.M)
    return re.sub(r"\n{2,}", "\n", s).strip()


def wiki(F, **p):
    p["format"] = "json"
    return json.loads(http(WIKI + "?" + urllib.parse.urlencode(p)))


# ================================================================ RaidTheory base
def load_raidtheory(src):
    from PIL import Image
    def load(p): return json.load(open(os.path.join(src, p), encoding="utf-8"))
    items = {}
    for f in glob.glob(os.path.join(src, "items", "*.json")):
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
    for f in glob.glob(os.path.join(src, "quests", "*.json")):
        q = json.load(open(f, encoding="utf-8"))
        rt_quests[q["id"]] = {k: v for k, v in {
            "map": q.get("map"), "trader": q.get("trader"),
            "o": [en(o) for o in q.get("objectives", [])],
            "o1": [en(o) for o in q.get("objectivesOneRound", [])] if isinstance(q.get("objectivesOneRound"), list) else None,
            "oth": q.get("otherRequirements")}.items() if v}
    hideout = {}
    for f in glob.glob(os.path.join(src, "hideout", "*.json")):
        h = json.load(open(f, encoding="utf-8"))
        if h.get("levels"):
            hideout[h["id"]] = {"n": en(h["name"]), "max": h["maxLevel"],
                                "lv": {l["level"]: reqs(l.get("requirementItemIds")) for l in h["levels"]}}
    projects = {p["id"]: {"n": en(p["name"]), "ph": [{"n": en(ph.get("name")), "req": reqs(ph.get("requirementItemIds"))}
                                                     for ph in p.get("phases", [])]}
                for p in load("projects.json") if not p.get("disabled")}
    bots = [{"id": b["id"], "n": b["name"], "maps": b.get("maps", []), "drops": b.get("drops", []),
             "threat": b.get("threat")} for b in load("bots.json")]
    trades = [{"tr": t["trader"], "it": t["itemId"], "q": t["quantity"], "c": t["cost"]} for t in load("trades.json")]
    maps = {m["id"]: en(m["name"]) for m in load("maps.json")}
    maps["pendola_pass"] = "Pendola Pass"
    return {"items": items, "rt_quests": rt_quests, "hideout": hideout, "projects": projects, "bots": bots,
            "trades": trades, "maps": maps}


def aligned_maps(F, log=print, px=1800):
    """Labelled wiki map images, downscaled, with the marker transform scaled to match."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    al = json.load(open(os.path.join(RES, "sources", "map_align.json"), encoding="utf-8"))
    mapimg, align = {}, {}
    for mid, a in al.items():
        raw = F.get("wikimap_" + a["file"], lambda u=a["url"]: http(u, binary=True), binary=True, max_age=30 * 24 * HOUR)
        if not raw:
            continue
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        k = px / max(im.size)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.Resampling.LANCZOS)
        b = io.BytesIO(); im.save(b, "JPEG", quality=74, optimize=True)
        mapimg[mid] = "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
        k = im.width / a["w"]
        align[mid] = {"s": a["s"] * k, "tx": a["tx"] * k, "ty": a["ty"] * k, "w": im.width, "h": im.height,
                      "src": "https://arcraiders.wiki/wiki/File:" + a["file"]}
    log(f"  map images: {len(mapimg)} aligned ({', '.join(mapimg)})")
    return mapimg, align


# ================================================================ assemble everything
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


def infobox(text, key):
    m = re.search(r"\n\|\s*" + key + r"\s*=\s*([^\n]*)", text)
    return m.group(1).strip() if m else ""


def section(text, name):
    m = re.search(r"^==\s*" + name + r"\s*==\s*$", text, re.M)
    if not m:
        return ""
    end = re.search(r"^==[^=]", text[m.end():], re.M)
    return text[m.end():m.end() + end.start()] if end else text[m.end():]


def quest_guide(text):
    """Steps (captioned screenshots) and notes from a wiki quest page."""
    g = section(text, "Guide")
    steps = []
    for gal in re.findall(r"<gallery[^>]*>(.*?)</gallery>", g, re.S):
        for ln in gal.strip().split("\n"):
            if "|" not in ln:
                continue
            f, cap = ln.split("|", 1)
            f = re.sub(r"^(File|Image):", "", f.strip())
            cap = wiki_plain(cap)
            if f and cap:
                steps.append({"c": cap, "f": f})
    rest = wiki_plain(re.sub(r"<gallery[^>]*>.*?</gallery>", "", g, flags=re.S))
    notes = wiki_plain(section(text, "Notes")) or None
    tips = wiki_plain(section(text, "Tips")) or None
    out = {k: v for k, v in {"steps": steps, "text": rest or None, "notes": notes, "tips": tips}.items() if v}
    return out or None


def wiki_thumbs(F, files, width=640):
    def fetch():
        out = {}
        fl = sorted(files)
        for i in range(0, len(fl), 50):
            d = wiki(F, action="query", prop="imageinfo", iiprop="url", iiurlwidth=width,
                     titles="|".join("File:" + f for f in fl[i:i + 50]))
            norm_map = {n["to"]: n["from"] for n in d["query"].get("normalized", [])}
            for p in d["query"]["pages"].values():
                ii = (p.get("imageinfo") or [{}])[0]
                if ii.get("thumburl") or ii.get("url"):
                    key = norm_map.get(p["title"], p["title"]).split(":", 1)[1]
                    out[key] = ii.get("thumburl") or ii.get("url")
        return out
    return F.get("wiki_quest_thumbs.json", fetch, max_age=7 * 24 * HOUR) or {}


def assemble(base, F, log=print):
    items = copy.deepcopy(base["items"])
    rt_quests = base["rt_quests"]
    hideout = copy.deepcopy(base["hideout"])
    maps = dict(base["maps"])
    by_name = {norm(v["n"]): k for k, v in items.items()}

    # ---- MetaForge items / quests / workshop
    def mf_all(endpoint):
        out, page = [], 1
        while True:
            d = json.loads(http(f"{MF}{endpoint}?limit=100&page={page}"))
            out += d["data"]
            if not d.get("pagination", {}).get("hasNextPage"):
                return out
            page += 1

    log("MetaForge …")
    mf_items = F.get("metaforge_items.json", lambda: mf_all("items"), max_age=12 * HOUR) or []
    mf_quests = F.get("metaforge_quests.json", lambda: mf_all("quests"), max_age=12 * HOUR) or []
    mf_hideout = F.get("metaforge_hideout.json", lambda: json.loads(http(MF + "hideout?limit=100")), max_age=24 * HOUR) or []

    SKIP = {"Cosmetic", "Outfits", "Furniture", "Design", "Stencil"}
    mf_id = {}

    def resolve(mid, name=None):
        if mid in mf_id:
            return mf_id[mid]
        u = uid(mid)
        return u if u in items else (by_name.get(norm(name)) or u)

    added = 0
    for it in mf_items:
        if it.get("item_type") in SKIP:
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
    log(f"  items: {len(items)} ({added} new from MetaForge)")

    def mf_req(lst):
        out = {}
        for r in lst or []:
            k = resolve(r.get("item_id") or r.get("id"), (r.get("item") or {}).get("name"))
            out[k] = out.get(k, 0) + r.get("quantity", 1)
        return out

    if mf_hideout:
        hideout = {}
        for h in mf_hideout:
            lv = {int(k): mf_req(v.get("requiredItems")) for k, v in (h.get("levels") or {}).items()}
            hideout[uid(h["id"])] = {"n": h["name"], "max": h.get("maxLevel", max(lv) if lv else 0), "lv": lv}

    # ---- Wiki: quests (chains, maps, walkthroughs), skills
    log("Wiki …")

    def wiki_quests():
        titles, cont = [], {}
        while True:
            d = wiki(F, action="query", list="categorymembers", cmtitle="Category:Quests", cmlimit=500, cmnamespace=0, **cont)
            titles += [m["title"] for m in d["query"]["categorymembers"]]
            if "continue" not in d:
                break
            cont = {"cmcontinue": d["continue"]["cmcontinue"]}
        pages = {}
        for i in range(0, len(titles), 50):
            d = wiki(F, action="query", prop="revisions", rvprop="content", rvslots="main", titles="|".join(titles[i:i + 50]))
            for p in d["query"]["pages"].values():
                if "revisions" in p:
                    pages[p["title"]] = p["revisions"][0]["slots"]["main"]["*"]
        return pages

    wq = F.get("wiki_quests.json", wiki_quests, max_age=24 * HOUR) or {}
    skills_wt = F.get("wiki_skills.json", lambda: wiki(F, action="parse", page="Skills", prop="wikitext")["parse"]["wikitext"]["*"],
                      max_age=7 * 24 * HOUR)

    MAP_ALIASES = {"dambattlegrounds": "dam_battlegrounds", "spaceport": "the_spaceport", "thespaceport": "the_spaceport",
                   "buriedcity": "buried_city", "thebluegate": "the_blue_gate", "bluegate": "the_blue_gate",
                   "stellamontis": "stella_montis", "pendolapass": "pendola_pass", "riventides": "riven_tides"}
    wiki_by_name = {}
    for title, text in wq.items():
        wiki_by_name[norm(infobox(text, "name") or title)] = (title, text)
        wiki_by_name.setdefault(norm(title), (title, text))

    quests, qname_to_id, guide_files = {}, {}, set()
    for q in mf_quests:
        qname_to_id[norm(q["name"])] = uid(q["id"])
    import difflib
    wiki_keys = list(wiki_by_name)
    for q in mf_quests:
        qid = uid(q["id"])
        rt = rt_quests.get(qid, {})
        key = norm(q["name"])
        if key not in wiki_by_name:                       # e.g. "When Everything Falls" vs "Where Everything Falls"
            close = difflib.get_close_matches(key, wiki_keys, n=1, cutoff=0.85)
            key = close[0] if close else key
        title, w = wiki_by_name.get(key, ("", ""))
        loc = sorted({MAP_ALIASES.get(norm(x)) for x in re.split(r"[;,]", infobox(w, "location"))} - {None}) or rt.get("map")
        nx = sorted({qname_to_id.get(norm(x)) for x in infobox(w, "next").split(";")} - {None})
        gd = quest_guide(w) if w else None
        if gd:
            guide_files.update(s["f"] for s in gd.get("steps", []))
        quests[qid] = {k: v for k, v in {
            "n": q["name"], "tr": q.get("trader_name") or rt.get("trader"), "m": loc or None,
            "o": q.get("objectives") or rt.get("o"), "o1": rt.get("o1"),
            "req": mf_req(q.get("required_items")), "rw": mf_req(q.get("rewards")), "nx": nx or None,
            "oth": rt.get("oth"), "xp": q.get("xp") or None,
            "new": 1 if "Frozen Trail" in infobox(w, "introduced") or qid not in rt_quests else None,
            "hid": 1 if infobox(w, "isHidden").lower() == "true" else None,
            "g": gd, "wp": "https://arcraiders.wiki/wiki/" + urllib.parse.quote(title.replace(" ", "_")) if title else None,
            "gu": urllib.parse.urljoin("https://metaforge.app", q["guide_url"]) if q.get("guide_url") else None,
        }.items() if v not in (None, {}, [])}
    for qid, q in quests.items():
        for nq in q.get("nx", []):
            if nq in quests:
                quests[nq].setdefault("pre", []).append(qid)
    thumbs = wiki_thumbs(F, guide_files) if guide_files else {}
    for q in quests.values():
        for s in q.get("g", {}).get("steps", []):
            s["u"] = thumbs.get(s["f"]) or thumbs.get(s["f"].replace("_", " "))
    log(f"  quests: {len(quests)} ({sum(1 for q in quests.values() if q.get('new'))} new), "
        f"{sum(1 for q in quests.values() if 'g' in q)} with a walkthrough, {len(thumbs)} screenshots")

    # ---- skills + builds (hand-maintained)
    skill_rows = parse_skills(skills_wt) if skills_wt else []
    builds = json.load(open(os.path.join(RES, "sources", "skill_builds.json"), encoding="utf-8"))["builds"]
    seen_max = {}
    for bd in builds:
        for ph in bd["phases"]:
            for nm_, to in ph["steps"]:
                seen_max[norm(nm_)] = max(seen_max.get(norm(nm_), 0), to)
    skills = []
    for s in skill_rows:
        if not s["max"].isdigit() and not s["max"].startswith("Option") and norm(s["n"]) in seen_max:
            s["max"] = str(seen_max[norm(s["n"])])
        big = s["max"].startswith("Option") or "Large Node" in s["type"]
        gate = (30 if "30" in s["type"] else 10) if big else 0
        skills.append({"id": norm(s["n"]), "n": s["n"], "b": s["b"], "d": s["d"], "x": s["x"] or None,
                       "max": 1 if big else int(s["max"]) if s["max"].isdigit() else 5,
                       "root": "Initial" in s["type"] or None, "gate": gate or None,
                       "grp": ("L2" if gate == 30 else "L1") if big else None})
    guides = json.load(open(os.path.join(RES, "sources", "hub_guides.json"), encoding="utf-8"))["guides"]
    log(f"  skills: {len(skills)}, builds: {len(builds)}")

    # ---- map markers + events
    MF_MAPS = {"dam": "dam_battlegrounds", "spaceport": "the_spaceport", "buried-city": "buried_city",
               "blue-gate": "the_blue_gate", "stella-montis": "stella_montis", "riven-tides": "riven_tides",
               "pendola-pass": "pendola_pass"}
    log("Map markers …")
    markers = {}
    for mf_map, mid in MF_MAPS.items():
        raw = F.get(f"metaforge_markers_{mf_map}.json", lambda m=mf_map: json.loads(http(
            f"https://metaforge.app/api/game-map-data?tableID=arc_map_data&mapID={m}"))["allData"], max_age=24 * HOUR)
        if not raw:
            continue
        subs, areas, pts = [], [], []
        for a in raw:
            cat, sub = a.get("category"), (a.get("subcategory") or "").strip()
            if cat not in ("containers", "nature", "arc", "events", "locations", "quests") or sub == "player_spawn":
                continue
            key = f"{cat}:{sub}"
            if key not in subs:
                subs.append(key)
            la = (a.get("lootAreas") or "").strip()
            if la and la not in areas:
                areas.append(la)
            label = (a.get("instanceName") or "").strip() if cat in ("locations", "quests") else ""
            z = a.get("zlayers")
            pts.append([round(a["lng"]), round(a["lat"]), subs.index(key), areas.index(la) if la else -1,
                        label, 1 if a.get("behindLockedDoor") else 0, z if z in (1, 2) else 0])
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        markers[mid] = {"box": [min(xs), min(ys), max(xs), max(ys)], "subs": subs, "areas": areas, "pts": pts}
    log(f"  markers: {sum(len(m['pts']) for m in markers.values())} on {len(markers)} maps")
    EVMAP = {"dam": "dam_battlegrounds", "spaceport": "the_spaceport", "buriedcity": "buried_city", "bluegate": "the_blue_gate",
             "stellamontis": "stella_montis", "riventides": "riven_tides", "pendolapass": "pendola_pass"}
    ev = F.get("metaforge_events.json", lambda: json.loads(http(MF + "events-schedule"))["data"], max_age=3 * HOUR) or []
    events = [{"n": e["name"], "m": EVMAP.get(norm(e["map"]), e["map"]), "s": e["startTime"], "e": e["endTime"]} for e in ev]

    meta = {"built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
            "sources": [
                {"n": "MetaForge", "u": "https://metaforge.app/arc-raiders", "what": "items, quests, workshop, farming tips, map markers, event schedule"},
                {"n": "ARC Raiders Wiki", "u": "https://arcraiders.wiki", "what": "quest chains and walkthroughs, skill list, map images by Cartotect (CC BY-SA 4.0)"},
                {"n": "Arc Raiders Hub", "u": "https://arcraidershub.com/guides", "what": "skill build order (copied by hand) and guide links"},
                {"n": "RaidTheory arcraiders-data", "u": "https://github.com/RaidTheory/arcraiders-data", "what": "recipes, recycling, ARC drops, trades, projects (MIT)"},
            ]}
    data = {"items": items, "quests": quests, "hideout": hideout, "projects": base["projects"], "bots": base["bots"],
            "trades": base["trades"], "maps": maps, "skills": skills, "builds": builds, "meta": meta,
            "markers": markers, "events": events, "guides": guides, "align": base.get("align", {})}
    return data


def render(data, mapimg, out_dir, log=print):
    tpl = open(os.path.join(RES, "web", "app.html"), encoding="utf-8").read()
    out = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":"))) \
             .replace("/*__MAPS__*/null", json.dumps(mapimg))
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, "artifact.html"), "w", encoding="utf-8").write(out)
    page = os.path.join(out_dir, "index.html")
    open(page + ".tmp", "w", encoding="utf-8").write(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>'
        + out + '</body></html>')
    for attempt in range(10):                   # Windows: the open window or an antivirus scan can hold the file
        try:
            os.replace(page + ".tmp", page)
            break
        except PermissionError:
            if attempt == 9:
                raise
            import time
            time.sleep(0.5)
    log(f"page: {page} ({len(out) // 1024} KB)")
    return page


def write_scanner_assets(src, data, assets):
    import numpy as np
    from PIL import Image
    os.makedirs(assets, exist_ok=True)
    json.dump({"quests": {k: {"n": v["n"], "pre": v.get("pre", [])} for k, v in data["quests"].items()},
               "items": {k: {"n": v["n"], "r": v.get("r"), "st": v.get("st", 1)} for k, v in data["items"].items()}},
              open(os.path.join(assets, "game.json"), "w", encoding="utf-8"), ensure_ascii=False)
    ids, imgs = [], []
    icon_dir = os.path.join(src, "images", "items_ingame")
    for fn in sorted(os.listdir(icon_dir)):
        iid = os.path.splitext(fn)[0]
        if iid not in data["items"]:
            continue
        im = Image.open(os.path.join(icon_dir, fn)).convert("RGBA")
        bg = Image.new("RGBA", im.size, (20, 22, 26, 255)); bg.alpha_composite(im)
        ids.append(iid); imgs.append(np.asarray(bg.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS))[:, :, ::-1])
    np.savez_compressed(os.path.join(assets, "refs.npz"), ids=np.array(ids), imgs=np.stack(imgs))
    return len(ids)


def run(src=None, base_path=None, cache=None, out_dir=None, refresh=False, offline=False, assets=True, log=print):
    """Full build from the RaidTheory checkout (src), or a page-only rebuild from base.json (base_path)."""
    cache = cache or os.path.join(HERE, "data", "cache")
    out_dir = out_dir or os.path.join(HERE, "dist")
    F = Fetcher(cache, refresh, offline, log)
    if src:
        base = load_raidtheory(src)
        mapimg, base["align"] = aligned_maps(F, log)
        base["mapimg"] = mapimg
    else:
        base = json.load(open(base_path or os.path.join(RES, "assets", "base.json"), encoding="utf-8"))
    data = assemble(base, F, log)
    page = render(data, base.get("mapimg", {}), out_dir, log)
    if src and assets:
        ad = os.path.join(HERE, "overlay", "assets")
        os.makedirs(ad, exist_ok=True)
        json.dump(base, open(os.path.join(ad, "base.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        log(f"scanner refs: {write_scanner_assets(src, data, ad)} item icons; base.json written")
    return page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", help="RaidTheory arcraiders-data checkout (full build)")
    ap.add_argument("--base", help="rebuild the page from this base.json instead of a checkout")
    ap.add_argument("--cache", default=os.path.join(HERE, "data", "cache"))
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    ap.add_argument("--refresh", action="store_true", help="download the web sources again")
    ap.add_argument("--offline", action="store_true", help="use only cached web sources")
    a = ap.parse_args()
    if not a.src and not a.base:
        ap.error("give the arcraiders-data checkout or --base")
    run(a.src, a.base, a.cache, a.out, a.refresh, a.offline)


if __name__ == "__main__":
    main()
