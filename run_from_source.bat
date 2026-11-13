@echo off
REM Raider Logbook without an .exe: runs the app with Python (3.10 to 3.14).
REM One-time setup: install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
REM The first start downloads the libraries (a few minutes); later starts take seconds.
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)" || (echo This needs Python 3.10 to 3.14. & python --version & pause & exit /b 1)
if not exist .venv\Scripts\python.exe python -m venv .venv || (pause & exit /b 1)
if not exist .venv\installed2.ok (
  echo Setting up Raider Logbook. This takes a few minutes the first time...
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r overlay\requirements.txt || (echo Installing the libraries failed. Send the messages above. & pause & exit /b 1)
  .venv\Scripts\python -c "import webview, cv2, mss, rapidfuzz, pynput, onnxruntime; import rapidocr" || (echo A library does not load. Send the messages above. & pause & exit /b 1)
  echo ok> .venv\installed2.ok
)
echo Starting Raider Logbook... (the first start builds its data, give it up to a minute)
start "" .venv\Scripts\pythonw.exe overlay\overlay.py
