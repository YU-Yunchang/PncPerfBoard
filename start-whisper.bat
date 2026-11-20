@echo off
cd /d "%~dp0"
py -3.11 start.py --with-whisper %*
if errorlevel 1 (
  echo Install 64-bit Python 3.11 with the Python Launcher, then retry.
  pause
  exit /b 1
)
