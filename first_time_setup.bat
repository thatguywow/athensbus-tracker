@echo off
cd /d D:\athensbus-tracker

echo Installing dependencies...
pip install -r requirements.txt

echo Syncing all OASA lines, routes and stops (takes 20-40 minutes)...
python scripts/sync_master_data.py

echo Done. Now commit the database to GitHub:
echo   git init
echo   git remote add origin https://github.com/thatguywow/athensbus-tracker.git
echo   git add .
echo   git commit -m "initial setup"
echo   git push -u origin main

pause