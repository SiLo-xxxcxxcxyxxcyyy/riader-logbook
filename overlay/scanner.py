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
BADGE = (0, 16, 46, 64)
DETAIL = (12, 44, 12, 42)     # rows/cols of a 64px slot compared at full detail when two items nearly tie
CLOSE = 0.03                  # "nearly tie": within this much of the best match
TIER_RE = re.compile(r"(.+)_(i|ii|iii|iv)")
NUMERAL = (70, 96, 66, 96)     # rows/cols of a 96px slot searched for the tier numeral
DINO_MIN = {"base": 0.78, "web": 0.44, "taught": 0.9}   # DINOv2 similarity needed, by picture source
DINO_MARGIN = 0.02            # ...and the lead over the best other item
LEARNED_MIN = 0.9            # taught pictures only count above this similarity
DETAIL_MARGIN = 0.05        # the detail picture must beat the main match's detail by this much       # rows/cols of a 64px slot where the eye / quest-diamond badge sits
NEAR_OK = 0.85                # pixel match this good on the grid box: skip the wider shift search
CACHE_SAME = 0.99             # similarity (whole slot and count corner, each) at which an earlier reading is reused
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
        self.ref_detail = np.stack([self._detail(im) for im in refs["imgs"]])
        self._n_base = len(self.ref_ids)          # refs after this index are pictures you taught
        # DINOv2 embeddings (overlay/matcher.py): the main matcher when its files are present
        self._dino = None
        try:
            import matcher
            m = matcher.load(assets)
            if m:
                self._dino, self.dino_ids, self.dino_emb, self.dino_kind = m
        except Exception:                          # missing onnxruntime / model: pixel features only
            self._dino = None
        # icons you taught the app (items missing from the icon set, e.g. new ones): <item_id>__<key>.png
        self.learned_dir = learned_dir
        taught = []
        if learned_dir and os.path.isdir(learned_dir):
            for fn in sorted(os.listdir(learned_dir)):
                if fn.endswith(".png") and "__" in fn:
                    im = cv2.imdecode(np.fromfile(os.path.join(learned_dir, fn), np.uint8), cv2.IMREAD_COLOR)
                    if im is not None:
                        self._add_ref(fn.split("__")[0], im, embed=False)
                        taught.append((fn.split("__")[0], im))
        if self._dino and taught:
            self._dino_add([t for t, _ in taught], [im for _, im in taught])
        # tier numerals (I-IV) drawn bottom-right on weapons and mods; all tiers share one icon
        num = np.load(os.path.join(assets, "numerals.npz")) if os.path.exists(os.path.join(assets, "numerals.npz")) else None
        self._numerals = [(str(l), im.astype(np.float32)) for l, im in zip(num["labels"], num["imgs"])] if num is not None else []
        self._ocr = None
        self._auto = False
        self._cache_vecs, self._cache_vals = [], []
        self.reset_vault_session()

    @staticmethod
    def family(item_id):
        """'anvil_iv' -> ('anvil', 'iv'); items without a tier -> (item_id, None)."""
        m = TIER_RE.fullmatch(item_id)
        return (m.group(1), m.group(2)) if m else (item_id, None)

    def read_tier(self, slot_bgr):
        """Read the tier numeral in the slot's bottom-right corner: 'i'..'iv', or None if unsure."""
        if not self._numerals:
            return None
        s = cv2.cvtColor(cv2.resize(slot_bgr, (96, 96), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        area = s[NUMERAL[0]:NUMERAL[1], NUMERAL[2]:NUMERAL[3]].astype(np.float32)
        scores = sorted(((float(cv2.matchTemplate(area, t, cv2.TM_CCOEFF_NORMED).max()), l) for l, t in self._numerals),
                        reverse=True)
        if scores[0][0] > 0.7 and scores[0][0] - scores[1][0] > 0.04:
            return scores[0][1]
        return None

    def _with_tier(self, slot_bgr, iid):
        """Weapons and mods: the icon names the weapon, the numeral the tier. Lets one taught tier
        (or one reference icon) recognise every tier of that weapon."""
        fam, tier = self.family(iid)
        if tier is None or (self.items.get(iid, {}).get("st") or 1) > 1:
            return iid
        t = self.read_tier(slot_bgr)
        return f"{fam}_{t}" if t and f"{fam}_{t}" in self.items else iid

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

    def _add_ref(self, item_id, slot_bgr, embed=True):
        self.ref_ids.append(item_id)
        self.ref_feat = np.vstack([self.ref_feat, self._feature(slot_bgr)[None, :]])
        self.ref_detail = np.vstack([self.ref_detail, self._detail(slot_bgr)[None, :]])
        if embed and self._dino:
            self._dino_add([item_id], [slot_bgr])

    @staticmethod
    def _dino_size(bgr):
        return cv2.resize(bgr, (96, 96), interpolation=cv2.INTER_AREA)

    def _dino_add(self, ids, imgs):
        e = self._dino.embed([self._dino_size(i) for i in imgs])
        self.dino_ids = self.dino_ids + list(ids)
        self.dino_emb = np.vstack([self.dino_emb, e])
        self.dino_kind = self.dino_kind + ["taught"] * len(ids)

    def _guesses(self, e, crop, k=3):
        """The k most likely different items for a slot the matcher wasn't sure about, offered as
        one-click answers (picking from a short list beats typing a name: ARC Conductive / Insulated /
        Plated Coupler are easy to mix up in a long list)."""
        s = self.dino_emb @ e
        out = []
        for j in np.argsort(-s)[:60]:
            iid = self._with_tier(crop, self.dino_ids[j])
            if iid not in out and iid in self.items:
                out.append(iid)
            if len(out) == k:
                break
        return out

    def _dino_decide(self, e):
        """Best item for one slot embedding: (item_id, score, margin) or None when unsure.
        Thresholds by where the reference picture came from: a game slot render, a slot drawn from
        internet art (looks less alike, so a lower bar), or a picture you taught (a real slot
        screenshot, so it must match closely). The best item must also beat the best other item
        by a margin. Tuned on real 1080p scans (cross-validated: 87/97 right, 2.5 wrong)."""
        s = self.dino_emb @ e
        order = np.argsort(-s)[:40]
        j = int(order[0])
        f0 = self.family(self.dino_ids[j])[0]
        j2 = next((int(k) for k in order[1:] if self.family(self.dino_ids[k])[0] != f0), None)
        margin = float(s[j] - s[j2]) if j2 is not None else 1.0
        if s[j] < DINO_MIN[self.dino_kind[j]] or margin < DINO_MARGIN:
            return None
        return self.dino_ids[j], float(s[j]), margin, {self.family(self.dino_ids[k])[0]: float(s[k]) for k in order[::-1]}

    @classmethod
    def _detail(cls, slot_bgr):
        """Full-resolution colour picture of the middle of the icon. Tells apart items whose icons
        differ only in a small symbol (Combat Mk. 3 Aggressive vs Flanking: same case, other emblem)."""
        s = cv2.resize(slot_bgr, (REF, REF), interpolation=cv2.INTER_AREA)
        return cls._parts(s[DETAIL[0]:DETAIL[1], DETAIL[2]:DETAIL[3]].astype(np.float32).ravel())

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
        # the box from the grid is usually right: try it and its nearest shifts first, and search the
        # wider set of shifts and sizes only when that doesn't give a clear match
        near = [(0, dx, dy) for dx in (-step, 0, step) for dy in (-step, 0, step)]
        wide = [(d, dx, dy) for d in (0, -2 * step, -4 * step) for dx in (-step * 2, -step, 0, step, step * 2)
                for dy in (-step * 2, -step, 0, step, step * 2) if (d, dx, dy) not in near]
        for stage in (near, wide):
            if stage is wide and best_sims is not None and best_sims.max() >= NEAR_OK:
                break
            for d, dx, dy in stage:
                if True:
                    x0, y0 = x + dx - d // 2, y + dy - d // 2
                    x1, y1 = x0 + w + d, y0 + h + d
                    if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
                        continue
                    sims = self.ref_feat @ self._feature(img[y0:y1, x0:x1])
                    # a taught picture is a screenshot of a real slot: same frame, background and
                    # rarity colour as every other slot, so it resembles them all (~0.7). Only a
                    # near-exact match (the same item again, ~0.95+) counts.
                    taught = sims[self._n_base:]
                    taught[taught < LEARNED_MIN] = -1.0
                    if best_sims is None or sims.max() > best_sims.max():
                        best_sims, best_crop = sims, (x0, y0, x1 - x0, y1 - y0)
        order = list(np.argsort(-best_sims))
        # near-ties between different items: let the middle of the icon decide
        top = best_sims[order[0]]
        close = [j for j in order[:20] if best_sims[j] >= top - CLOSE]
        if len({self.ref_ids[j] for j in close}) > 1:
            bx, by, bw, bh = best_crop
            det = self.ref_detail[close] @ self._detail(img[by:by + bh, bx:bx + bw])
            # only when the middle clearly differs: tier variants (Ferro III / IV) share the same
            # picture and differ in the numeral below, which the main match already weighs
            if det.max() - det[0] > DETAIL_MARGIN:
                winner = close[int(np.argmax(det))]
                order.remove(winner)
                order.insert(0, winner)
        order = [order[0]] + [j for j in order[1:] if self.ref_ids[j] != self.ref_ids[order[0]]]
        return [(self.ref_ids[j], float(best_sims[j])) for j in order[:k]], best_crop

    def scan_vault(self, img, ocr_lines=None, view=None, progress=None):
        """view: (top, bottom) of the stash's visible area in pixels. Rows cut off by its edges (smooth
        scrolling leaves one half shown) are skipped: they'd be read as a different row than the same
        row seen whole, and break the joining of pages."""
        slots = self.find_slots(img)
        if view and slots:
            top, bottom = view
            slots = [s for s in slots if s[1] >= top - 2 and s[1] + s[3] <= bottom + 2]
        if len(slots) < 4:
            return None
        found, unknown, pending, cached, vecs, todo = [], 0, [], [], [], []
        for (x, y, w, h) in slots:
            crop = img[y:y + h, x:x + w]
            if crop.size == 0 or self._is_empty(crop):     # empty slot (just the frame)
                continue
            cv = self._cache_vec(crop)
            hit = self._cache_get(cv)
            if hit is not None:                            # the same slot seen in an earlier frame
                cached.append({**hit, "crop": crop.copy(), "box": (x, y, w, h)})
                continue
            todo.append(((x, y, w, h), cv))
        if progress and todo:
            # what's known already, and what is still being read (for the in-game marks)
            progress([[*map(int, s["box"]), None if s.get("unknown") else s["id"], int(s["count"]), "ok"] for s in cached] +
                     [[*map(int, b), None, 1, "busy"] for b, _ in todo])
        for (x, y, w, h), cv in todo:
            old, (x, y, w, h) = self.classify_slot(img, (x, y, w, h))
            pending.append((old, (x, y, w, h), img[y:y + h, x:x + w].copy()))
            vecs.append(cv)
        embs = self._dino.embed([self._dino_size(c) for _, _, c in pending]) if self._dino and pending else None
        for n, (((iid, score), (alt, alt_score)), (x, y, w, h), c) in enumerate(pending):
            slot = {"crop": c, "key": self._slot_key(c), "feat": self._feature(c), "box": (x, y, w, h), "count": 1,
                    "cvec": vecs[n]}
            sure = score >= MATCH_MIN
            # a near-exact pixel match (a picture you taught, or the same render) is reliable: keep it,
            # unless DINOv2 rates that item clearly lower than its best. The pixel features hardly see
            # colour: a red Spaceport keycard matched a taught white Dam keycard at 0.99.
            exact = score >= LEARNED_MIN
            if exact and embs is not None:
                s = self.dino_emb @ embs[n]
                fam = self.family(iid)[0]
                mine = max((float(s[k]) for k, d_id in enumerate(self.dino_ids) if self.family(d_id)[0] == fam), default=-1.0)
                exact = mine >= float(s.max()) - 0.015
            if embs is not None and not exact:
                d = self._dino_decide(embs[n])
                sure = d is not None                             # unsure: ask the user rather than guess
                if d is not None:
                    did, dscore, margin, fam_best = d
                    # the pixel matcher is finer between near-twins (weapon tiers, Combat Mk. 3 Aggressive
                    # vs Flanking): keep its answer when DINOv2 rates it about as high
                    if not (score >= MATCH_MIN and fam_best.get(self.family(iid)[0], -1) >= dscore - 0.03):
                        iid = did
                    alt = iid                                    # DINOv2's margin already covers look-alikes
                    score = dscore if margin >= 0.05 else min(dscore, 0.7)
            if not sure:
                unknown += 1
                guess = self._guesses(embs[n], c) if embs is not None else []
                found.append({**slot, "id": "?" + slot["key"], "unknown": True, "score": score, "guess": guess})
                continue
            tiered = self._with_tier(c, iid)
            # near-identical icons: flag for review, unless they are tiers of one weapon and the numeral was read
            tier_read = self.family(iid)[1] is not None and self.read_tier(c) is not None
            if alt != iid and score - alt_score < 0.02 and not (tier_read and self.family(alt)[0] == self.family(iid)[0]):
                score = min(score, 0.7)
            found.append({**slot, "id": tiered, "score": score})
        if sum(1 for f in found + cached if not f.get("unknown")) < 1 and len(found) + len(cached) < 3:
            return None
        self._read_counts(img, found)
        for f in found:
            self._cache_put(f)
        found = sorted(found + cached, key=lambda s: (s["box"][1], s["box"][0]))
        return {"slots": found, "unknown": sum(1 for f in found if f.get("unknown"))}

    # ---------- slot cache: a slot whose picture (count included) was already read ----------
    @staticmethod
    def _cache_vec(crop):
        """The whole slot in colour (small), plus its count corner at full detail: a stack of the same
        item with another count, or a coupler of another colour, must not pass for this one."""
        def unit(a):
            a = a.astype(np.float32).ravel()
            a -= a.mean()
            n = float(np.linalg.norm(a))
            return a / n if n > 1e-6 else a
        h, w = crop.shape[:2]
        whole = unit(cv2.resize(crop, (24, 24), interpolation=cv2.INTER_AREA))
        corner = crop[int(h * 0.70):, int(w * 0.45):]
        corner = unit(cv2.resize(cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY), (40, 22), interpolation=cv2.INTER_AREA))
        return np.concatenate([whole, corner]) / np.sqrt(2.0)

    def _cache_get(self, v):
        if not self._cache_vecs:
            return None
        m = np.stack(self._cache_vecs)
        n = 24 * 24 * 3
        whole, corner = m[:, :n] @ v[:n] * 2.0, m[:, n:] @ v[n:] * 2.0      # each part's own similarity
        s = np.minimum(whole, corner)
        j = int(np.argmax(s))
        return dict(self._cache_vals[j]) if s[j] > CACHE_SAME else None

    def _cache_put(self, slot):
        keep = ("id", "score", "count", "key", "feat", "unknown", "guess")
        self._cache_vecs.append(slot.pop("cvec"))
        self._cache_vals.append({k: slot[k] for k in keep if k in slot})
        if len(self._cache_vecs) > 600:
            del self._cache_vecs[:100], self._cache_vals[:100]

    def _read_counts(self, img, found):
        """Read the stack count of each slot with recognition-only OCR on the slot corners."""
        for s in found:
            st = 9999 if s.get("unknown") else (self.items.get(s["id"], {}).get("st") or 1)
            if not s.get("unknown") and self.family(s["id"])[1] is not None and st <= 1:
                continue                    # a weapon or mod tier: the corner holds its numeral, never a count
            x, y, w, h = s["box"]
            best = None
            for corner, (ya, yb, xa, xb) in enumerate(((0.70, 1.0, 0.45, 1.0), (0.0, 0.30, 0.45, 1.0))):   # bottom-right, top-right
                crop = img[y + int(h * ya):y + int(h * yb), x + int(w * xa):x + int(w * xb)]
                if crop.size == 0:
                    continue
                text_px = float((cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) > 190).mean())   # white count digits
                f = 96.0 / crop.shape[0]
                crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
                self.ocr  # make sure the engines are loaded
                # recognition only first (fast); the full detector only when that reads no count
                rec = getattr(self._ocr_digits, "rec", None)
                res = [(None, t, c) for t, c in rec(crop)] if rec else []
                if not any(re.fullmatch(r"[x×X]?\s*\d{1,4}\s*[x×X]?", r[1].strip().replace("O", "0").replace("o", "0")
                                        .replace("l", "1").replace("I", "1")) for r in res) and text_px > 0.004:
                    res = self._ocr_digits(crop)
                for r in res or []:
                    text, conf = r[-2], float(r[-1])
                    t = text.strip().replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1")
                    m = re.fullmatch(r"([x×X])?\s*(\d{1,4})\s*[x×X]?", t)
                    # the game writes stack counts as "×24"; bare digits may be a misread tier numeral (II -> 11)
                    limit = 9999 if (m and m.group(1)) else max(st, 1)
                    if corner and not (m and m.group(1)):
                        continue            # top corner: only an explicit "×N" (it's mostly empty there)
                    if m and 1 <= int(m.group(2)) <= limit and conf > 0.3:
                        if best is None or conf > best[1]:
                            best = (int(m.group(2)), conf)
                if best:
                    break                   # the count sits bottom right; the top corner only when that's empty
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
        self._abs = {}             # category -> {stash row number: row} (all-items view, automatic reading)
        self._seq, self._pos = {}, {}   # category -> rows seen so far in stash order / where the last page was
        self._value = None         # stash value in creds, from the header
        self._gap_now = False      # the last page didn't join up with what was read
        self._gaps = 0             # pages that didn't line up with anything seen (scrolled past rows)

    def fast_header(self, small, k):
        """Read the stash header from its fixed place (top left, under the STASH title) with
        recognition only, no text search over the screen: (header, label position, stash value) or
        None if the title there isn't STASH. ~0.1 s instead of ~3 s."""
        line = getattr(self.ocr, "line", None)
        if line is None:
            return None
        def crop(x0, y0, x1, y1):
            return small[int(y0 * k):int(y1 * k), int(x0 * k):int(x1 * k)]
        title = line(crop(140, 138, 260, 174))
        if _norm(title) != "stash":
            return None
        nums = line(crop(168, 170, 420, 198))
        m = re.search(r"(\d{1,4})\s*[/l|]\s*(\d{2,4})", nums)
        head = None
        if m and int(m.group(1)) <= int(m.group(2)):
            head = {"used": int(m.group(1)), "cap": int(m.group(2))}
            rest = nums[m.end():]
        else:
            m = re.match(r"\D{0,3}?(\d{1,4})(?![\d,.])", nums)
            if m:
                head = {"used": int(m.group(1)), "cap": None}
            rest = nums[m.end():] if m else nums
        v = re.search(r"(\d{1,3}(?:[,.\s]\d{3})+|\d{4,})", rest)
        value = int(re.sub(r"\D", "", v.group(1))) if v else None
        return head, (189 * k, 155 * k), value

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

    def _align(self, feats, seq):
        """Best place for a page's rows in a run of known rows: (overlap, offset) with page row i at
        seq[offset + i], every overlapping row the same; None if it shares no row."""
        best = None
        for o in range(-len(feats) + 1, len(seq) + 1):
            ov = 0
            for i, f in enumerate(feats):
                j = o + i
                if 0 <= j < len(seq):
                    if not self._same_row(f, seq[j]["feats"]):
                        ov = -1
                        break
                    ov += 1
            if ov > 0 and (best is None or ov > best[0]):
                best = (ov, o)
        return best

    @staticmethod
    def _overlap(a, b, same):
        """Largest k with the last k rows of run a equal to the first k rows of run b (0 if none)."""
        for k in range(min(len(a), len(b)), 0, -1):
            if all(same(x["feats"], y["feats"]) for x, y in zip(a[-k:], b[:k])):
                return k
        return 0

    def _count(self, slots, sign=1):
        """Add (sign 1) or take back (sign -1) slots in the totals."""
        self._slots += sign * len(slots)
        cat = self._cats.get(self._cat)
        if cat is not None:
            cat["slots"] += sign * len(slots)
        for s in slots:
            if s.get("unknown"):
                key = s["key"]
                if sign > 0:
                    u = self._unknowns.setdefault(key, {"key": key, "count": 0, "img": _thumb(s["crop"]),
                                                        "guess": s.get("guess") or []})
                    self._unknown_crops.setdefault(key, s["crop"])
                else:
                    u = self._unknowns.get(key)
                    if u is None:
                        continue
                u["count"] += sign * s["count"]
                if u["count"] <= 0:
                    self._unknowns.pop(key, None)
                continue
            if sign < 0:
                t = self._totals.get(s["id"])
                if t is None:
                    continue
                t["count"] -= s["count"]
                v = t["variants"].get(s["key"])
                if v:
                    v["count"] -= s["count"]
                    if v["count"] <= 0:
                        del t["variants"][s["key"]]
                if t["count"] <= 0:
                    del self._totals[s["id"]]
                continue
            t = self._totals.setdefault(s["id"], {"id": s["id"], "count": 0, "score": 1.0, "variants": {}})
            t["count"] += s["count"]
            t["score"] = min(t["score"], round(s["score"], 3))
            v = t["variants"].setdefault(s["key"], {"key": s["key"], "count": 0, "score": 1.0, "img": _thumb(s["crop"], 48)})
            v["count"] += s["count"]
            v["score"] = min(v["score"], round(s["score"], 3))
            self._variant_crops.setdefault((s["id"], s["key"]), s["crop"])

    @staticmethod
    def stash_thumb(img):
        """The stash scrollbar's thumb: (top, length) in pixels, or None. A light bar right of the
        grid (x ~585 at 1080p) whose top follows the scroll position."""
        H = img.shape[0]
        s = H / 1080
        g = cv2.cvtColor(img[int(200 * s):int(1000 * s), int(576 * s):int(596 * s)], cv2.COLOR_BGR2GRAY)
        best = None
        for x in range(g.shape[1]):
            on = np.concatenate([g[:, x] > 60, [False]])
            start = None
            for i, v in enumerate(on):
                if v and start is None:
                    start = i
                elif not v and start is not None:
                    if best is None or i - start > best[1]:
                        best = (start, i - start)
                    start = None
        if not best:
            return None
        return best[0] + int(200 * s), best[1]

    def row_numbers(self, img, rows, slots_total):
        """Where each row of this picture sits in the whole stash (0 = first row), read from the
        scrollbar: the list scrolls ~11.9 px per scrollbar px with 73 rows (fitted on real 1080p
        frames: scroll = (thumb top - 324) * (rows * 104 - 682) / 579, first row at y 268 when at the
        top). Only for the all-items view; None when the thumb isn't clear (the mouse pointer on it)."""
        t = self.stash_thumb(img)
        s = img.shape[0] / 1080
        if not t or abs(t[1] - 90 * s) > 6 * s or not rows:
            return None
        pitch = 104 * s
        n_rows = -(-slots_total // 4)
        scroll = (t[0] - 324 * s) * (n_rows * pitch - 682 * s) / (579 * s)
        out = []
        for r in rows:
            v = (scroll + r[0]["box"][1] - 268 * s) / pitch
            if abs(v - round(v)) > 0.3:                  # doesn't land on a row: don't trust it
                return None
            out.append(int(round(v)))
        return out

    def _add_vault_page_abs(self, page, rows, nums):
        """All-items view with row numbers: each stash row is counted once, by its number. A picture
        without numbers (pointer on the scrollbar) is placed by matching rows already read."""
        known = self._abs.setdefault(self._cat, {})
        page_rows = [{"feats": [s["feat"] for s in r],
                      "slots": [{k: s[k] for k in ("id", "count", "key", "unknown", "score", "guess") if k in s} for s in r]}
                     for r in rows]
        if nums is None:
            best = None
            for o in range(min(known, default=0) - len(rows), max(known, default=0) + 2):
                ov = 0
                for i, r in enumerate(page_rows):
                    k = known.get(o + i)
                    if k is not None:
                        if not self._same_row(r["feats"], k["feats"]):
                            ov = -1
                            break
                        ov += 1
                if ov > 0 and (best is None or ov > best[0]):
                    best = (ov, o)
            if best is None:
                return False                             # can't tell where this is: wait for a clearer picture
            nums = [best[1] + i for i in range(len(rows))]
        new_idx = [i for i, n in enumerate(nums) if n not in known]
        if not new_idx:
            return False
        for i in new_idx:
            known[nums[i]] = page_rows[i]
        self._pages.append(len(rows))
        self._gap_now = False
        self._count([dict(s, crop=c["crop"]) for i in new_idx for s, c in zip(page_rows[i]["slots"], rows[i])])
        self._unknown += page["unknown"]
        return True

    def _add_vault_page(self, page, nums=None):
        """Add a page, counting only rows not seen before. The rows read so far are kept in order, in
        runs (per category): a page that lines up with a run extends it (scrolling a little, back up, or
        the same page again counts nothing twice); one that lines up with nothing starts a new run (the
        stash opened in the middle, or a jump), and two runs that come to overlap are joined, taking
        back the rows counted in both."""
        rows = self._rows(page["slots"])
        if self._auto and self._cat == 0:
            return self._add_vault_page_abs(page, rows, nums)
        page_rows = [{"feats": [s["feat"] for s in r],
                      "slots": [{k: s[k] for k in ("id", "count", "key", "unknown", "score", "guess") if k in s} for s in r]}
                     for r in rows]
        feats = [r["feats"] for r in page_rows]
        runs = self._seq.setdefault(self._cat, [])
        best = None                                               # (overlap, run index, offset)
        for n, run in enumerate(runs):
            a = self._align(feats, run)
            if a and (best is None or a[0] > best[0]):
                best = (a[0], n, a[1])
        if best is None:
            if runs:
                self._gaps += 1
            if runs and not self._auto:
                runs[-1].extend(page_rows)                        # F9, a screen at a time: the next rows
                n, new_idx = len(runs) - 1, list(range(len(rows)))
            else:
                runs.append(page_rows)
                n, new_idx = len(runs) - 1, list(range(len(rows)))
        else:
            _, n, o = best
            run = runs[n]
            new_idx = [i for i in range(len(rows)) if not 0 <= o + i < len(run)]
            if not new_idx:                                       # the same rows again
                return False
            above = [page_rows[i] for i in new_idx if o + i < 0]
            below = [page_rows[i] for i in new_idx if o + i >= len(run)]
            run[:0] = above
            run.extend(below)
        self._gap_now = False
        self._pages.append(len(rows))
        self._last_rows = feats
        self._count([dict(s, crop=c["crop"]) for i in new_idx for s, c in zip(page_rows[i]["slots"], rows[i])])
        self._unknown += page["unknown"]
        # runs that now overlap: join them and take back the rows counted twice
        if self._auto:
            joined = True
            while joined and len(runs) > 1:
                joined = False
                for a in range(len(runs)):
                    for b in range(len(runs)):
                        if a == b:
                            continue
                        k = self._overlap(runs[a], runs[b], self._same_row)
                        if k:
                            self._count([s for r in runs[b][:k] for s in r["slots"]], -1)
                            runs[a] = runs[a] + runs[b][k:]
                            del runs[b]
                            joined = True
                            break
                    if joined:
                        break
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
        for runs in list(self._seq.values()) + [[list(d.values())] for d in self._abs.values()]:   # stored rows follow
            for run in runs:
                for r in run:
                    for s in r["slots"]:
                        if s["key"] == key and (s.get("id") == from_id if from_id else s.get("unknown")):
                            s["id"], s["unknown"] = to_id, False
        self._add_ref(to_id, crop)
        self._cache_vecs, self._cache_vals = [], []          # cached answers may be the old, wrong ones
        if self.learned_dir:
            os.makedirs(self.learned_dir, exist_ok=True)
            cv2.imencode(".png", crop)[1].tofile(os.path.join(self.learned_dir, f"{to_id}__{key[:16]}.png"))
        t = self._totals.setdefault(to_id, {"id": to_id, "count": 0, "score": 1.0, "variants": {}})
        t["count"] += count
        v = t["variants"].setdefault(key, {"key": key, "count": 0, "score": 1.0, "img": _thumb(crop, 48)})
        v["count"] += count
        self._variant_crops[(to_id, key)] = crop
        return self._vault_result()

    def _blocks(self):
        """How many separate stretches of rows were read (1 = no rows missing in between)."""
        ks = sorted(self._abs.get(self._cat, {}))
        return sum(1 for i, k in enumerate(ks) if i == 0 or k != ks[i - 1] + 1)

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
                "mixed": 0 in self._cats and len(self._cats) > 1, "gaps": self._gaps, "gap": self._gap_now,
                "runs": self._blocks() if self._cat in self._abs else len(self._seq.get(self._cat, [])),
                "value": self._value}

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

    def quest_screen(self, small, k):
        """A quest open at a trader (Celeste > QUESTS): read its title, its objectives' checkboxes and
        the player level from their fixed places, with recognition only (~0.3 s, no screen-wide
        text search). Returns {"id", "score", "text", "steps": [done?...], "level"} or None."""
        line = getattr(self.ocr, "line", None)
        if line is None:
            return None
        def crop(x0, y0, x1, y1):
            return small[int(y0 * k):int(y1 * k), int(x0 * k):int(x1 * k)]
        text = line(crop(850, 424, 1380, 456))
        t = _norm(text)
        if len(t) < 4:
            return None
        best = max(((fuzz.ratio(t, qn), qid) for qid, qn in self._qnames), default=(0, None))
        if best[0] < QUEST_MIN:
            return None
        # objectives: checkboxes in a column at x 1420-1436 (1080p) on the light panel, joined by a thin
        # line. A ticked box is filled grey (~100), an open one is light with a dark border. Rows where
        # both box edges are darker than the panel belong to a box.
        g = cv2.cvtColor(crop(1415, 120, 1441, 760), cv2.COLOR_BGR2GRAY)
        steps = []
        if g.size:
            xl, xr = int(5 * k), int(20 * k)
            edge = (g[:, xl] < 200) & (g[:, xr] < 200)
            y = 0
            while y < len(edge):
                if not edge[y]:
                    y += 1
                    continue
                y1 = y
                while y1 < len(edge) and edge[y1]:
                    y1 += 1
                if y1 - y >= int(9 * k):
                    mid = g[(y + y1) // 2 - int(2 * k):(y + y1) // 2 + int(3 * k), xl + int(3 * k):xr - int(3 * k)]
                    mid = np.delete(mid, np.s_[max(0, int(13 * k) - xl - int(3 * k) - 1):int(13 * k) - xl - int(3 * k) + 2], axis=1)
                    steps.append(bool(mid.size and float(np.median(mid)) < 180))
                y = y1
        lv = re.sub(r"\D", "", line(crop(1838, 12, 1884, 52)))
        return {"id": best[1], "score": float(best[0]), "text": text, "steps": steps,
                "level": int(lv) if lv and 1 <= int(lv) <= 200 else None}

    # ---------- entry point ----------
    def scan(self, img, auto=False, progress=None):
        """Auto-detect what is on screen. Returns a JSON-able dict for the UI. auto: called by the
        background watcher, so screens that are neither the stash nor the quest log return None."""
        self._auto = auto
        h, w = img.shape[:2]
        small = img if w <= 1920 else cv2.resize(img, (1920, int(h * 1920 / w)), interpolation=cv2.INTER_AREA)
        slots = self.find_slots(img)
        lines, fast = None, None
        if len(slots) >= 8:
            # most likely the stash: read only its header (STASH 287/292, top left) instead of the
            # whole screen, which takes several seconds
            k = small.shape[0] / 1080
            fast = self.fast_header(small, k)
            if fast:
                lines = []
            else:
                top = self._read(small[:int(300 * k), :int(700 * k)])
                if any(_norm(t) == "stash" for t, _, _ in top):
                    lines = top
        if lines is None and len(slots) < 8:
            qs = self.quest_screen(small, small.shape[0] / 1080)
            if qs:
                q = self._quests.get(qs["id"])
                new = [] if q else [qs["id"]]
                self._quests[qs["id"]] = {**(q or {}), **qs, "score": max(qs["score"], (q or {}).get("score", 0))}
                return {"kind": "quests", "quests": sorted(self._quests.values(), key=lambda q: -q["score"]),
                        "added": new, "level": qs["level"], "screen": qs["id"]}
        if lines is None:
            if auto:
                return None              # not the stash nor an open quest: stay cheap, no screen-wide OCR
            lines = self._read(small)
        quests = self.scan_quests(lines)
        if quests and len(slots) < 8:                            # a quest screen (reward icons are a few slots)
            new = [q["id"] for q in quests if q["id"] not in self._quests]
            for q in quests:
                if q["score"] > self._quests.get(q["id"], {}).get("score", 0):
                    self._quests[q["id"]] = q
            return {"kind": "quests", "quests": sorted(self._quests.values(), key=lambda q: -q["score"]), "added": new}
        # the stash list's visible area at 1080p: under the CATEGORY bar to above the hotkey bar. Only for
        # automatic reading: it sees every scroll step, so a cut-off row comes whole in a later frame;
        # F9 pages a whole screen at a time and would lose it
        kk = small.shape[0] / 1080
        stash_view = (int(262 * kk * h / small.shape[0]), int(944 * kk * h / small.shape[0])) if auto and (fast or any(_norm(t) == "stash" for t, _, _ in (lines or []))) else None
        page = self.scan_vault(img, view=stash_view, progress=progress)
        if page:
            k = small.shape[0] / 1080
            head, label = (fast[0], fast[1]) if fast else self.read_stash_header(lines, k)
            if fast and fast[2]:
                self._value = fast[2]
            cat = self.stash_category(small, label, k)
            if cat is None and head:
                cat = 0 if head["cap"] else -1                   # -1: some category, button not found
            if cat is not None:
                self._cat = cat
                c = self._cats.setdefault(cat, {"used": None, "cap": None, "slots": 0})
                if head:
                    c["used"], c["cap"] = head["used"], head["cap"]
            nums = None
            if auto and self._cat == 0 and head and head.get("cap"):
                nums = self.row_numbers(img, self._rows(page["slots"]), head["cap"])
            added = self._add_vault_page(page, nums)
            # what this picture holds, slot by slot (for the in-game marks)
            frame = [[*map(int, s["box"]), None if s.get("unknown") else s["id"], int(s["count"])] for s in page["slots"]]
            return {**self._vault_result(), "duplicate": not added, "frame": frame}
        if auto:
            return None
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

        def line(img):
            """Read a crop that holds one line of text (no text detection): the text, or ''."""
            if img is None or img.size == 0:
                return ""
            pad = cv2.copyMakeBorder(img, 6, 6, 12, 12, cv2.BORDER_REPLICATE)
            r = eng(pad, use_det=False, use_cls=False, use_rec=True)
            return " ".join(t for t in (getattr(r, "txts", None) or []) if t).strip()
        def rec(img):
            """Recognition only (no text detection) of a one-line crop: [(text, score)]."""
            pad = cv2.copyMakeBorder(img, 8, 8, 24, 24, cv2.BORDER_REPLICATE)
            r = eng(pad, use_det=False, use_cls=False, use_rec=True)
            return [(t, float(sc)) for t, sc in zip(getattr(r, "txts", None) or [], getattr(r, "scores", None) or []) if t]
        run.line, run.rec = line, rec
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
