@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 2026-11-02: this machine sets HTTP_PROXY; loopback must bypass it
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
rem 启动鲸鲸：已经在跑就直接退出；没跑就交给监管器（它拉主程序，主程序拉引擎）。
rem 要重启用「重启鲸鲸.bat」——它会先写 logs\all.stop 并等进程真退干净。
set "PORT=39871"
netstat -ano | findstr "127.0.0.1:%PORT%" | findstr "LISTENING" >nul && (
  echo [OK] 鲸鲸已经在跑（引擎端口 %PORT% 在听），不用再启动。
  timeout /t 2 >nul
  exit /b 0
)
if exist "logs\all.stop" del "logs\all.stop" >nul 2>&1
if exist "logs\watchdog.stop" del "logs\watchdog.stop" >nul 2>&1
echo 启动中……爬起来没有看 logs\supervisor.out 和 logs\loop_engine.out
start "" "%~dp0.venv_iso\Scripts\pythonw.exe" "%~dp0scripts\supervisor.py"
timeout /t 6 >nul
netstat -ano | findstr "127.0.0.1:%PORT%" | findstr "LISTENING" >nul && (
  echo [OK] 引擎已经在跑。
) || (
  echo [慢] 还没起来，再等十几秒；一直起不来就看 logs\supervisor.out
)
timeout /t 2 >nul
