@echo off
chcp 65001 >nul
set PY=C:\Users\chaew\AppData\Local\Python\pythoncore-3.14-64\python.exe
if not exist "%PY%" set PY=python
echo.
echo   Claude invoice verification - installing the anthropic package...
echo.
"%PY%" -m pip install --upgrade anthropic
if errorlevel 1 goto fail
echo.
echo   Done. Restart the dashboard, then paste your key into the Claude key box.
echo.
pause
exit /b 0
:fail
echo.
echo   Install failed. Check the internet connection and run again.
echo.
pause
