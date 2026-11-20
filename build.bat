@echo off
REM ---------------------------------------------------------------------
REM Build sap-abap-exporter.exe  (run this on Windows)
REM ---------------------------------------------------------------------
setlocal

echo.
echo === SAP ABAP Source Exporter - build ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python was not found on PATH.
    echo     Install Python 3.10+ from https://www.python.org/downloads/windows/
    echo     and tick "Add python.exe to PATH".
    exit /b 1
)

echo [1/4] Creating virtual environment...
if not exist .venv (
    python -m venv .venv || exit /b 1
)

echo [2/4] Installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || exit /b 1

echo [3/4] Running self-tests...
python -m pytest tests -q
if errorlevel 1 echo     (tests reported problems - continuing anyway)

echo [4/4] Building the executable...
python -m PyInstaller --noconfirm --clean sap_abap_exporter.spec || exit /b 1

echo.
echo === Done ===
echo Executable: %cd%\dist\sap-abap-exporter.exe
echo.
endlocal
