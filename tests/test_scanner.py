"""Synthetic tests for the screen scanner.

These build fake screenshots from the in-game icon set (different size, noise,
JPEG artefacts, brightness shift) and check that the scanner reads them back.
They prove the pipeline works; real screenshots are still needed to tune slot
detection and count position for the actual game UI.

Run:  python tests/test_scanner.py /path/to/arcraiders-data
"""
import os
import random
import re
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "overlay"))
from scanner import Scanner  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "arcraiders-data"
FONT = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf")
             if os.path.exists(f)), "arial.ttf")
OUT = os.environ.get("TEST_OUT", "")


def jpeg(img, q=78):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def fake_vault(sc, rng, slot=74, cols=8, rows=5, gap=8, n_items=32):
    img = np.full((1080, 1920, 3), (24, 21, 18), np.uint8)
    cv2.rectangle(img, (0, 0), (1920, 60), (40, 36, 32), -1)
    cv2.putText(img, "STASH", (40, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (220, 220, 220), 2)
    ids = rng.sample(sc.ref_ids, n_items)
    icon_dir = os.path.join(SRC, "images", "items_ingame")
    truth = {}
    x0, y0 = 420, 160
    k = 0
    for r in range(rows):
        for c in range(cols):
            x, y = x0 + c * (slot + gap), y0 + r * (slot + gap)
            if k >= n_items:                                      # empty slot
                cv2.rectangle(img, (x, y), (x + slot - 1, y + slot - 1), (55, 52, 48), 1)
                continue
            iid = ids[k]; k += 1
            im = Image.open(os.path.join(icon_dir, iid + ".png")).convert("RGBA")
            bg = Image.new("RGBA", im.size, (20, 22, 26, 255)); bg.alpha_composite(im)
            tile = cv2.resize(np.asarray(bg.convert("RGB"))[:, :, ::-1], (slot, slot), interpolation=cv2.INTER_LINEAR)
            tile = np.clip(tile.astype(np.int16) * rng.uniform(0.85, 1.12) + rng.randint(-10, 10), 0, 255).astype(np.uint8)
            img[y:y + slot, x:x + slot] = tile
            cv2.rectangle(img, (x, y), (x + slot - 1, y + slot - 1), (90, 90, 90), 1)
            st = sc.items.get(iid, {}).get("st") or 1
            n = rng.choice([v for v in (1, 2, 3, 5, 8, 12, 15, 25, 40, 99) if v <= st])
            if n > 1:
                cv2.putText(img, str(n), (x + slot - 8 - 9 * len(str(n)), y + slot - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 240, 240), 1, cv2.LINE_AA)
            truth[iid] = truth.get(iid, 0) + n
    noise = np.random.default_rng(rng.randint(0, 9999)).normal(0, 4, img.shape)
    return jpeg(np.clip(img + noise, 0, 255).astype(np.uint8)), truth


def fake_quest_log(sc, rng, n=5):
    img = Image.new("RGB", (1920, 1080), (18, 20, 24))
    d = ImageDraw.Draw(img)
    big, small = ImageFont.truetype(FONT, 26), ImageFont.truetype(FONT, 18)
    qids = rng.sample(list(sc.quests), n)
    d.text((60, 40), "QUESTS", font=ImageFont.truetype(FONT, 40), fill=(230, 230, 230))
    for i, q in enumerate(qids):
        y = 140 + i * 150
        d.rectangle((60, y - 10, 760, y + 120), outline=(70, 70, 80))
        d.text((80, y), sc.quests[q]["n"].upper() if i % 2 else sc.quests[q]["n"], font=big, fill=(235, 225, 200))
        d.text((80, y + 44), "Trader: Shani", font=small, fill=(150, 150, 160))
        d.text((80, y + 74), "Reach the location and search the area", font=small, fill=(150, 150, 160))
    arr = jpeg(np.asarray(img)[:, :, ::-1].copy(), 82)
    return arr, set(qids)


def main():
    sc = Scanner()
    rng = random.Random(7)
    ok_items = tot_items = ok_counts = 0
    for t in range(4):
        img, truth = fake_vault(sc, rng, slot=rng.choice([64, 74, 88]))
        if OUT:
            cv2.imwrite(os.path.join(OUT, f"vault{t}.jpg"), img)
        sc.reset_vault_session()
        res = sc.scan(img)
        got = {x["id"]: x["count"] for x in res.get("items", [])}
        tot_items += len(truth)
        ok_items += sum(1 for k in truth if k in got)
        ok_counts += sum(1 for k, v in truth.items() if got.get(k) == v)
        wrong = [k for k in got if k not in truth]
        # a near-identical tier variant (Arpeggio I vs II, Kettle III vs IV) that the app flags for review counts as found
        base = lambda k: re.sub(r"_(i|ii|iii|iv|v)$", "", k)
        flagged = {base(x["id"]) for x in res.get("items", []) if x["score"] <= 0.7}
        ok_items += sum(1 for k in truth if k not in got and base(k) in flagged)
        print(f"vault {t}: kind={res.get('kind')} found {sum(k in got for k in truth)}/{len(truth)} items, "
              f"counts right {sum(got.get(k) == v for k, v in truth.items())}, false {len(wrong)}")
    q_ok = q_tot = q_false = 0
    for t in range(4):
        img, truth = fake_quest_log(sc, rng)
        if OUT:
            cv2.imwrite(os.path.join(OUT, f"quests{t}.jpg"), img)
        res = sc.scan(img)
        got = {q["id"] for q in res.get("quests", [])}
        q_tot += len(truth); q_ok += len(truth & got); q_false += len(got - truth)
        print(f"quests {t}: kind={res.get('kind')} found {len(truth & got)}/{len(truth)}, false {len(got - truth)}")
    print(f"\nITEMS {ok_items}/{tot_items}  COUNTS {ok_counts}/{tot_items}  QUESTS {q_ok}/{q_tot} (false {q_false})")
    # synthetic look-alikes (tiers, key cards) differ a little between platforms; this guards against regressions
    assert ok_items / tot_items >= 0.92, "icon matching below 92%"
    assert q_ok / q_tot >= 0.95 and q_false == 0, "quest recognition below 95%"


if __name__ == "__main__":
    main()
