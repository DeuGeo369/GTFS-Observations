@echo off
REM ---------------------------------------------------------------------------
REM Re-runs the aggregation every 4 hours so results are current whenever you
REM look. Aggregation is a full rebuild from the observation table, so running
REM it repeatedly is safe - there is no incremental state to corrupt.
REM
REM Run in a SECOND window, alongside run_harvester.bat
REM ---------------------------------------------------------------------------

cd /d "%~dp0"
call .venv\Scripts\activate.bat

if not exist logs mkdir logs

:loop
echo [%date% %time%] aggregating
echo [%date% %time%] aggregating >> logs\aggregate.log
python manage.py aggregate >> logs\aggregate.log 2>&1
echo [%date% %time%] done - next run in 4 hours

REM 14400 seconds = 4 hours
timeout /t 14400 /nobreak > nul
goto loop
