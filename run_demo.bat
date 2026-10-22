@echo off
rem One-click launcher for the demo portal on Windows.
rem First run creates .venv and installs requirements (a few minutes); later runs start in seconds.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment in .venv ...
    py -3.11 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Could not create .venv. Install Python 3.11 or newer from python.org and try again.
        pause
        exit /b 1
    )
)
echo Checking packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo Package installation failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo Starting the demo portal at http://localhost:8501  (press Ctrl+C to stop)
".venv\Scripts\python.exe" -m streamlit run dashboard\app.py --server.address 127.0.0.1 --server.port 8501
