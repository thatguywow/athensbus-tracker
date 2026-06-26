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

## Εγκατάσταση (μία φορά ανά υπολογιστή)

### 1. Python
Κατέβασε από python.org/downloads (3.12+). Κατά την εγκατάσταση: ✓ "Add Python to PATH"

### 2. Δημιούργησε ή κάνε clone το repository

**Πρώτη φορά (νέο repo):**
```
git init
git remote add origin https://github.com/thatguywow/athensbus-tracker.git
```

**Σε νέο υπολογιστή (repo ήδη υπάρχει):**
```
git clone https://github.com/thatguywow/athensbus-tracker.git
cd athensbus-tracker
pip install -r requirements.txt
```

### 2β. Ρύθμιση GitHub token για push
Απαραίτητο ανά υπολογιστή ώστε το `run_hourly.bat` να μπορεί να κάνει push αυτόματα:
```
git remote set-url origin https://thatguywow:ghp_yourtoken@github.com/thatguywow/athensbus-tracker.git
git config --global credential.helper store
```
Αντικατέστησε το `ghp_yourtoken` με το Personal Access Token σου:
GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token
- Expiration: No expiration
- Scope: ✓ repo

Χρειάζεται μόνο **μία φορά ανά υπολογιστή**. Μετά κάθε push γίνεται αυτόματα.

### 3. GitHub Settings (μία φορά μόνο)
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
- `run_poller.bat` — άνοιξέ το και άσε το ανοιχτό
- `run_hourly.bat` — βάλε το στο Task Scheduler (δες παρακάτω)

---

## Windows Task Scheduler — Αυτόματη εκτέλεση

### run_poller.bat (στην εκκίνηση των Windows)
1. Win+R → `shell:startup`
2. Δημιούργησε shortcut του `run_poller.bat` εκεί

### run_hourly.bat (κάθε ώρα)
1. Win+S → "Task Scheduler" → Create Basic Task
2. Name: `Athens Bus Hourly`
3. Trigger: Daily → Repeat every **1 hour** → for duration: **Indefinitely**
4. Action: Start a program → `D:\athensbus-tracker\run_hourly.bat`
5. Start in: `D:\athensbus-tracker`
6. Finish

---

## Μεταφορά σε νέο υπολογιστή

Αν αλλάξεις υπολογιστή ή θες να τρέχει σε server:

```
git clone https://github.com/thatguywow/athensbus-tracker.git
cd athensbus-tracker
pip install -r requirements.txt
git remote set-url origin https://thatguywow:ghp_yourtoken@github.com/thatguywow/athensbus-tracker.git
git config --global credential.helper store
```

Μετά στημένο Task Scheduler (ίδια βήματα παραπάνω) και τελείωσες.

> ⚠️ Μην τρέχεις `run_poller.bat` και `run_hourly.bat` σε **δύο υπολογιστές ταυτόχρονα** — θα προκύψουν git conflicts. Σταμάτα τα bat στον παλιό υπολογιστή πριν ξεκινήσεις στον νέο.

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
