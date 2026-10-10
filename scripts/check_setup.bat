@echo off
setlocal
cd /d "%~dp0\.."
call conda activate ai_data_agent || (echo [ERROR] Run scripts\setup_windows.bat first. & exit /b 1)
python scripts\check_setup.py %*
endlocal
