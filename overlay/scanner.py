"""Screen scanner for Raider Logbook.

Reads a screenshot of ARC Raiders and recognises either
  * the quest log  -> quest titles via OCR, fuzzy-matched to the 100 known quests
  * a vault page   -> item slots via icon matching against in-game icons, stack counts via OCR

It only looks at pixels. It never reads game memory or files and never sends input.

CLI for testing:  python scanner.py screenshot.png [--debug out.png]
"""
from __future__ import annotations

import json
import os
import re
import sys

import cv2
import numpy as np
from rapidfuzz import fuzz

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(getattr(sys, "_MEIPASS", HERE), "assets")

REF = 64                      # reference icon size (px)
ICON_ROWS = (4, 46)           # rows of a 64px slot used for matching (skips the bottom strip with the count)
ICON_COLS = (4, 60)
ROW_SAME = 0.93               # feature similarity above which two slot pictures count as the same slot
BADGE = (0, 16, 46, 64)       # rows/cols of a 64px slot where the eye / quest-diamond badge sits
MATCH_MIN = 0.62              # minimum similarity to accept an icon match
QUEST_MIN = 86                # minimum fuzzy score (0-100) to accept a quest title


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower().replace("’", "'")).strip()


class Scanner:
    def __init__(self, assets: str = ASSETS, learned_dir: str | None = None):
        game = json.load(open(os.path.join(assets, "game.json"), encoding="utf-8"))
        self.quests = game["quests"]
        self.items = game["items"]
        self._qnames = [(qid, _norm(q["n"])) for qid, q in self.quests.items()]
        refs = np.load(os.path.join(assets, "refs.npz"))
        self.ref_ids = [str(x) for x in refs["ids"]]
        self.ref_feat = np.stack([self._feature(im) for im in refs["imgs"]])
        # icons you taught the app (items missing from the icon set, e.g. new ones): <item_id>__<key>.png
        self.learned_dir = learned_dir
        if learned_dir and os.path.isdir(learned_dir):
            for fn in sorted(os.listdir(learned_dir)):
                if fn.endswith(".png") and "__" in fn:
                    im = cv2.imdecode(np.fromfile(os.path.join(learned_dir, fn), np.uint8), cv2.IMREAD_COLOR)
                    if im is not None:
                        self._add_ref(fn.split("__")[0], im)
        self._ocr = None
        self.reset_vault_session()

    # ---------- helpers ----------
    @property
    def ocr(self):
        """OCR function img -> [(box, text, score)]; lone digits use a lower score threshold."""
        if self._ocr is None:
            self._ocr, self._ocr_digits = _ocr_engine(), _ocr_engine(text_score=0.3)
        return self._ocr

    def _read(self, img):
        """OCR -> list of (text, (cx, cy), box)."""
        res = self.ocr(img)
        out = []
        for box, text, _conf in res or []:
            pts = np.array(box, dtype=float)
            out.append((text, (pts[:, 0].mean(), pts[:, 1].mean()), pts))
        return out

    @staticmethod
    def _parts(a):
        a = a - a.mean()
        n = np.linalg.norm(a)
        return a / n if n > 1e-6 else a

    @classmethod
    def _feature(cls, slot_bgr):
        s = cv2.resize(slot_bgr, (REF, REF), interpolation=cv2.INTER_AREA)
        # the game puts badges in the top-right corner (eye = marked by you, diamond = needed for a
        # quest); reference icons have none, so blank that corner on both sides
        s[BADGE[0]:BADGE[1], BADGE[2]:BADGE[3]] = np.median(s[2:14, 2:18].reshape(-1, 3), axis=0)
        icon = cv2.resize(s[ICON_ROWS[0]:ICON_ROWS[1], ICON_COLS[0]:ICON_COLS[1]], (28, 21),
                          interpolation=cv2.INTER_AREA).astype(np.float32)
        g = cv2.cvtColor(icon.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
        edges = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
        # bottom-right of the strip holds the tier numeral (I, II, III, IV) on weapons and mods
        strip = cv2.cvtColor(s[48:62, 36:62], cv2.COLOR_BGR2GRAY).astype(np.float32)
        f = np.concatenate([cls._parts(icon.ravel()), cls._parts(edges.ravel()), 1.0 * cls._parts(strip.ravel())])
        return f / max(np.linalg.norm(f), 1e-6)

    # ---------- vault ----------
    @staticmethod
    def find_slots(img):
        """Find square item slots. Returns list of (x, y, w, h) with the dominant slot size."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        H, W = gray.shape
        lo, hi = max(24, int(min(H, W) * 0.025)), int(min(H, W) * 0.25)
        cands = []
        for edges in (cv2.Canny(gray, 30, 90), cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -4)):
            edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))
            cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                x, y, w, h = cv2.boundingRect(c)
                if lo <= w <= hi and 0.88 <= w / h <= 1.12 and cv2.contourArea(c) > 0.6 * w * h:
                    cands.append((x, y, w, h))
        if not cands:
            return []
        sizes = np.array([(w + h) / 2 for _, _, w, h in cands])
        # dominant size: the size with most candidates within ±8%
        best = max(sizes, key=lambda s: np.sum(np.abs(sizes - s) <= 0.08 * s))
        keep = [r for r, s in zip(cands, sizes) if abs(s - best) <= 0.08 * best]
        keep.sort(key=lambda r: -r[2] * r[3])
        out = []
        for r in keep:                                   # non-maximum suppression
            if all(_iou(r, o) < 0.4 for o in out):
                out.append(r)
        return _snap_to_grid(_largest_group(out, img))

    def _add_ref(self, item_id, slot_bgr):
        self.ref_ids.append(item_id)
        self.ref_feat = np.vstack([self.ref_feat, self._feature(slot_bgr)[None, :]])

    @staticmethod
    def _slot_key(slot_bgr):
        """Same-looking slots (the same unknown item) get the same key."""
        g = cv2.cvtColor(cv2.resize(slot_bgr, (64, 64), interpolation=cv2.INTER_AREA)[4:46, 4:60], cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (8, 6), interpolation=cv2.INTER_AREA)
        return "".join("1" if v > g.mean() else "0" for v in g.ravel())

    @staticmethod
    def _is_empty(slot_bgr):
        h, w = slot_bgr.shape[:2]
        inner = slot_bgr[int(h * .15):int(h * .85), int(w * .15):int(w * .85)]
        return inner.std() < 9

    def learn(self, key, item_id):
        """Remember an unknown slot as item_id (saved for future scans) and move it into the totals."""
        return self.reassign(None, key, item_id)

    def classify(self, slot_bgr):
        f = self._feature(slot_bgr)
        sims = self.ref_feat @ f
        i = int(np.argmax(sims))
        return self.ref_ids[i], float(sims[i])

    def classify_top(self, slot_bgr, k=2):
        sims = self.ref_feat @ self._feature(slot_bgr)
        order = np.argsort(-sims)[:k]
        return [(self.ref_ids[j], float(sims[j])) for j in order]

    def classify_slot(self, img, box, k=2):
        """Classify a slot, trying small shifts and sizes so a slightly-off box still lines up."""
        x, y, w, h = box
        H, W = img.shape[:2]
        best_sims, best_crop = None, None
        step = max(1, round(w / 40))
        for d in (0, -2 * step, -4 * step):                      # shrink
            for dx in (-step * 2, -step, 0, step, step * 2):
                for dy in (-step * 2, -step, 0, step, step * 2):
                    x0, y0 = x + dx - d // 2, y + dy - d // 2
                    x1, y1 = x0 + w + d, y0 + h + d
                    if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
                        continue
                    sims = self.ref_feat @ self._feature(img[y0:y1, x0:x1])
                    if best_sims is None or sims.max() > best_sims.max():
                        best_sims, best_crop = sims, (x0, y0, x1 - x0, y1 - y0)
        order = np.argsort(-best_sims)[:k]
        return [(self.ref_ids[j], float(best_sims[j])) for j in order], best_crop

    def scan_vault(self, img, ocr_lines=None):
        slots = self.find_slots(img)
        if len(slots) < 4:
            return None
        found, unknown = [], 0
        for (x, y, w, h) in slots:
            crop = img[y:y + h, x:x + w]
            if crop.size == 0 or self._is_empty(crop):     # empty slot (just the frame)
                continue
            ((iid, score), (alt, alt_score)), (x, y, w, h) = self.classify_slot(img, (x, y, w, h))
            c = img[y:y + h, x:x + w].copy()
            slot = {"crop": c, "key": self._slot_key(c), "feat": self._feature(c), "box": (x, y, w, h), "count": 1}
            if score < MATCH_MIN:
                unknown += 1
                found.append({**slot, "id": "?" + slot["key"], "unknown": True, "score": score})
                continue
            if score - alt_score < 0.02:            # near-identical icons (e.g. tier variants): flag for review
                score = min(score, 0.7)
            found.append({**slot, "id": iid, "score": score})
        if sum(1 for f in found if not f.get("unknown")) < 1 and len(found) < 3:
            return None
        self._read_counts(img, found)
        return {"slots": found, "unknown": unknown}

    def _read_counts(self, img, found):
        """Read the stack count of each slot with recognition-only OCR on the slot corners."""
        for s in found:
            st = 9999 if s.get("unknown") else (self.items.get(s["id"], {}).get("st") or 1)
            x, y, w, h = s["box"]
            best = None
            for (ya, yb, xa, xb) in ((0.70, 1.0, 0.45, 1.0), (0.0, 0.30, 0.45, 1.0)):   # bottom-right, top-right
                crop = img[y + int(h * ya):y + int(h * yb), x + int(w * xa):x + int(w * xb)]
                if crop.size == 0:
                    continue
                f = 96.0 / crop.shape[0]
                crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
                self.ocr  # make sure the engines are loaded
                res = self._ocr_digits(crop)
                for r in res or []:
                    text, conf = r[-2], float(r[-1])
                    t = text.strip().replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1")
                    m = re.fullmatch(r"([x×X])?\s*(\d{1,4})\s*[x×X]?", t)
                    # the game writes stack counts as "×24"; bare digits may be a misread tier numeral (II -> 11)
                    limit = 9999 if (m and m.group(1)) else max(st, 1)
                    if m and 1 <= int(m.group(2)) <= limit and conf > 0.3:
                        if best is None or conf > best[1]:
                            best = (int(m.group(2)), conf)
            if best:
                s["count"] = best[0]

    def reset_vault_session(self):
        """Start a new scan session (vault pages and quests are collected until the next reset)."""
        self._pages, self._totals, self._unknown, self._last_rows = [], {}, 0, []
        self._unknowns, self._unknown_crops = {}, {}
        self._quests = {}
        self._slots = 0            # occupied slots counted so far (rows seen twice are counted once)
        self._variant_crops = {}   # (item_id, slot key) -> picture, so a wrong match can be corrected
        self._cats = {}            # stash category index (0 = all) -> {"used", "cap", "slots"}
        self._cat = None           # category of the last page

    @staticmethod
    def read_stash_header(lines, k=1.0):
        """The stash header under "STASH": "287/292" (used/capacity) on the all-items view, or just
        "223" when a category filter is on. Returns ({"used", "cap"}, label position) or (None, label).
        Other counters on screen (backpack 0/10, quick use 0/4) are ignored by taking the one closest
        below the STASH label."""
        label = next((c for t, c, _ in lines if _norm(t) == "stash"), None)
        if label is None:
            return None, None
        best = None
        for t, (cx, cy), _ in lines:
            m = re.fullmatch(r"\D{0,3}?(\d{1,4})(?:\s*[/l|]\s*(\d{2,4}))?", t.strip())
            if not m:
                continue
            used, cap = int(m.group(1)), int(m.group(2)) if m.group(2) else None
            dy, dx = cy - label[1], abs(cx - label[0])
            if not (0 < dy < 60 * k and dx < 120 * k and (cap is None or used <= cap)):
                continue
            if best is None or dy + dx < best[0]:
                best = (dy + dx, {"used": used, "cap": cap})
        return (best and best[1]), label

    @staticmethod
    def stash_category(img, label, k=1.0):
        """Which category button left of the stash is selected (0 = all items). The selected one is a
        white disc; buttons sit in a column 89px left of the STASH label, 140px below it, 50px apart
        (at 1080p). Returns None if no button stands out."""
        if label is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        x, r = int(label[0] - 89 * k), max(4, int(20 * k))
        vals = []
        for i in range(10):
            y = int(label[1] + (140 + 50 * i) * k)
            patch = gray[max(0, y - r):y + r, max(0, x - r):x + r]
            vals.append(float((patch > 200).mean()) if patch.size else 0.0)   # share of white pixels
        order = np.argsort(vals)[::-1]
        if vals[order[0]] > 0.3 and vals[order[1]] < 0.1:
            return int(order[0])
        return None

    @staticmethod
    def _rows(slots):
        """Group slots into rows by their y position (a slot a few px off still joins its row)."""
        size = float(np.median([s["box"][3] for s in slots]))
        rows = []
        for s in sorted(slots, key=lambda s: s["box"][1]):
            if rows and s["box"][1] - rows[-1][0]["box"][1] < size * 0.45:
                rows[-1].append(s)
            else:
                rows.append([s])
        return [sorted(r, key=lambda s: s["box"][0]) for r in rows]

    @staticmethod
    def _same_row(a, b):
        """Two rows show the same slots: same length and every slot looks the same. Compares the
        pictures, not the recognised names, so one misread slot doesn't break the overlap."""
        return len(a) == len(b) and all(float(x @ y) > ROW_SAME for x, y in zip(a, b))

    def _add_vault_page(self, page):
        """Add a page, skipping rows already counted when the stash was only scrolled a bit."""
        rows = self._rows(page["slots"])
        feats = [[s["feat"] for s in r] for r in rows]
        last = self._last_rows
        skip = 0
        for k in range(min(len(feats), len(last)), 0, -1):       # largest overlap: our first k rows == their last k
            if all(self._same_row(a, b) for a, b in zip(feats[:k], last[-k:])):
                skip = k
                break
        if skip == len(rows) and len(rows) == len(last):          # the same page again
            return False
        self._pages.append(len(rows))
        self._last_rows = feats
        new = [s for r in rows[skip:] for s in r]
        self._slots += len(new)
        cat = self._cats.get(self._cat)
        if cat is not None:
            cat["slots"] += len(new)
        for s in new:
            if s.get("unknown"):
                key = s["key"]
                u = self._unknowns.setdefault(key, {"key": key, "count": 0, "img": _thumb(s["crop"])})
                u["count"] += s["count"]
                self._unknown_crops.setdefault(key, s["crop"])
                continue
            t = self._totals.setdefault(s["id"], {"id": s["id"], "count": 0, "score": 1.0, "variants": {}})
            t["count"] += s["count"]
            t["score"] = min(t["score"], round(s["score"], 3))
            v = t["variants"].setdefault(s["key"], {"key": s["key"], "count": 0, "score": 1.0, "img": _thumb(s["crop"], 48)})
            v["count"] += s["count"]
            v["score"] = min(v["score"], round(s["score"], 3))
            self._variant_crops.setdefault((s["id"], s["key"]), s["crop"])
        self._unknown += page["unknown"]
        return True

    def reassign(self, from_id, key, to_id):
        """'These slots are really to_id': move one look-alike group (an unknown, or a variant of a
        recognised item, e.g. a weapon whose silencer changes its icon) and remember its picture."""
        if from_id:
            t = self._totals.get(from_id)
            v = t and t["variants"].pop(key, None)
            if not v:
                return None
            t["count"] -= v["count"]
            if t["count"] <= 0:
                del self._totals[from_id]
            crop, count = self._variant_crops.pop((from_id, key)), v["count"]
        else:
            u = self._unknowns.pop(key, None)
            if u is None:
                return None
            crop, count = self._unknown_crops.pop(key), u["count"]
        self._add_ref(to_id, crop)
        if self.learned_dir:
            os.makedirs(self.learned_dir, exist_ok=True)
            cv2.imencode(".png", crop)[1].tofile(os.path.join(self.learned_dir, f"{to_id}__{key[:16]}.png"))
        t = self._totals.setdefault(to_id, {"id": to_id, "count": 0, "score": 1.0, "variants": {}})
        t["count"] += count
        v = t["variants"].setdefault(key, {"key": key, "count": 0, "score": 1.0, "img": _thumb(crop, 48)})
        v["count"] += count
        self._variant_crops[(to_id, key)] = crop
        return self._vault_result()

    def _vault_result(self):
        items = sorted(({**t, "variants": sorted(t["variants"].values(), key=lambda v: v["score"])}
                        for t in self._totals.values()),
                       key=lambda x: self.items.get(x["id"], {}).get("n", x["id"]))
        unk = list(self._unknowns.values())
        cats = [{"cat": c, **v} for c, v in sorted(self._cats.items())]
        return {"kind": "vault", "items": items, "pages": len(self._pages), "unknown": len(unk), "unknowns": unk,
                "slots": self._slots, "stash": self._cats.get(self._cat) and {"cat": self._cat, **self._cats[self._cat]},
                "categories": cats,
                # the all-items view plus a category in one session counts that category twice
                "mixed": 0 in self._cats and len(self._cats) > 1}

    # ---------- quests ----------
    def scan_quests(self, lines):
        best = {}
        for text, _, _ in lines:
            t = _norm(text)
            if len(t) < 4:
                continue
            for qid, qn in self._qnames:
                sc = fuzz.ratio(t, qn)
                if sc < QUEST_MIN and len(t) > len(qn) + 3:
                    sc = fuzz.token_set_ratio(t, qn) - 4      # title merged with other text on the same line
                if sc >= QUEST_MIN and sc > best.get(qid, {}).get("score", 0) and len(t) <= len(qn) * 2 + 12:
                    best[qid] = {"id": qid, "score": float(sc), "text": text}
        return sorted(best.values(), key=lambda q: -q["score"])

    # ---------- entry point ----------
    def scan(self, img):
        """Auto-detect what is on screen. Returns a JSON-able dict for the UI."""
        h, w = img.shape[:2]
        small = img if w <= 1920 else cv2.resize(img, (1920, int(h * 1920 / w)), interpolation=cv2.INTER_AREA)
        lines = self._read(small)
        quests = self.scan_quests(lines)
        slots = self.find_slots(img)
        if quests and len(slots) < 8:                            # a quest screen (reward icons are a few slots)
            new = [q["id"] for q in quests if q["id"] not in self._quests]
            for q in quests:
                if q["score"] > self._quests.get(q["id"], {}).get("score", 0):
                    self._quests[q["id"]] = q
            return {"kind": "quests", "quests": sorted(self._quests.values(), key=lambda q: -q["score"]), "added": new}
        page = self.scan_vault(img)
        if page:
            k = small.shape[0] / 1080
            head, label = self.read_stash_header(lines, k)
            cat = self.stash_category(small, label, k)
            if cat is None and head:
                cat = 0 if head["cap"] else -1                   # -1: some category, button not found
            if cat is not None:
                if cat != self._cat:
                    self._last_rows = []                          # other category: no overlap with the last page
                self._cat = cat
                c = self._cats.setdefault(cat, {"used": None, "cap": None, "slots": 0})
                if head:
                    c["used"], c["cap"] = head["used"], head["cap"]
            added = self._add_vault_page(page)
            return {**self._vault_result(), "duplicate": not added}
        return {"error": "Nothing recognised. Open the quest log or a vault page and try again.",
                "debug": {"size": f"{w}x{h}", "text_lines": len(lines), "slots_found": len(slots),
                          "slot_size": int(np.median([s[2] for s in slots])) if slots else 0}}


def _thumb(crop, size=72):
    import base64
    t = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
    return "data:image/png;base64," + base64.b64encode(cv2.imencode(".png", t)[1].tobytes()).decode()


def _ocr_engine(text_score=None):
    """RapidOCR wrapper for both package generations: `rapidocr` 3.x (Python 3.10-3.14) and the
    older `rapidocr_onnxruntime` 1.x (Python <= 3.12)."""
    try:
        from rapidocr import RapidOCR
        eng = RapidOCR(params={"Global.text_score": text_score, "Global.log_level": "warning"} if text_score
                       else {"Global.log_level": "warning"})

        def run(img):
            r = eng(img)
            if getattr(r, "boxes", None) is not None:
                return [(b.tolist(), t, float(sc)) for b, t, sc in zip(r.boxes, r.txts, r.scores)]
            if not text_score:
                return []
            # small corner crops (stack counts): the detector can miss a lone digit, so read the crop as one line
            h, w = img.shape[:2]
            pad = cv2.copyMakeBorder(img, 8, 8, 24, 24, cv2.BORDER_REPLICATE)
            r = eng(pad, use_det=False, use_cls=False, use_rec=True)
            txts = list(getattr(r, "txts", None) or [])
            scores = list(getattr(r, "scores", None) or [])
            return [([[0, 0], [w, 0], [w, h], [0, h]], t, float(sc)) for t, sc in zip(txts, scores) if t]
        return run
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR
        eng = RapidOCR(text_score=text_score) if text_score else RapidOCR()

        def run(img):
            res, _ = eng(img)
            return [(b, t, float(sc)) for b, t, sc in (res or [])]
        return run


def _largest_group(rects, img=None):
    """Keep the cluster of touching slots that is the stash grid, dropping loadout/backpack slots and
    reward icons elsewhere on the screen. The stash is the cluster with the most filled slots: with a
    short category (3 augments) the empty backpack grid next to it has more slots in total."""
    if len(rects) < 2:
        return rects
    size = float(np.median([w for _, _, w, _ in rects]))
    c = [(x + w / 2, y + h / 2) for x, y, w, h in rects]
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            dx, dy = abs(c[i][0] - c[j][0]), abs(c[i][1] - c[j][1])
            if (dx < size * 1.35 and dy < size * 0.3) or (dy < size * 1.35 and dx < size * 0.3):
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(rects)):
        groups.setdefault(find(i), []).append(rects[i])
    if img is None:
        return max(groups.values(), key=len)

    def filled(g):   # clearly holds an item (empty quick-use slots show a faint ring, std ~13; items ~50)
        n = 0
        for x, y, w, h in g:
            inner = img[y + h * 15 // 100:y + h * 85 // 100, x + w * 15 // 100:x + w * 85 // 100]
            n += inner.size > 0 and float(inner.std()) > 25
        return n
    return max(groups.values(), key=lambda g: (filled(g), len(g)))


def _snap_to_grid(rects):
    """Fit detected slots to a regular grid and return every grid cell (also ones the detector missed)."""
    if len(rects) < 4:
        return rects
    size = float(np.median([min(w, h) for _, _, w, h in rects]))
    cx = sorted(x + w / 2 for x, _, w, _ in rects)
    cy = sorted(y + h / 2 for _, y, _, h in rects)

    def centers(vals):
        groups = [[vals[0]]]
        for v in vals[1:]:
            (groups[-1].append(v) if v - groups[-1][-1] < size * 0.35 else groups.append([v]))
        cs = [float(np.median(g)) for g in groups if len(g) >= 2] or [float(np.median(g)) for g in groups]
        if len(cs) >= 2:                                 # fill gaps in a regular pitch
            pitch = float(np.median(np.diff(cs)))
            full = [cs[0]]
            for c in cs[1:]:
                steps = int(round((c - full[-1]) / pitch))
                for k in range(1, steps):
                    full.append(full[-1] + (c - full[-1]) / (steps - k + 1))
                full.append(c)
            cs = full
        return cs

    cols, rows = centers(cx), centers(cy)
    half = size / 2
    return [(int(round(c - half)), int(round(r - half)), int(size), int(size)) for r in rows for c in cols]


def _iou(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx)); iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--debug", help="write an annotated copy of the image here")
    a = ap.parse_args()
    img = cv2.imread(a.image)
    sc = Scanner()
    res = sc.scan(img)
    print(json.dumps(res, indent=1, ensure_ascii=False))
    if a.debug:
        for (x, y, w, h) in sc.find_slots(img):
            cv2.rectangle(img, (x, y), (x + w, y + h), (0, 200, 255), 2)
        cv2.imwrite(a.debug, img)
