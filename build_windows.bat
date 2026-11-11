@echo off
REM Builds RaiderLogbook.exe (folder release\RaiderLogbook). Needs Python 3.11+ and git on PATH.
setlocal
python -m pip install -r overlay\requirements.txt pyinstaller || exit /b 1
if not exist data\arcraiders-data git clone --depth 1 https://github.com/RaidTheory/arcraiders-data.git data\arcraiders-data || exit /b 1
python build.py data\arcraiders-data || exit /b 1
pyinstaller --noconfirm --windowed --name RaiderLogbook --distpath release ^
  --add-data "dist\index.html;ui" --add-data "overlay\assets;assets" ^
  --add-data "web\app.html;web" --add-data "sources;sources" --paths . --hidden-import build ^
  --collect-all rapidocr_onnxruntime overlay\overlay.py || exit /b 1
echo.
echo Done: release\RaiderLogbook\RaiderLogbook.exe
