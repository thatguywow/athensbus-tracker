@echo off
cd /d D:\athensbus-tracker
echo Pulling latest database...
git pull
echo.
echo Exporting to Excel...
python scripts/export_excel.py
echo.
echo Done! Check the folder for the .xlsx file.
pause
