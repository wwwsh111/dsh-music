@echo off
setlocal
cd /d "%~dp0"
title DSH Music 1.0 Installer

echo.
echo DSH Music 1.0 - one-click installer
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo Install did not finish. Exit code: %RC%
  echo Read the error above, fix it, then run this file again.
) else (
  echo Finished.
)
echo.
pause
exit /b %RC%
