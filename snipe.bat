@echo off
REM teesniper launcher for Windows.
REM Double-click to start, or run from cmd:  snipe.bat snipe -d 2026-09-15
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating virtual environment...
  python -m venv .venv || goto :nopython
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
)

if "%~1"=="" (
  .venv\Scripts\python.exe -m teesniper snipe
) else (
  .venv\Scripts\python.exe -m teesniper %*
)
echo.
pause
exit /b

:nopython
echo.
echo Python was not found. Install Python 3.11+ from python.org
echo and make sure "Add python.exe to PATH" is ticked during setup.
echo.
pause
