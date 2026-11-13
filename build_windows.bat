@echo off
REM Builds RaiderLogbook.exe (folder release\RaiderLogbook). Needs Python 3.11+ and git on PATH.
setlocal
python -m pip install -r overlay\requirements.txt || exit /b 1
REM Compile PyInstaller's bootloader ourselves (needs Visual Studio C++ build tools). The prebuilt
REM bootloader is shared with a lot of malware and is the usual reason antivirus flags PyInstaller apps.
if not exist build\pyinstaller-src git clone --depth 1 --branch v6.22.3 https://github.com/pyinstaller/pyinstaller.git build\pyinstaller-src || exit /b 1
pushd build\pyinstaller-src\bootloader
python ./waf all --target-arch=64bit || (popd & exit /b 1)
popd
python -m pip install --no-cache-dir .\build\pyinstaller-src || exit /b 1
python -c "import PyInstaller, os; b=os.path.join(os.path.dirname(PyInstaller.__file__),'bootloader','Windows-64bit-intel','runw.exe'); print('bootloader', b, os.path.getsize(b))" || exit /b 1
if not exist data\arcraiders-data git clone --depth 1 https://github.com/RaidTheory/arcraiders-data.git data\arcraiders-data || exit /b 1
python build.py data\arcraiders-data || exit /b 1
pyinstaller --noconfirm --windowed --noupx --name RaiderLogbook --version-file version_info.txt --distpath release ^
  --add-data "dist\index.html;ui" --add-data "overlay\assets;assets" ^
  --add-data "web\app.html;web" --add-data "sources;sources" --add-data "update;update" --paths . --hidden-import build ^
  --collect-all rapidocr --collect-all onnxruntime --collect-all webview --collect-all pythonnet --collect-all clr_loader ^
  overlay\overlay.py || exit /b 1
echo.
echo Done: release\RaiderLogbook\RaiderLogbook.exe
