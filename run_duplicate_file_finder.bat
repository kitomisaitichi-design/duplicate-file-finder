@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "Duplicate File Finder" pyw "%~dp0duplicate_file_finder.py"
) else (
    start "Duplicate File Finder" pythonw "%~dp0duplicate_file_finder.py"
)
endlocal
