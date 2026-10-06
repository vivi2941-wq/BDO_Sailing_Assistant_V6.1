@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "BUILD_LOG=%~dp0build_exe.log"
echo Builder started at %date% %time% > "%BUILD_LOG%"
echo Working folder: %CD% >> "%BUILD_LOG%"
echo. >> "%BUILD_LOG%"

call :build >> "%BUILD_LOG%" 2>&1
set "BUILD_RESULT=%ERRORLEVEL%"
type "%BUILD_LOG%"
echo.
if "%BUILD_RESULT%"=="0" (
    echo BUILD SUCCEEDED: dist\BDO_Sailing_Assistant_V5.90.5
) else (
    echo BUILD FAILED. Please send build_exe.log back for diagnosis.
)
exit /b %BUILD_RESULT%

:build
if exist ".build-venv\Scripts\python.exe" goto :venv_ready
where py >nul 2>&1
if not errorlevel 1 (
    py -3.11 -m venv .build-venv
    if not errorlevel 1 goto :venv_ready
    py -3.12 -m venv .build-venv
    if not errorlevel 1 goto :venv_ready
    py -3 -m venv .build-venv
    if not errorlevel 1 goto :venv_ready
)
where python >nul 2>&1
if not errorlevel 1 (
    python -m venv .build-venv
    if not errorlevel 1 goto :venv_ready
)
echo [ERROR] Python was not found. Install 64-bit Python 3.11 or 3.12 and enable Add Python to PATH.
exit /b 10

:venv_ready
".build-venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 20
".build-venv\Scripts\python.exe" -m pip install -r requirements-public.txt
if errorlevel 1 exit /b 21
".build-venv\Scripts\python.exe" release_tools\audit_public_release.py .
if errorlevel 1 exit /b 30
".build-venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean BDO_Sailing_Assistant.spec
if errorlevel 1 exit /b 40
exit /b 0
