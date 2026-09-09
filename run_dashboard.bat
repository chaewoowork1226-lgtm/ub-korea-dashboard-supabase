@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py forecast_tool.py serve
) else (
  python forecast_tool.py serve
)
pause
