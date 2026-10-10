@echo off
REM One-time setup on Windows with Anaconda/Miniconda.
REM Run from an "Anaconda Prompt" in the project folder:  scripts\setup_windows.bat
setlocal
cd /d "%~dp0\.."

where conda >nul 2>nul
if errorlevel 1 (
  echo [ERROR] conda was not found. Open "Anaconda Prompt" from the Start menu and run this again.
  exit /b 1
)

call conda env list | findstr /b /c:"ai_data_agent " >nul
if errorlevel 1 (
  echo Creating conda environment ai_data_agent ^(Python 3.12^)...
  call conda env create -f environment.yml || exit /b 1
) else (
  echo Updating existing environment ai_data_agent...
  call conda env update -n ai_data_agent -f environment.yml --prune || exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env from .env.example - open it and set PG_PASSWORD and OPENAI_API_KEY.
)

echo.
echo Setup complete. Next steps:
echo   1. Edit .env  (notepad .env)
echo   2. scripts\check_setup.bat
echo   3. scripts\run_app.bat
endlocal
