@echo off
chcp 65001 >nul 2>&1
title 鲸鲸 - 体检
cd /d "%~dp0"

python -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [XX] 没找到可用的 Python，无法体检。请先运行 安装鲸鲸.bat。
    pause
    exit /b 1
)

python "scripts\doctor.py" %*
set "CODE=%errorlevel%"
echo.
pause
exit /b %CODE%
