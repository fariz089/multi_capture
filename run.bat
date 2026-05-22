@echo off
REM ─────────────────────────────────────────────────────────────────
REM Multi-Capture Launcher (Windows)
REM ─────────────────────────────────────────────────────────────────
REM Double-click file ini buat jalanin GUI. Otomatis pakai venv kalau
REM ada di .venv/, fallback ke python global.

cd /d "%~dp0"

if exist .venv\Scripts\python.exe (
    set PYTHON=.venv\Scripts\python.exe
) else (
    set PYTHON=python
)

%PYTHON% multi_capture.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo === ERROR ===
    echo Cek log di atas. Pastikan sudah run:
    echo   pip install -r requirements.txt
    echo   playwright install chromium
    echo.
    pause
)
