@echo off
rem 兼容入口：推荐直接双击 日历备忘录.exe（本脚本仅供习惯用 bat 的场景）。
chcp 65001 >nul
title 日历备忘录
cd /d "%~dp0"

rem 1) 重新打包后的标准结构：<本目录>\日历备忘录\日历备忘录.exe
if exist "%~dp0日历备忘录\日历备忘录.exe" (
    start "" "%~dp0日历备忘录\日历备忘录.exe" %*
    goto :end
)

rem 2) 开发机：源码直跑（打包目录已被清理时用它，需要 .venv）
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" desktop.py %*
    goto :end
)

echo   [错误] 未找到 日历备忘录.exe
echo   请确认压缩包已完整解压（exe 与 _internal 目录必须放在一起）。
pause

:end
