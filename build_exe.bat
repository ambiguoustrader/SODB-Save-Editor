@echo off
setlocal
cd /d "%~dp0"

echo === Shadows of Doubt - SODB Save Editor build ===
echo Working directory: %CD%
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ and enable "Add Python to PATH".
  pause
  exit /b 1
)

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-dev.txt

if exist "icon.ico" (
  python -m PyInstaller --clean --noconfirm --onefile --windowed --name "SODB_Save_Editor" --icon "icon.ico" --add-data "icon.ico;." sod_save_editor.py
) else (
  python -m PyInstaller --clean --noconfirm --onefile --windowed --name "SODB_Save_Editor" sod_save_editor.py
)

if exist "dist\SODB_Save_Editor.exe" (
  echo.
  echo Done: dist\SODB_Save_Editor.exe
) else (
  echo.
  echo Build failed. Check output above.
  pause
  exit /b 1
)

pause
