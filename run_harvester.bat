@echo off
REM ---------------------------------------------------------------------------
REM Keeps the harvester alive. A crashed harvester is silent, and lost coverage
REM cannot be recovered later - the realtime feed keeps no history. This loop
REM restarts after any exit and logs when it happened.
REM
REM Run:  run_harvester.bat
REM Stop: close the window, or Ctrl+C twice
REM ---------------------------------------------------------------------------

cd /d "%~dp0"
call .venv\Scripts\activate.bat

if not exist logs mkdir logs

:loop
echo.
echo [%date% %time%] starting harvester
echo [%date% %time%] starting harvester >> logs\harvester.log

python manage.py harvest >> logs\harvester.log 2>&1

echo [%date% %time%] harvester exited - restarting in 30s
echo [%date% %time%] harvester exited >> logs\harvester.log
timeout /t 30 /nobreak > nul
goto loop
