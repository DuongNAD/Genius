@echo off
setlocal
:: Determine script directory
set "DIR=%~dp0"

:: Check if python3 or python is available
where python3 >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python3"
) else (
    where python >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON_EXE=python"
    ) else (
        echo Error: Python interpreter not found on PATH. >&2
        exit /b 1
    )
)

"%PYTHON_EXE%" "%DIR%.agents\skills\grok_researcher\run.py" %*
exit /b %errorlevel%
