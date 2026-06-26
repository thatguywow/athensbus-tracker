@echo off
chcp 65001 >nul
cd /d D:\athensbus-tracker
echo ============================================
echo  ATHENS BUS - Live Poller
echo  Running continuously. Close window to stop.
echo ============================================
echo.

set PYTHONIOENCODING=utf-8

pip install -r requirements.txt -q

python scripts/local_poller.py
pause