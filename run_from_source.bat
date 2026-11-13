@echo off
REM Raider Logbook without an .exe: runs the app with Python.
REM One-time setup: install Python 3.12 from https://www.python.org/downloads/
REM (tick "Add python.exe to PATH"). The first start then downloads the libraries (a few minutes).
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found. Install Python 3.12 from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  echo First start: setting up Raider Logbook. This takes a few minutes...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r overlay\requirements.txt || (echo Installing the libraries failed. & pause & exit /b 1)
)
.venv\Scripts\python -c "import webview, cv2, mss, rapidocr_onnxruntime, rapidfuzz, pynput" || (echo A library is missing. Delete the .venv folder and start this again. & pause & exit /b 1)
echo Starting Raider Logbook... (the first start builds its data, give it up to a minute)
start "" .venv\Scripts\pythonw.exe overlay\overlay.py
