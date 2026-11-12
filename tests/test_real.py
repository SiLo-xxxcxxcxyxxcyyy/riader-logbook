"""Checks against real game screenshots (1920x1080) in tests/real/."""
import os, sys
import cv2
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "overlay"))
from scanner import Scanner  # noqa: E402

sc = Scanner()
q = sc.scan(cv2.imread(os.path.join(ROOT, "tests", "real", "quest_1920.png")))
assert q.get("kind") == "quests" and q["quests"][0]["id"] == "source_of_the_contamination", q
v = sc.scan(cv2.imread(os.path.join(ROOT, "tests", "real", "vault_1920.png")))
assert v.get("kind") == "vault", v
slots = sum(i["count"] for i in v["items"])
assert len(sc.find_slots(cv2.imread(os.path.join(ROOT, "tests", "real", "vault_1920.png")))) == 24
ids = {i["id"] for i in v["items"]}
for expected in ("medium_shield", "renegade_ii", "hullcracker_iv", "anvil_i", "anvil_ii", "ferro_iii"):
    assert expected in ids, (expected, ids)
print(f"real screenshots OK: quest recognised, vault {len(v['items'])} item types / {slots} pieces")
