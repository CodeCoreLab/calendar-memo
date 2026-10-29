@echo off
chcp 65001 >nul
title 日历备忘录
cd /d "%~dp0"

echo.
echo   正在启动日历备忘录...
echo   数据保存在本目录 data 文件夹，不会丢失
echo.

rem 优先用打包好的独立程序（用户机器上不需装 Python）
if exist "%~dp0dist\日历备忘录\日历备忘录.exe" (
    "%~dp0dist\日历备忘录\日历备忘录.exe" %*
    goto :end
)

rem 回退到源码模式（开发者机器）
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" desktop.py %*
    goto :end
)

echo   [错误] 未找到可运行的程序。
echo   请确认压缩包已完整解压，或运行 build.bat 重新打包。
pause

:end
