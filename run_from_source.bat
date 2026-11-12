@echo off
REM Runs Raider Logbook straight from the source code, without the .exe.
REM Needs Python 3.11+ from python.org (tick "Add python.exe to PATH" when installing).
setlocal
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv || (echo Python not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
  .venv\Scripts\python -m pip install -r overlay\requirements.txt || (pause & exit /b 1)
)
if not exist overlay\assets\base.json (
  if not exist data\arcraiders-data git clone --depth 1 https://github.com/RaidTheory/arcraiders-data.git data\arcraiders-data
  .venv\Scripts\python build.py data\arcraiders-data || (pause & exit /b 1)
)
start "" .venv\Scripts\pythonw overlay\overlay.py
