@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" copy /Y ".env.example" ".env" >nul

echo Opening the private live-lookup settings file.
echo Add either your Telnyx key or your Twilio credentials, then save and close.
echo Restart NumVerify Pro after saving.
echo.
start "" notepad ".env"

endlocal
