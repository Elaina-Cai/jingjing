@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 2026-11-02: this machine sets HTTP_PROXY; loopback must bypass it
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
rem ============================================================
rem 重启鲸鲸（2026-11-05 v4：不再自己拼流程，改成调用 scripts\restart_stack.py）
rem   为什么改：bat 原先只写一个 logs\all.stop，而引擎要等监管器退出时才被按下 loop.stop；
rem   中间那 1 秒新监管器就起来了，会把还活着的旧引擎当孤儿 taskkill ——
rem   「引擎起了 27 次、只有 9 条子进程退出」就是这么来的（§36）。
rem   restart_stack.py 会写全三个哨兵（all.stop / scheduler.stop / loop.stop），
rem   给 25 秒优雅期，然后才清哨兵、起监管器；每一步都记进 logs\exits.jsonl。
rem ============================================================
if not exist logs mkdir logs
set "PY=%~dp0.venv_iso\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv_it\Scripts\python.exe"
if not exist "%PY%" (
  echo [XX] 找不到 python.exe：.venv_iso 和 .venv_it 都没有
  pause
  exit /b 1
)
echo [1/2] 优雅重启：写三个哨兵 → 等进程自己退 → 清哨兵 → 起监管器
"%PY%" "%~dp0scripts\restart_stack.py"
if errorlevel 1 (
  echo [XX] 重启失败：看 logs\supervisor.out / logs\loop_engine.out / logs\exits.jsonl
  pause
  exit /b 1
)
echo [2/2] 好了。谁退了、什么码：logs\exits.jsonl（也可以 python scripts\exit_report.py --hours 2）
timeout /t 3 /nobreak >nul
endlocal
exit /b 0
