@echo off

chcp 936 >nul

setlocal

cd /d "%~dp0"
rem 2026-11-02: this machine sets HTTP_PROXY; loopback must bypass it
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

set "PYW=%~dp0.venv_iso\Scripts\pythonw.exe"
rem 2026-10-30：提示词形状 v2（system = 身份+日级状态，user = 此刻+分界线+原文）。
rem 想退回旧形状就删掉下面这一行、再双击本文件；两种形状都过门禁（447 条）。
set "PROMPT_SHAPE_V2=1"



rem 1) 引擎不在就先拉起来（哨兵先清掉，不然拉起来也会自己退）

netstat -ano -p TCP | findstr "127.0.0.1:39871" | findstr "LISTENING" >nul

if errorlevel 1 (

  echo [1/3] 引擎没在跑，先起主程序…

  if exist "logs\all.stop" del /q "logs\all.stop"

  if exist "logs\watchdog.stop" del /q "logs\watchdog.stop"

  start "" "%PYW%" "%~dp0scripts\supervisor.py"
start "" "%PYW%" "%~dp0scripts\desk_supervisor.py"

  ping -n 7 127.0.0.1 >nul

) else (

  echo [1/3] 引擎已在跑
  echo      要重启请双击「重启鲸鲸.bat」：它会写全三个哨兵、等进程自己退（别直接关窗口）

)



rem 2) 采集器（只读；窗口关掉之后它自己退）

echo [2/3] 起采集器…

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object {$_.CommandLine -like '*collector.py*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>nul

start "" "%PYW%" "%~dp0console\collector.py"

ping -n 3 127.0.0.1 >nul

rem 采集器起来了吗？只看端口不够：进程可能在、端口没开（2026-10-30 就是因为 import 顺序
rem 死在 ModuleNotFoundError，端口一直空着，面板只好报 Failed to fetch）
powershell -NoProfile -Command "$d='data\console'; New-Item -ItemType Directory -Force -Path $d | Out-Null; $ok=$false; for($i=0;$i -lt 20;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:39872/health'; if($r.StatusCode -eq 200){ $ok=$true; break } } catch {}; Start-Sleep -Milliseconds 700 }; if(-not $ok){ Add-Content -Encoding UTF8 (Join-Path $d 'collector.err') ('['+(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')+'] launcher: 采集器 /health 20 秒没应答（127.0.0.1:39872）'); exit 1 }"

if errorlevel 1 echo [2/3] 采集器没应答，原因记在 data\console\collector.err



rem 3) 窗口

echo [3/3] 开窗口…

"%PYW%" "%~dp0console\open_window.py"

exit /b 0

