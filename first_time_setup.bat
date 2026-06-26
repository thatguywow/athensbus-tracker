@echo off
cd /d D:\athensbus-tracker
echo ============================================
echo  ΑΘΗΝΑ ΛΕΩΦΟΡΕΙΑ - First Time Setup
echo ============================================
echo.

echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Syncing all OASA lines, routes and stops (20-40 minutes)...
python scripts/sync_master_data.py

echo.
echo Syncing today's schedule...
python scripts/sync_schedules.py

echo.
echo ============================================
echo  Done! Now push to GitHub:
echo.
echo  git add db/athensbus.db
echo  git commit -m "initial master data sync"
echo  git push
echo ============================================
pause
