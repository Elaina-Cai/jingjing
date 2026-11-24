@echo off
chcp 65001 >nul 2>&1
title 鲸鲸 - 平台接入（统一五步）
cd /d "%~dp0"

python -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [XX] 没找到可用的 Python，请先运行 安装鲸鲸.bat。
    pause
    exit /b 1
)

python "scripts\platform_setup.py" %*
set "CODE=%errorlevel%"
pause
exit /b %CODE%
