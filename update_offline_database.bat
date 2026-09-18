@echo off
setlocal
cd /d "%~dp0"
title Update NumVerify Offline Database

if not exist ".venv\Scripts\python.exe" (
  echo Please run start_portal.bat once before updating the database.
  pause
  exit /b 1
)

echo Downloading the latest official NANPA assignment report...
".venv\Scripts\python.exe" update_offline_database.py
if errorlevel 1 (
  echo.
  echo Database update failed. Check your internet connection and try again.
  pause
  exit /b 1
)

echo.
echo Update complete. Restart the portal to load the refreshed database.
pause
endlocal
