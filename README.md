# Παρακολούθηση Λεωφορείων Αθήνας / Athens Bus Tracker

Αυτοματοποιημένο σύστημα συλλογής δεδομένων λεωφορείων ΟΑΣΑ.
Τρέχει εξ ολοκλήρου τοπικά — το GitHub χρησιμοποιείται μόνο για αποθήκευση και hosting.

---

## Αρχιτεκτονική

```
Τοπικό PC (τρέχει πάντα):
  ├── run_poller.bat      → κάθε 5 λεπτά: GPS pings → τοπικό SQLite
  └── run_hourly.bat      → κάθε ώρα: compute + generate + git push

GitHub (αποθήκευση + hosting):
  ├── db/athensbus.db     → η βάση δεδομένων (pushed ωριαία)
  ├── docs/data/          → JSON για το dashboard (pushed ωριαία)
  └── GitHub Pages        → το live dashboard (auto-deploy μετά από κάθε push)
```

**Το GitHub Actions τρέχει ΜΟΝΟ το `deploy-pages.yml`** — αυτόματα μετά από κάθε push.
Όλα τα άλλα workflows είναι πληροφοριακά μόνο.

---

## Εγκατάσταση (μία φορά)

### 1. Python
Κατέβασε από python.org/downloads (3.12+). Κατά την εγκατάσταση: ✓ "Add Python to PATH"

### 2. Δημιούργησε δημόσιο GitHub repository
```
git init
git remote add origin https://github.com/yourusername/athensbus-tracker.git
```

### 3. GitHub Settings
- **Settings → Actions → General → Workflow permissions → Read and write** ✓
- **Settings → Pages → Source → GitHub Actions** ✓

### 4. Πρώτη φορά — sync master data (τοπικά)
```
first_time_setup.bat
```
Τρέχει ~20-40 λεπτά. Μετά:
```
git add db/athensbus.db
git commit -m "initial setup"
git push -u origin main
```

### 5. Εκκίνηση
Άνοιξε **δύο** terminal παράθυρα:
- Παράθυρο 1: `run_poller.bat` (τρέχει πάντα)
- Παράθυρο 2: task scheduler ή manual `run_hourly.bat` κάθε ώρα

---

## Windows Task Scheduler — Αυτόματη εκτέλεση

### run_poller.bat (στην εκκίνηση των Windows)
1. Win+R → `shell:startup`
2. Δημιούργησε shortcut του `run_poller.bat` εκεί

### run_hourly.bat (κάθε ώρα)
1. Win+S → "Task Scheduler" → Create Basic Task
2. Name: "Athens Bus Hourly"
3. Trigger: Daily → Repeat every 1 hour
4. Action: Start a program → `D:\athensbus-tracker\run_hourly.bat`
5. Finish

---

## Καθημερινή χρήση

| Ενέργεια | Πώς |
|---|---|
| Εκκίνηση poller | `run_poller.bat` (ή αυτόματα στην εκκίνηση) |
| Εξαγωγή Excel | `export.bat` |
| Recompute μιας ημέρας | `python scripts/compute_daily_report.py 2026-06-25` |
| Sync νέων γραμμών/στάσεων | `first_time_setup.bat` (σπάνια) |

---

## Τι αποθηκεύεται

| Δεδομένα | Πίνακας | Διατήρηση |
|---|---|---|
| GPS pings (κάθε 5 λεπτά) | `vehicle_pings` | 30 ημέρες |
| Terminus observations | `terminus_observations` | 30 ημέρες |
| Ανακατασκευασμένα δρομολόγια | `trips` + `trip_stop_times` | Μόνιμα |
| Καρτελάκια (rotation slots) | `slot_assignments` | Μόνιμα |
| Ημερήσια στατιστικά | `daily_route_stats` | Μόνιμα |
| Δραστηριότητα οχημάτων | `vehicle_activity` | Μόνιμα |

---

## Scripts

| Script | Εκτελείται από | Συχνότητα |
|---|---|---|
| `local_poller.py` | run_poller.bat | Συνεχώς (κάθε 5λεπ) |
| `run_hourly.py` | run_hourly.bat | Κάθε ώρα |
| `sync_master_data.py` | first_time_setup.bat | Σπάνια |
| `sync_schedules.py` | run_hourly.py | 1x/ημέρα (αυτόματα) |
| `compute_daily_report.py` | run_hourly.py | Κάθε ώρα |
| `generate_site_data.py` | run_hourly.py | Κάθε ώρα |
| `export_excel.py` | export.bat | Manual |
