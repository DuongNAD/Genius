@echo off
setlocal
:: Determine script directory
set "DIR=%~dp0"

:: Resolve a Python interpreter: prefer an active python/python3, then fall
:: back to the Windows "py" launcher (common when python is not on PATH).
set "PYTHON_EXE="
where python3 >nul 2>nul && set "PYTHON_EXE=python3"
if not defined PYTHON_EXE (
    where python >nul 2>nul && set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
    where py >nul 2>nul && set "PYTHON_EXE=py"
)
if not defined PYTHON_EXE (
    echo Error: Python interpreter not found on PATH ^(tried python3, python, py^). >&2
    exit /b 1
)

"%PYTHON_EXE%" "%DIR%.agents\skills\claude_architect\run.py" %*
exit /b %errorlevel%
