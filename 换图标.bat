@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv_iso\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [X] 找不到 python：%PY%
  pause
  exit /b 2
)

set "IMG=%~1"
if "%IMG%"=="" set "IMG=%~dp0assets\icon\鲸鲸-原图.jpg"

echo [1/2] 用这张图生成图标：%IMG%
"%PY%" "%~dp0tools\make_icon.py" "%IMG%"
if errorlevel 1 (
  echo [X] 生成失败
  pause
  exit /b 1
)

echo [2/2] 刷新资源管理器图标缓存
ie4uinit.exe -show
echo.
echo 完成。桌面和窗口图标都已更新（桌面若还显示旧图，按 F5 或注销重登）。
echo.
pause
