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

## Open problem (start here)
Vault scanning on the user's PC fails: scans come out identical to the previous one, and the last one
found "1 item slot (40px), 110 text lines" while the stash was open (real slots are ~97px at 1920x1080).
Earlier, 15 scans of different stash pages were all the same image (GDI returned a frozen frame).
DXGI capture via dxcam was added but not verified on the user's machine. Steps:
1. Read `%APPDATA%\RaiderLogbook\app.log` / `crash.log` and the newest `scans\*.png`.
2. Check whether `dxcam` is installed in `.venv` and whether capture info says `method: dxgi` or a DXGI error.
3. Run `python overlay/scanner.py <scan.png>` on the saved scans; fix capture.py/scanner.py; run
   `tests/test_real.py`.
Also unexplained: the app crashed once while pressing F9 repeatedly in the stash (crash logging now exists).

## Conventions
Commit with "Raider Logbook <noreply@anthropic.com>" as author; push to `main` of
github.com/SiLo-xxxcxxcxyxxcyyy/riader-logbook. Rebuild `dist/` after UI changes and bump the manifest.
