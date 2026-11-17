@echo off
chcp 65001 >nul
if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" -I "%~dp0start_web.py" %*
) else if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -I "%~dp0start_web.py" %*
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python -I "%~dp0start_web.py" %*
  ) else (
    py -3 -I "%~dp0start_web.py" %*
  )
)
if errorlevel 1 pause
