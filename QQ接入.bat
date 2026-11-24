@echo off
chcp 65001 >nul 2>&1
title 鲸鲸 - QQ 接入（统一五步）
cd /d "%~dp0"
echo.
echo 已并入统一入口：平台接入 --platform qq（等价于原来的 QQ接入）
echo.
python "scripts\platform_setup.py" --platform qq %*
set "CODE=%errorlevel%"
echo.
pause
exit /b %CODE%
