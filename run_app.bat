@echo off
setlocal
cd /d "%~dp0"
title StudyGraph Setup
cls

echo =====================================
echo           StudyGraph Setup
echo =====================================
echo.

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [SETUP] Creating virtual environment...
    py -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
        if errorlevel 1 (
            echo.
            echo [ERROR] Failed to create virtual environment.
            echo Make sure Python is installed and available as py or python.
            pause
            exit /b 1
        )
    )
)

echo [SETUP] Installing dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip >nul
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)

if not exist "requirements.txt" (
    echo.
    echo [ERROR] requirements.txt was not found in this folder.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -m pip install -r requirements.txt >nul
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install requirements.txt.
    pause
    exit /b 1
)

if not exist "studygraph_setup_env.py" (
    echo.
    echo [ERROR] studygraph_setup_env.py was not found in this folder.
    echo Make sure it is in the same folder as run_app.bat.
    pause
    exit /b 1
)

"%PYTHON_EXE%" "studygraph_setup_env.py"
if errorlevel 1 (
    echo.
    echo Setup stopped because the environment/database check failed.
    pause
    exit /b 1
)

echo.
echo [RUN] Starting StudyGraph...
echo.
"%PYTHON_EXE%" "app.py"
pause
exit /b 0
