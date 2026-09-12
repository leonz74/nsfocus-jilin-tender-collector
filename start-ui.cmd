@echo off
setlocal
chcp 65001 >nul 2>nul

rem Resolve all paths from this launcher, even when started from System32.
set "PROJECT_ROOT=%~dp0"
set "CONFIG_PATH=%PROJECT_ROOT%config.json"
set "EXAMPLE_PATH=%PROJECT_ROOT%config.example.json"

if not exist "%EXAMPLE_PATH%" (
    echo Missing example config: "%EXAMPLE_PATH%"
    pause
    exit /b 1
)

if not exist "%CONFIG_PATH%" (
    copy /Y "%EXAMPLE_PATH%" "%CONFIG_PATH%" >nul
    if errorlevel 1 (
        echo Could not create config: "%CONFIG_PATH%"
        pause
        exit /b 1
    )
    echo Created config: "%CONFIG_PATH%"
)

set "PYTHONPATH=%PROJECT_ROOT%src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo Starting local UI: http://127.0.0.1:8765
python -m tender_downloader web --config "%CONFIG_PATH%" --port 8765
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Startup failed with exit code %EXIT_CODE%.
    echo If the UI is already open, use that window or close its launcher first.
    pause
)

exit /b %EXIT_CODE%
