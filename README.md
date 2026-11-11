# Raider Logbook

A helper for ARC Raiders. It tells you what to sell from your vault, which map to drop on for your quests, and where to find the items you're missing. It runs in a browser or as an always-on-top desktop overlay that can read your quest log and vault from the screen.

## Features
- **Lobby** ranks maps by how many of your available quests they have (plus a bonus for ARC drops you're missing), lists each quest's objectives, and lets you circle spots on the map.
- **Vault** gives every item a verdict (Keep, Sell, Recycle, Learn, Gear, Material) with the reason.
- **Quests** tracks your progress through the quest chain.
- **Shopping list** totals what you still need for quests, workshop upgrades and projects, minus what you own.
- **Items** shows where each item loots, which ARC drop it and on which maps, who sells it, and what recycles into it.
- **Scan** (desktop overlay only) reads the quest log and vault pages from your screen.

## Desktop overlay (Windows)

Hotkeys:

| Key | Action |
| --- | --- |
| F8 | Show or hide the overlay |
| F9 | Scan the screen: quest log or vault page, detected automatically |
| Shift+F9 | Start a new vault scan, then press F9 on each vault page |

Run the game in **borderless windowed** mode so the overlay can stay on top.

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

## Data
Game data comes from [RaidTheory/arcraiders-data](https://github.com/RaidTheory/arcraiders-data) (MIT), the data behind arctracker.io. Re-run `build.py` to update.
