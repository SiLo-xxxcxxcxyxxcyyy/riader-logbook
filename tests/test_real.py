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

# second stash view (materials tab): items without a game icon render are recognised from wiki /
# MetaForge art (DINOv2 matcher); without the matcher they come back as "unknowns"
sc.reset_vault_session()
m = sc.scan(cv2.imread(os.path.join(ROOT, "tests", "real", "vault_materials_1920.png")))
seeds = next(i["count"] for i in m["items"] if i["id"] == "assorted_seeds")
assert seeds == 463, seeds
got = {i["id"]: i["count"] for i in m["items"]}
if sc._dino:
    assert got.get("arc_data_module") == 6 and got.get("raider_logbook") == 1, got
    assert len(m["unknowns"]) <= 1, m["unknowns"]
    print("materials tab OK: 463 seeds, ARC Data Module x6 and Raider Logbook from wiki art")
else:
    assert sorted(u["count"] for u in m["unknowns"]) == [1, 6], m["unknowns"]
    print("materials tab OK (pixel matcher only): 463 seeds, unknown items 6 + 1")
