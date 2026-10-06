@echo off
rem Rebuild the model-lookup HTML from a monthly price-list xlsx.
rem Usage: drag the new month xlsx onto this file.
chcp 65001 >nul
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if "%~1"=="" (
  echo [Usage] Drag the monthly price-list xlsx onto this file to rebuild.
  echo         e.g.  D:\xxx\2026年11月价格表-LYDF-共享.xlsx
  pause
  exit /b 1
)
if not exist "%PY%" (
  echo [Error] Python not found: %PY%
  pause
  exit /b 2
)
"%PY%" "%~dp0build.py" "%~1"
echo.
echo Done. The lookup page has been saved next to the xlsx as: 型号查询.html
echo (rename it if you want to keep several months side by side)
pause
