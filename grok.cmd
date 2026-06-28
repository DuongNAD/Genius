@echo off
setlocal
:: Determine script directory
set "DIR=%~dp0"

:: Check if python is available
where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python"
) else (
    if exist "C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe" (
        set "PYTHON_EXE=C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"
    ) else (
        echo Error: Python interpreter not found on PATH. >&2
        exit /b 1
    )
)

"%PYTHON_EXE%" "%DIR%.agents\skills\grok_researcher\run.py" %*
exit /b %errorlevel%
