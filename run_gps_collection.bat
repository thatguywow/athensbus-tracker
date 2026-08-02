@echo off
REM ============================================================
REM  Συλλογή GPS για ΜΙΑ ΠΛΗΡΗ ημέρα βάρδιας (04:00 -> 04:00).
REM
REM  Τρέχει ΤΟΠΙΚΑ, επίτηδες: το όριο ρυθμού του ΟΑΣΑ είναι ανά IP,
REM  οπότε από το σπίτι δεν κλέβει budget από τον poller του VPS.
REM  Ο VPS συνεχίζει κανονικά με getStopArrivals — καμία αλλαγή εκεί.
REM
REM  Μετά το τέλος:
REM    python scripts\import_remote_passages.py <ΗΜΕΡΟΜΗΝΙΑ>
REM    python scripts\compare_methods.py <ΗΜΕΡΟΜΗΝΙΑ> --reconstruct
REM ============================================================
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo [%date% %time%] Εκκίνηση συλλογής GPS (24 ώρες, 18 req/s) >> gps_collection.log

REM 1440 λεπτά = 24 ώρες. Ξεκινώντας 04:00 καλύπτει ακριβώς μία ημέρα βάρδιας.
.venv\Scripts\python.exe scripts\gps_tracker.py --rate 18 --minutes 1440 >> gps_collection.log 2>&1

echo [%date% %time%] Τέλος συλλογής >> gps_collection.log
