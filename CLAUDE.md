# Raider Logbook – notes for Claude

Companion app for ARC Raiders: vault verdicts (sell/keep/recycle), quests by map with spots on a real
map, loot routes, skill plans, and a screen scanner for the quest screen and the stash. The user is
Czech; reply in Czech. UI text stays English.

## Layout
- `web/app.html` – the whole UI (one page, vanilla JS). Data is injected by `build.py`.
- `build.py` – merges sources into `dist/index.html` / `dist/artifact.html`. Full build:
  `python build.py <arcraiders-data checkout>`; page-only rebuild: `python build.py --base overlay/assets/base.json --out DIR`.
- `overlay/overlay.py` – launcher (logging, data-only update check), then `overlay/app.py` (pywebview
  window, hotkeys F8/F9/Shift+F9, Api for the page).
- `overlay/scanner.py` – OCR (rapidocr 3.x, falls back to rapidocr_onnxruntime) + icon matching.
- `overlay/capture.py` – finds the ARC Raiders window (PioneerGame*.exe or title "ARC Raiders") and
  captures it: DXGI (dxcam) first, GDI (mss) fallback, warns when a capture equals the previous one.
- `overlay/updater.py` – downloads only data files listed in `update/manifest.json` (html/json),
  never code. Bump `version` in the manifest on every pushed page/data change.
- `sources/` – hand-kept data (skill builds, guide links, map alignment). arcraidershub.com forbids
  scraping: never fetch it automatically.
- `tests/test_real.py` (real 1920x1080 screenshots in `tests/real/`) and `tests/test_scanner.py`
  (synthetic; needs the RaidTheory data checkout).

## Running on the user's PC
- The unsigned .exe is blocked by Defender/browsers, so the user runs `run_from_source.bat`
  (Python 3.14, venv in `.venv`).
- User data, logs and scans: `%APPDATA%\RaiderLogbook\` → `app.log`, `crash.log`, `scans\scan-*.png`
  (every scan is saved), `learned\` (icons the user taught), `page\index.html`, `cache\`.

## Scanner capture notes
- The user's PC: laptop (2560x1600 at x=-2560) + primary external monitor (1920x1080 at 0,0), game on
  the primary. mss lists monitors as [laptop, primary], DXGI/dxcam as [primary, laptop]: never map
  between them by index. `capture._dxgi_output_for` picks the DXGI output by its DesktopCoordinates.
  (Before this fix DXGI captured the laptop screen, so every scan was the same desktop picture.)
- Verified live on 2026-11-13: `method: dxgi`, correct stash frame, scanner finds the vault.
- Still unexplained: the app crashed once while pressing F9 repeatedly in the stash (see `crash.log`).
- Debug a scan: `python overlay/scanner.py <scan.png> --debug out.png`; then run `tests/test_real.py`.
- Stash completeness: the scanner OCRs the header under "STASH" (`read_stash_header`): "287/292" on the
  all-items view, just "223" when a category filter is on. `stash_category` finds the selected category
  button (white disc left of the grid; 0 = all). Slots are counted per category; scanning all-items and
  a category in one session counts twice (`mixed`, the UI warns). Page overlap compares slot pictures
  (`ROW_SAME`), not recognised names; rows are grouped by y with a tolerance.
- Badges in a slot's top-right corner (eye = marked, diamond = quest item) are blanked before matching.
- Every recognised item keeps its look-alike groups ("variants"); "Wrong?" in the Scan tab calls
  `reassign(from_id, key, to_id)`, which fixes the scan and saves the picture to `learned\` (weapons
  with attachments look like other weapons). Short categories (3 augments): the stash grid is chosen as the cluster with the most clearly filled slots, not the biggest one (the empty backpack grid is bigger).
- While a scan runs, `Api._state` calls `window.onScanState(stage)` → orange "Scanning…" bar on top.
- ~377 items (mostly newer ones) have no icon in RaidTheory's set (refs.npz), so they come back as
  "Not recognised". MetaForge preview icons were tried as extra references and match the in-game slots
  poorly (similarity ~0.1), so unknowns are taught in the app (saved in `%APPDATA%\RaiderLogbook\learned`).

## Conventions
Commit with "Raider Logbook <noreply@anthropic.com>" as author; push to `main` of
github.com/SiLo-xxxcxxcxyxxcyyy/riader-logbook. Rebuild `dist/` after UI changes and bump the manifest.
- Weapons/mods: the icon gives the weapon (`family()`), the I-IV numeral bottom-right gives the tier
  (`read_tier`, templates in `assets/numerals.npz` cut from a real 1080p stash). Whole weapons lack
  references (Torrente, Canto, Stitcher, Bantam): teach one tier and all tiers follow.
- Corrections (Save / Wrong?) are logged as `correction: from (key) -> to` in app.log.
