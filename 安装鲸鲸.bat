@echo off
chcp 65001 >nul 2>&1
title 鲸鲸 - 安装向导
cd /d "%~dp0"

echo.
echo ============================================
echo   鲸鲸 · 一键安装向导
echo ============================================
echo.

python -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [XX] 没找到可用的 Python。
    echo      请先安装 Python 3.12 或更高版本：
    echo      https://www.python.org/downloads/
    echo      安装时务必勾选 "Add python.exe to PATH"。
    echo.
    echo      即将打开下载页面...
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

python "scripts\setup_wizard.py" %*
set "CODE=%errorlevel%"
echo.
if not "%CODE%"=="0" (
    echo [!] 向导退出码 %CODE%。请把上面最后几行信息发给懂技术的人。
)
pause
exit /b %CODE%
