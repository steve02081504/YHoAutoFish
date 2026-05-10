@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "REQ=%~dp0requirements.txt"

if not exist "%VENV_PY%" (
    echo Cratting venv
    where py >nul 2>&1 && (
        py -3 -m venv .venv
    ) || (
        python -m venv .venv
    )
    if not exist "%VENV_PY%" (
        echo failed
        pause
        exit /b 1
    )
    if not exist "%REQ%" (
        echo no requirements.txt found
        pause
        exit /b 1
    )
    echo initting
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r "%REQ%"
    if errorlevel 1 (
        echo faild
        pause
        exit /b 1
    )
    echo :)
)

"%VENV_PY%" "%~dp0main.py" %*
exit /b %ERRORLEVEL%
