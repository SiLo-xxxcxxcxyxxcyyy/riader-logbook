# Raider Logbook

A helper for ARC Raiders. It tells you what to sell from your vault, which map to drop on for your quests, and where to find the items you're missing. It runs in a browser or as an always-on-top desktop overlay that can read your quest log and vault from the screen.

## Features
- **Help** tab explains every feature, the hotkeys and how to get started.
- **Lobby** shows running and upcoming map events, and a tactical map from the wiki's labelled map images (by Cartotect) with MetaForge's community markers lined up on top: your quest spots are circled automatically, with extracts, hatches, key rooms, caches, ARC and nature spawns as layers. It also suggests a **loot route**: the densest clusters of spawn spots for what you need (your shopping list, a nature item, caches, probes, security lockers or a loot type), ordered by distance and ending at the nearest extract.
- The Lobby also ranks maps by how many of your available quests they have (plus a bonus for ARC drops you're missing), lists each quest's objectives, and lets you circle spots on the map.
- **Vault** gives every item a verdict (Keep, Sell, Recycle, Learn, Gear, Material) with the reason.
- **Quests** tracks your progress through the quest chain. Each quest has **How to do it**: where it's marked on the map, the wiki walkthrough (captioned screenshots, notes) and links to the full guides, plus **Show on map**.
- **Shopping list** totals what you still need for quests, workshop upgrades and projects, minus what you own.
- **Items** shows where each item loots, which ARC drop it and on which maps, who sells it, and what recycles into it.
- **Skills** shows the Frozen Trail 2.0 skill tree (48 skills) and four leveling plans (PvE, PvP solo, PvP squad, PvPvE): set how many skill points you have and it tells you where the next ones go.
- **Scan** (desktop overlay only) reads the quest log and vault pages from your screen.

## Desktop overlay (Windows)

Hotkeys:

| Key | Action |
| --- | --- |
| F8 | Show or hide the overlay |
| F9 | Scan the screen: quest log or vault page, detected automatically |
| Shift+F9 | Start a new vault scan, then press F9 on each vault page |

Run the game in **borderless windowed** mode so the overlay can stay on top.

**Always up to date:** on every launch the overlay rebuilds its data in the background from MetaForge and the ARC Raiders Wiki and reloads when done (about 10–15 s the first time). Downloads are cached in `%APPDATA%\RaiderLogbook\cache` and refreshed when older than their limit: events 3 h, items and quests 12 h, map markers and wiki 24 h. Start with `--no-update` to skip it.

The scanner only takes screenshots, like OBS or Discord. It does not read game memory or files and never sends input to the game.

### Run from source
```
pip install -r overlay/requirements.txt
git clone --depth 1 https://github.com/RaidTheory/arcraiders-data.git data/arcraiders-data
python build.py data/arcraiders-data
python overlay/overlay.py
```

### Build the .exe
Run `build_windows.bat`. The app lands in `release\RaiderLogbook\RaiderLogbook.exe`.
The GitHub Actions workflow `.github/workflows/build-windows.yml` does the same in the cloud and uploads the build as an artifact.

## Browser version
After `python build.py ...`, open `dist/index.html`. Progress is stored in the browser's local storage. Use Vault → Backup to move it between devices.

## How the scanner works
- **Quest log:** OCR (RapidOCR) reads all text on screen and fuzzy-matches lines against the 100 known quest titles. Recognised quests become your active quests and everything before them is marked done.
- **Vault:** it finds the grid of square item slots, matches each slot against the in-game icon set (`images/items_ingame`, 417 icons) using colour and edge features, and reads stack counts from the slot corners with OCR. Pages already scanned in the same session are skipped, so scanning the same page twice doesn't double your counts.
- Test it with `python tests/test_scanner.py data/arcraiders-data`. The test builds fake screenshots, so it checks the pipeline but not the real game UI.

### Known limits
- Weapon and mod tiers (I–IV) and some keys have near-identical icons. Those matches are shown in red for you to check.
- Slot detection and count position are tuned on synthetic screenshots. Real screenshots may need tuning in `overlay/scanner.py`.
- 164 items have no in-game icon in the data set and can't be scanned yet. Add them by hand.
- The data has no coordinates for quest locations or blueprint spawns.

## Map alignment
`tools/align_maps.py` lines up MetaForge's marker coordinates with the wiki's labelled map images: it reads the place names on each image with OCR, pairs them with named markers (extracts, hatches, key rooms) and fits scale and offset with RANSAC. Results are in `sources/map_align.json` (median error 30–57 px on 4096–5120 px images). Riven Tides and Pendola Pass have no labelled image that matches yet and fall back to a marker-only drawing. Re-run the script when a map changes.

## Data
`build.py` merges four community sources and caches the downloads in `data/cache` (use `--refresh` to update, `--offline` to build from the cache):

| Source | Used for |
| --- | --- |
| [MetaForge](https://metaforge.app/arc-raiders) API | Current items, quests, workshop levels, farming tips (covers Frozen Trail / Pendola Pass) |
| [ARC Raiders Wiki](https://arcraiders.wiki) (CC BY-SA 4.0) | Quest chains and maps, 2.0 skill list, Pendola Pass map |
| [MetaForge](https://metaforge.app/arc-raiders) map data + events | ~43,000 map markers (containers, nature, caches, quest spots, extracts), event schedule |
| [Arc Raiders Hub](https://arcraidershub.com/guides) | Build orders (`sources/skill_builds.json`) and guide links (`sources/hub_guides.json`), both kept by hand because the site's terms forbid scraping |
| [RaidTheory/arcraiders-data](https://github.com/RaidTheory/arcraiders-data) (MIT) | Recipes, recycling, ARC drops, trades, projects, map images, scanner icons |

The RaidTheory repo stopped updating in August 2026, so recipes, recycling and ARC drops for Frozen Trail items (and the new Bully, Skulker and Hydra) are missing until a source publishes them.
