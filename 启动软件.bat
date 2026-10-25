@echo off
cd /d "%~dp0"

REM ---- apply pending console update (xianyu_console.new.exe) ----
if exist xianyu_console.new.exe (
  if exist xianyu_console.exe del /f /q xianyu_console.exe >nul 2>nul
  if exist xianyu_console.exe (
    echo [i] Console is still running - update applies on next start.
  ) else (
    ren xianyu_console.new.exe xianyu_console.exe >nul 2>nul
    if exist xianyu_console.exe echo [i] Console updated to the new build.
  )
)

if exist xianyu_console.exe (
  start "" xianyu_console.exe
) else if exist runtime\python\Scripts\python.exe (
  runtime\python\Scripts\python.exe app_launcher.py
) else (
  python app_launcher.py
)
