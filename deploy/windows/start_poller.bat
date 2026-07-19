@echo off
REM Athens Bus Tracker — poller (συλλογή δεδομένων ΟΑΣΑ)
REM Τρέχει συνεχώς. Άφησε το παράθυρο ανοιχτό ή βάλε το στο Task Scheduler
REM (At startup, "Start in" = ο φάκελος του project).
cd /d "%~dp0..\.."
python scripts\local_poller.py
pause
