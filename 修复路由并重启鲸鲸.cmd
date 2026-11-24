@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv_iso\Scripts\python.exe" ".tmp_outside_run.py"
set "exit_code=%ERRORLEVEL%"
echo.
echo 退出码: %exit_code%
pause
exit /b %exit_code%
