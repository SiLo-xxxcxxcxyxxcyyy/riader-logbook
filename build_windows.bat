@echo off
REM Builds RaiderLogbook.exe (folder release\RaiderLogbook). Needs Python 3.11+ and git on PATH.
setlocal
python -m pip install -r overlay\requirements.txt || exit /b 1
REM Build PyInstaller's bootloader from source: the prebuilt one is shared with a lot of malware
REM and is the usual reason antivirus flags PyInstaller apps.
set PYINSTALLER_COMPILE_BOOTLOADER=1
python -m pip install --no-binary pyinstaller --no-cache-dir "pyinstaller>=6.10" || exit /b 1
if not exist data\arcraiders-data git clone --depth 1 https://github.com/RaidTheory/arcraiders-data.git data\arcraiders-data || exit /b 1
python build.py data\arcraiders-data || exit /b 1
pyinstaller --noconfirm --windowed --noupx --name RaiderLogbook --version-file version_info.txt --distpath release ^
  --add-data "dist\index.html;ui" --add-data "overlay\assets;assets" ^
  --add-data "web\app.html;web" --add-data "sources;sources" --add-data "update;update" --paths . --hidden-import build ^
  --collect-all rapidocr_onnxruntime --collect-all webview --collect-all pythonnet --collect-all clr_loader ^
  overlay\overlay.py || exit /b 1
echo.
echo Done: release\RaiderLogbook\RaiderLogbook.exe
