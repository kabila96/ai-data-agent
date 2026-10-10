@echo off
REM Start the app:  scripts\run_app.bat   (opens http://localhost:8501)
setlocal
cd /d "%~dp0\.."
call conda activate ai_data_agent || (echo [ERROR] Run scripts\setup_windows.bat first. & exit /b 1)
python -m streamlit run app.py --server.port 8501 --server.address localhost
endlocal
