# Παρακολούθηση Λεωφορείων Αθήνας / Athens Bus Tracker

Αυτοματοποιημένο σύστημα συλλογής δεδομένων λεωφορείων ΟΑΣΑ με dashboard GitHub Pages.

---

## Γρήγορη Εκκίνηση / Quick Setup

### 1. Δημιουργία δημόσιου GitHub repository

```bash
git clone <this-repo-url>
# ή αντιγράψτε το zip και κάντε push σε νέο public repo
git push -u origin main
```

### 2. Ενεργοποίηση GitHub Pages

**Settings → Pages → Source → "Deploy from a branch" → main → /docs**

### 3. Δικαιώματα Actions

**Settings → Actions → General → Workflow permissions → "Read and write permissions"** ✓

### 4. Συγχρονισμός βασικών δεδομένων (ΤΟΠΙΚΑ — ο ΟΑΣΑ μπλοκάρει τα IPs του GitHub)

```bash
pip install -r requirements.txt
python scripts/sync_master_data.py   # ~20-40 λεπτά, όλες οι γραμμές/στάσεις
git add db/athensbus.db
git commit -m "αρχικά δεδομένα"
git push
```

### 5. Εκκίνηση τοπικού poller

```bash
# Ορίστε τα credentials μία φορά:
export GITHUB_TOKEN=ghp_yourtoken      # Personal Access Token με repo scope
export GITHUB_REPO=yourname/your-repo

# Εκκίνηση (τρέχει συνεχώς, κάθε 5 λεπτά):
python scripts/local_poller.py
```

Σε Windows μπορείτε να το προσθέσετε στο Task Scheduler ή να αφήσετε ανοιχτό terminal.

---

## Αρχιτεκτονική

```
Κάθε 5 λεπτά │  local_poller.py (τοπικά)
(τοπικά)      │  → getBusLocation για όλες τις διαδρομές
              │  → getStopArrivals για τερματικές στάσεις
              │  → commit pings.jsonl + terminus.jsonl στο branch ping-artifacts
              │
00:20 UTC     │  compute-daily-report.yml (GitHub Actions)
κάθε μέρα     │  → checkout ping-artifacts branch
              │  → merge_ping_artifacts.py (pings + terminus → DB)
              │  → compute_daily_report.py (ανακατασκευή δρομολογίων)
              │  → rotation_slots.py (σειρές δρομολόγων, αλλαγές βάρδιας)
              │  → generate_site_data.py (JSON για dashboard)
              │  → git commit ΜΟΝΟ ΜΙΑ ΦΟΡΑ στο main
              │
Κυριακή       │  sync-master-data.yml (μόνο schedules μέσω Actions)
02:00 UTC     │  → Τα lines/routes/stops συγχρονίζονται τοπικά
```

---

## Τι Παρακολουθείται

| Δεδομένα | Πίνακας DB | Διατήρηση |
|---|---|---|
| GPS θέσεις οχημάτων (κάθε 5 λεπτά) | `vehicle_pings` | 30 ημέρες |
| Προβλέψεις τερματικών στάσεων | `terminus_observations` | 30 ημέρες |
| Ανακατασκευασμένα δρομολόγια | `trips` + `trip_stop_times` | Μόνιμα |
| Σειρές δρομολόγων (rotation slots) | `slot_assignments` | Μόνιμα |
| Αλλαγές βάρδιας | `slot_handoffs` | Μόνιμα |
| Δραστηριότητα οχημάτων ανά ημέρα | `vehicle_activity` | Μόνιμα |
| Ημερήσια στατιστικά (πραγματικά vs προγ.) | `daily_route_stats` | Μόνιμα |

---

## Εξαγωγή Excel

```bash
# Τελευταία ημέρα, 14 ημέρες ιστορικό:
python scripts/export_excel.py

# Συγκεκριμένη ημέρα:
python scripts/export_excel.py --date 2026-06-21

# Πλήρες ιστορικό:
python scripts/export_excel.py --all
```

**Φύλλα Excel:**
1. **Οχήματα ανά Ημέρα** — Col A (μπλε) αριθμός οχήματος, Col B (πράσινο) διαδρομή
2. **Πλέγμα Δρομολογίων** — γραμμές=04:00-23:55 ανά 5 λεπτά, στήλες=διαδρομές, κελιά=όχημα
3. **Δρομολόγια & Στάσεις** — κάθε δρομολόγιο με χρόνους ανά στάση
4. **Ημερήσια Στατιστικά** — πραγματικά vs προγραμματισμένα, % εκτέλεσης, καθυστέρηση
5. **Αναχωρήσεις Οχημάτων** — κάθε αναχώρηση με slot και απόκλιση
6. **Αλλαγές Βάρδιας** — πότε άλλαξε όχημα σε κάθε σειρά
7. **Γραμμές & Διαδρομές** — πίνακας αναφοράς

---

## Σύστημα Σειρών Δρομολόγων (Rotation Slots)

Κάθε διαδρομή έχει Ν λεωφορεία σε εναλλαγή. Π.χ. με headway 10 λεπτά και κύκλο 40 λεπτά:
- 4 λεωφορεία σε εναλλαγή
- Σειρά 1: αναχωρεί 06:00, 06:40, 07:20, ...
- Σειρά 2: αναχωρεί 06:10, 06:50, 07:30, ...

Το σύστημα:
1. Εξάγει headway από το πρόγραμμα
2. Εκτιμά τον κύκλο από τη διάρκεια πραγματικών δρομολογίων
3. Αναθέτει κάθε πραγματικό δρομολόγιο στη σωστή σειρά
4. Ανιχνεύει αλλαγές οχήματος (shift changes, διαλείμματα)
5. Χειρίζεται καθυστερήσεις με κυλιόμενο παράθυρο ±60% headway

---

## Ακρίβεια Χρόνων

| Τύπος | Πηγή | Ακρίβεια |
|---|---|---|
| Αναχώρηση/Άφιξη τερματικού | ΟΑΣΑ getStopArrivals | ±1-2 λεπτά |
| Ενδιάμεσες στάσεις (γραμμική παρεμβολή) | GPS interpolation | ±1-3 λεπτά |
| Ενδιάμεσες στάσεις (snap fallback) | Πλησιέστερο ping | ±0-5 λεπτά |

---

## Επαναϋπολογισμός παλιάς ημέρας

```bash
python scripts/compute_daily_report.py 2026-06-15
# Idempotent: διαγράφει και ξαναϋπολογίζει καθαρά
```

---

## Γνωστοί Περιορισμοί

- **Polling ανά 5 λεπτά**: GitHub Actions cron floor. Τοπικό script → δεν ισχύει αυτός ο περιορισμός αν αλλάξετε το POLL_INTERVAL_SECS στο local_poller.py.
- **UTC vs ώρα Αθήνας**: Η ημερήσια αναφορά τρέχει στις 00:20 UTC (~03:20 Αθήνα). Δρομολόγια πολύ αργά τη νύχτα (~02:00-03:00 Αθήνα) μπορεί να μπουν σε λάθος ημέρα.
- **ΟΑΣΑ API αξιοπιστία**: Μερικές φορές επιστρέφει κενά ή timeout. Το σύστημα ανέχεται μερικές αποτυχίες.
