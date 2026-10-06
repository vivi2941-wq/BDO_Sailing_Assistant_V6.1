@echo off
cd /d "%~dp0"
call "%~dp0BUILD_EXE_CORE.cmd"
echo.
echo This window will remain open. Press any key only after reading the result.
pause >nul
exit /b
