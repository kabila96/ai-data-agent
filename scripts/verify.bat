@echo off
REM Phase 5 verification: setup check + full test suite against your database.
REM Writes verification_report.txt in the project folder. Uses .env; no passwords are printed.
setlocal
cd /d "%~dp0\.."
call conda activate ai_data_agent || (echo [ERROR] Run scripts\setup_windows.bat first. & exit /b 1)
set TEST_FROM_ENV=1
set PYTHONIOENCODING=utf-8
echo Running verification... this takes about a minute.
> verification_report.txt echo ===== Verification %DATE% %TIME% =====
>> verification_report.txt echo.
>> verification_report.txt echo ===== check_setup =====
python scripts\check_setup.py --openai >> verification_report.txt 2>&1
>> verification_report.txt echo.
>> verification_report.txt echo ===== pytest =====
python -m pytest tests -o addopts="" -q -rs --tb=short >> verification_report.txt 2>&1
>> verification_report.txt echo.
>> verification_report.txt echo ===== environment =====
python -c "import sys,platform; print(sys.version); print(platform.platform())" >> verification_report.txt 2>&1
echo Done. Results are in verification_report.txt
endlocal
