@echo off
chcp 65001 >nul 2>&1
title 鲸鲸 - 全量回归（离线：会先停服）
cd /d "%~dp0"
echo 将停掉鲸鲸 -> 跑全量回归（约 45 分钟）-> 自动起服
echo UAC 提权提示随后出现
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','%~dp0scripts\full_regression_offline.ps1' -WorkingDirectory '%~dp0'"
