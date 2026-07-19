@echo off
REM Athens Bus Tracker — self-hosted dashboard server (χωρίς GitHub)
REM Σερβίρει τη σελίδα και ανανεώνει τα δεδομένα μετά από κάθε compute.
REM Άλλαξε PORT / CYCLE_MINUTES εδώ αν θέλεις.
cd /d "%~dp0..\.."
set PORT=8000
set CYCLE_MINUTES=15
python scripts\serve_site.py --port %PORT% --interval %CYCLE_MINUTES%
pause
