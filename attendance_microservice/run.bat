@echo off
echo ========================================================
echo   Campus Mind - Wi-Fi Smart Attendance Microservice
echo ========================================================
cd /d "%~dp0"
set "PATH=%LOCALAPPDATA%\Programs\Python\Python315;%LOCALAPPDATA%\Programs\Python\Python315\Scripts;%PATH%"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
if %errorlevel% neq 0 (
    py -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
)
pause

