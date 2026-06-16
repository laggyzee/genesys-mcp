@echo off
REM provision_users.bat - double-click launcher (Windows).
REM Opens cmd and runs provision_users.py in interactive mode.
setlocal
set "REPO=%~dp0.."
pushd "%REPO%"

set "VENVPY=%REPO%\.venv\Scripts\python.exe"
if exist "%VENVPY%" (
  "%VENVPY%" scripts\provision_users.py --interactive
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 scripts\provision_users.py --interactive
  ) else (
    python scripts\provision_users.py --interactive
  )
)

popd
echo.
pause
