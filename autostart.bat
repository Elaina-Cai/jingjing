@echo off
chcp 65001 >nul 2>&1
title 鲸鲸 - 微信私人助理
cd /d "%~dp0"
rem 2026-11-02: this machine sets HTTP_PROXY; loopback must bypass it
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
set "PORT=39871"

rem --- 端口门：AI 引擎占着 39871 就说明这套已经在跑了，别再起第二套 ---
netstat -ano | findstr "127.0.0.1:%PORT%" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [gate] 端口 %PORT% 已在监听：这套已经在跑，本窗口退出。
    exit /b 0
)

rem --- 日志轮转：给不走 logging 的几条日志补上限（失败不阻断） ---
if exist "%~dp0.venv_iso\Scripts\python.exe" "%~dp0.venv_iso\Scripts\python.exe" -m core.infra.log_rotate >nul 2>&1

echo ════════════════════════════════════════════
echo   鲸鲸 · 微信私人助理  启动中...
echo ════════════════════════════════════════════
echo.

echo [1/4] 检查微信账号配置...
findstr /C:"WECHAT_YINYUE_WXID=" .env >nul 2>&1
if errorlevel 1 (
    echo     未检测到账号配置，运行自动检测...
    %~dp0.venv_iso\Scripts\python.exe scripts\get_wechat_info.py --set-env
    if errorlevel 1 (
        echo     [!] 自动检测失败，请手动运行: python scripts/get_wechat_info.py --set-env
        pause
        exit /b 1
    )
) else (
    echo     微信账号已配置
)
echo.

echo [2/4] 检查 Ollama 服务...
netstat -ano | findstr ":11434" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo     Ollama 未运行，尝试启动...
    start "" /B ollama serve
    timeout /t 3 /nobreak >nul
) else (
    echo     Ollama 已运行
)
echo.

echo [3/4] 检查微信密钥配置...
findstr /C:"WECHAT_KEY=" .env >nul 2>&1
if errorlevel 1 (
    echo     [!] .env 中未找到 WECHAT_KEY
    echo     请先运行: python start.py  进行首次设置
    echo.
    pause
    exit /b 1
)
echo     微信密钥已配置
echo.

echo [4/4] 起监管器（调度器 + AI 引擎由它带起来）
echo     没有界面了：看 logs\loop_engine.out / logs\yinyue.log
echo     停止方式：创建 logs\watchdog.stop（本轮退出后不再重启）
echo     崩溃自恢复：已启用（退出自动拉起，连续失败退避到 60 秒）
echo ════════════════════════════════════════════
echo.

set "FAILS=0"
:run
rem 2026-11-04（E-55）：哨兵没有时效 → 一个忘删的文件让她静音一下午。
rem 超过 2 小时就当"忘了删"：清掉、继续跑（阈值与 scripts\watchdog_yinyue.py 的 PAUSE_MAX_HOURS 一致）。
if exist "logs\watchdog.stop" powershell -NoProfile -Command "$p='logs\watchdog.stop'; if((Test-Path $p) -and ((Get-Date)-(Get-Item $p).LastWriteTime).TotalHours -ge 2){Remove-Item $p -Force; Write-Host '[watchdog] sentinel older than 2h -> cleared, resuming'}"
if exist "logs\watchdog.stop" (
    echo [watchdog] 检测到 logs\watchdog.stop，停止自恢复。
    goto stopped
)
"%~dp0.venv_iso\Scripts\pythonw.exe" "%~dp0scripts\supervisor.py"
set "CODE=%errorlevel%"
set /a FAILS+=1
if %FAILS% GEQ 5 (set "WAIT=60") else (set "WAIT=5")
echo [%date% %time%] supervisor exited code=%CODE% fails=%FAILS% wait=%WAIT%s >> "logs\watchdog.log"
rem 2026-11-05（§33.5 第 1 条）：bat 自己拼不了 JSON，交给 python 记一行；失败也不许打断循环。
"%~dp0.venv_iso\Scripts\python.exe" "%~dp0scripts\exit_note.py" --role bat --rc %CODE% --detail "watchdog 循环看到 supervisor 退出（fails=%FAILS%）" >nul 2>&1
echo [watchdog] 调度器退出 (code=%CODE%)，%WAIT% 秒后自动重启（第 %FAILS% 次）...
timeout /t %WAIT% /nobreak >nul
goto run

:stopped
"%~dp0.venv_iso\Scripts\python.exe" "%~dp0scripts\exit_note.py" --role bat --why watchdog_stop --detail "看到 logs\watchdog.stop，bat 循环停止自恢复" >nul 2>&1
echo.
echo ════════════════════════════════════════════
echo   服务已停止（watchdog.stop）
echo ════════════════════════════════════════════
pause
