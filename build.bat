@echo off
chcp 65001 >nul
title 打包 日历备忘录
cd /d "%~dp0"

echo.
echo   === 打包为可移植分发包 ===
echo.

if not exist ".venv\Scripts\python.exe" (
    echo   [错误] 未找到 .venv，请先执行：
    echo       python -m venv .venv
    echo       .venv\Scripts\pip install -r requirements.txt pyinstaller
    pause
    exit /b 1
)

echo   [1/3] 打包中（约 40 秒）...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm memo.spec
if errorlevel 1 (
    echo   [失败] 打包出错，请看上面的日志
    pause
    exit /b 1
)

echo   [2/3] 压缩为 zip...
".venv\Scripts\python.exe" -c "import zipfile,os; src='dist/日历备忘录'; out='dist/日历备忘录-Windows-x64.zip'; z=zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=9); [z.write(os.path.join(r,f), os.path.relpath(os.path.join(r,f),'dist')) for r,d,fs in os.walk(src) for f in fs]; z.close(); print('   zip:', round(os.path.getsize(out)/1024/1024,1), 'MB')"

echo   [3/3] 完成
echo.
echo   产物： dist\日历备忘录-Windows-x64.zip
echo   分发： 把 zip 直接发给用户，解压后双击「日历备忘录.exe」即可
echo.
pause
