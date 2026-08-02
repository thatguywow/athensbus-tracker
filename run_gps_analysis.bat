@echo off
REM ============================================================
REM  Ανάλυση μετά τη συλλογή: κατεβάζει τις διελεύσεις του VPS και
REM  συγκρίνει τις δύο μεθόδους στην ΙΔΙΑ ημέρα βάρδιας.
REM
REM  Τρέχει 04:30, δηλαδή ΜΕΤΑ το τέλος της 24ωρης συλλογής (04:00).
REM  Η ημέρα βάρδιας είναι η ΧΘΕΣΙΝΗ ημερολογιακή: στις 04:30 της 2ας
REM  Αυγούστου, η ημέρα που μόλις ολοκληρώθηκε είναι η 1η Αυγούστου.
REM
REM  Το αποτέλεσμα γράφεται σε gps_report_<ΗΜΕΡΟΜΗΝΙΑ>.txt ώστε να
REM  υπάρχει ακόμη κι αν δεν το ζητήσει κανείς αμέσως.
REM ============================================================
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

for /f %%d in ('.venv\Scripts\python.exe -c "import datetime;print((datetime.date.today()-datetime.timedelta(days=1)).isoformat())"') do set SD=%%d

set REPORT=gps_report_%SD%.txt

echo ============================================================ > "%REPORT%"
echo  Σύγκριση μεθόδων - ημέρα βάρδιας %SD% >> "%REPORT%"
echo  Παρήχθη: %date% %time% >> "%REPORT%"
echo ============================================================ >> "%REPORT%"
echo. >> "%REPORT%"

echo [%time%] Λήψη διελεύσεων VPS... >> "%REPORT%"
.venv\Scripts\python.exe scripts\import_remote_passages.py %SD% >> "%REPORT%" 2>&1

echo. >> "%REPORT%"
echo [%time%] Σύγκριση... >> "%REPORT%"
.venv\Scripts\python.exe scripts\compare_methods.py %SD% --reconstruct >> "%REPORT%" 2>&1

echo. >> "%REPORT%"
echo [%time%] Σφάλμα παρεμβολής... >> "%REPORT%"
.venv\Scripts\python.exe scripts\validate_interpolation.py >> "%REPORT%" 2>&1

echo. >> "%REPORT%"
echo [%time%] ΤΕΛΟΣ >> "%REPORT%"
