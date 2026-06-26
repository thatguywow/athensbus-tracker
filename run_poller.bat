@echo off
cd /d D:\athensbus-tracker
echo ============================================
echo  ΑΘΗΝΑ ΛΕΩΦΟΡΕΙΑ - Live Poller
echo  Τρεχει συνεχως. Κλεισε το παραθυρο για να σταματησει.
echo ============================================
echo.

set PYTHONIOENCODING=utf-8

pip install -r requirements.txt -q

python scripts/local_poller.py
pause
