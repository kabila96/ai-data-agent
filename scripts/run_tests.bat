@echo off
REM Offline tests:            scripts\run_tests.bat
REM Include live DB tests:    set TEST_PG_PASSWORD=<read-only role password>  then run this script
REM   optional: TEST_PG_HOST, TEST_PG_PORT, TEST_PG_DATABASE, TEST_PG_USER, TEST_PG_SUPERUSER_PASSWORD
setlocal
cd /d "%~dp0\.."
call conda activate ai_data_agent || (echo [ERROR] Run scripts\setup_windows.bat first. & exit /b 1)
python -m pytest tests %*
endlocal
