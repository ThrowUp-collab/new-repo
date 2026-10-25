@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 aar_archiver.py %*
) else (
    python aar_archiver.py %*
)
echo.
pause
