@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
python sod_save_editor.py
pause
