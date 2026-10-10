@echo off
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" /b ".venv\Scripts\pythonw.exe" app.py
) else (
    start "" /b pythonw.exe app.py
)
endlocal
