@echo off
setlocal
cd /d "%~dp0"
title NumVerify Pro Live Intelligence

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=py"
  goto :python_ready
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo During installation, check "Add Python to PATH".
  pause
  exit /b 1
)
set "PYTHON_CMD=python"

:python_ready

if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 goto :failed
)

echo Installing or checking required packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

if not exist "nanpa_prefixes.sqlite3" (
  echo.
  echo Building the offline NANPA prefix database...
  ".venv\Scripts\python.exe" update_offline_database.py
  if errorlevel 1 (
    echo The NANPA database could not be downloaded. The portal will still
    echo start with basic libphonenumber validation only.
  )
)

echo.
if exist ".env" (
  echo Live lookup settings detected. The portal will use the configured provider.
) else (
  echo Offline fallback mode. Run configure_live_lookup.bat for current carrier data.
)
echo.
echo Starting NumVerify Pro at http://127.0.0.1:8000
echo Keep this window open while using the portal.
echo Press Ctrl+C here to stop it.
echo.
".venv\Scripts\python.exe" app.py
goto :end

:failed
echo.
echo Setup could not be completed. Check your internet connection and Python installation.
pause
exit /b 1

:end
endlocal
