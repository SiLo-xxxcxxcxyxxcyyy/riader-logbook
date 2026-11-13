"""Build overlay/assets/refs_dino.npz: DINOv2 reference vectors for the scanner (see overlay/matcher.py).

1. every slot render in assets/refs.npz ("base"),
2. for items the icon set lacks: the item art from the ARC Raiders Wiki (page image; weapons use
   their family page) or else MetaForge, drawn into a game slot ("web"): background = median of
   the real slot renders of that rarity, art scaled to 66% and centred at 40% height (fitted on
   items that have both a slot render and wiki art: similarity 0.22 -> 0.83).

Needs network. Run after a data update:  python tools/build_web_refs.py [metaforge_items.json]
"""
import concurrent.futures as cf
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "overlay", "assets")
sys.path.insert(0, os.path.join(ROOT, "overlay"))
import matcher  # noqa: E402

UA = {"User-Agent": "RaiderLogbook"}
WIKI = "https://arcraiders.wiki/w/api.php"
MF_CACHE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RaiderLogbook", "cache", "metaforge_items.json")
FIT = {"s": 0.66, "cx": 0.5, "cy": 0.4}
TIER = re.compile(r"\s+(I|II|III|IV)$")


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()


def wiki_images(titles):
    """title -> thumbnail url (follows redirects)."""
    out = {}
    for s in range(0, len(titles), 40):
        chunk = titles[s:s + 40]
        q = json.loads(get(WIKI + "?" + urllib.parse.urlencode(dict(
            action="query", titles="|".join(chunk), prop="pageimages", pithumbsize=256, redirects=1, format="json"))))
        back = {}
        for k in ("normalized", "redirects"):
            for x in q["query"].get(k, []):
                back[x["to"]] = back.get(x["from"], x["from"])
        for p in q["query"]["pages"].values():
            if "thumbnail" in p:
                out[back.get(p["title"], p["title"])] = p["thumbnail"]["source"]
    return out


def art(raw):
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    bb = im.getchannel("A").point(lambda a: 255 if a > 20 else 0).getbbox()
    return im.crop(bb) if bb else None


def draw_slot(im, bg, S=96):
    out = Image.fromarray(np.ascontiguousarray(bg[:, :, ::-1])).resize((S, S), Image.Resampling.LANCZOS).convert("RGBA")
    w, h = im.size
    k = FIT["s"] * S / max(w, h)
    t = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.Resampling.LANCZOS)
    out.alpha_composite(t, (round(FIT["cx"] * S - t.width / 2), round(FIT["cy"] * S - t.height / 2)))
    return np.ascontiguousarray(np.asarray(out.convert("RGB"))[:, :, ::-1])


def main(mf_path=MF_CACHE):
    items = json.load(open(os.path.join(ASSETS, "game.json"), encoding="utf-8"))["items"]
    r = np.load(os.path.join(ASSETS, "refs.npz"))
    ids, imgs = [str(x) for x in r["ids"]], r["imgs"]
    rar = lambda i: {"Amplified": "Legendary"}.get(items[i].get("r"), items[i].get("r"))  # noqa: E731
    bgs = {}
    for k in {rar(i) for i in ids}:
        sel = [imgs[n] for n, i in enumerate(ids) if rar(i) == k]
        if len(sel) >= 5:
            bgs[k] = np.median(np.stack(sel), axis=0).astype(np.uint8)
    missing = [i for i in items if i not in set(ids)]

    urls = {}
    w = wiki_images([items[i]["n"] for i in missing])
    fam = wiki_images(sorted({TIER.sub("", items[i]["n"]) for i in missing if TIER.search(items[i]["n"])}))
    for i in missing:
        n = items[i]["n"]
        urls[i] = w.get(n) or fam.get(TIER.sub("", n))
    try:
        mf = {m["id"].replace("-", "_"): m.get("icon") for m in json.load(open(mf_path, encoding="utf-8"))}
        for i in missing:
            urls[i] = urls.get(i) or mf.get(i)
    except OSError:
        print("no MetaForge item list; wiki only")
    todo = [(i, u) for i, u in urls.items() if u]

    def fetch(iu):
        try:
            return iu[0], art(get(iu[1]))
        except Exception as e:
            print("  ", iu[0], e)
            return iu[0], None
    with cf.ThreadPoolExecutor(8) as ex:
        web = [(i, a) for i, a in ex.map(fetch, todo) if a is not None]
    web_imgs = [draw_slot(a, bgs.get(rar(i), bgs["Common"])) for i, a in web]

    emb = matcher.Embedder(os.path.join(ASSETS, "dinov2s_q.onnx"))
    base_imgs = [np.ascontiguousarray(np.asarray(Image.fromarray(np.ascontiguousarray(x[:, :, ::-1])).resize((96, 96), Image.Resampling.LANCZOS))[:, :, ::-1]) for x in imgs]
    E = emb.embed(base_imgs + web_imgs)
    np.savez_compressed(os.path.join(ASSETS, "refs_dino.npz"), ids=np.array(ids + [i for i, _ in web]),
                        emb=E.astype(np.float16), kind=np.array(["base"] * len(ids) + ["web"] * len(web)))
    print(f"refs_dino.npz: {len(ids)} slot renders + {len(web)} drawn from wiki/MetaForge art "
          f"({len(missing) - len(web)} items without any picture)")


if __name__ == "__main__":
    main(*sys.argv[1:2])
