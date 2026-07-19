# Παρακολούθηση Λεωφορείων Αθήνας / Athens Bus Tracker

Αυτοματοποιημένο σύστημα καταγραφής και ανάλυσης δρομολογίων ΟΑΣΑ
(λεωφορεία + τρόλεϊ): πραγματικές αναχωρήσεις/λήξεις ανά διαδρομή,
αντιστοίχιση με το ημερήσιο πρόγραμμα, οχήματα ανά γραμμή και αμαξοστάσιο.

## Πώς δουλεύει (σύνοψη)

- **Poller** (`scripts/local_poller.py`): ρωτά συνεχώς το δημόσιο telematics
  API του ΟΑΣΑ (getStopArrivals) στις πρώτες/τελευταίες στάσεις κάθε
  διαδρομής και καταγράφει **διελεύσεις** οχημάτων → SQLite (`db/athensbus.db`).
- **Ανακατασκευή** (`scripts/trip_reconstruction_passages.py`): από τις
  διελεύσεις χτίζει δρομολόγια (αναχώρηση, λήξη, όχημα). Ημέρα λειτουργίας
  **04:00→04:00** — τα νυχτερινά ανήκουν στη βάρδια που τα εκτέλεσε.
- **Πρόγραμμα** (`scripts/sync_schedules.py`): ωριαίος «καθρέφτης» του
  ημερήσιου προγράμματος ΟΑΣΑ, με δίχτυα ασφαλείας για ελαττωματικά feeds.
- **Compute + Site** (`scripts/compute_daily_report.py`,
  `scripts/generate_site_data.py`): αντιστοίχιση εκτελεσμένων ↔
  προγραμματισμένων, στατιστικά, JSON για το dashboard (`docs/`).

> Το telematics API του ΟΑΣΑ **μπλοκάρει datacenter IPs** — ο poller πρέπει
> να τρέχει από οικιακή/κανονική σύνδεση.

## Δύο τρόποι λειτουργίας

### Α) GitHub Pages (η «κλασική» λειτουργία)

Τοπικό PC τρέχει poller + ωριαίο pipeline· το GitHub φιλοξενεί τη σελίδα.

```
Τοπικό PC:
  ├── run_poller.bat   → poller, συνεχώς
  └── run_hourly.bat   → κάθε ώρα: sync → compute → generate → git push
GitHub:
  ├── docs/data/       → JSON (pushed ωριαία)
  └── GitHub Pages     → live dashboard (auto-deploy σε κάθε push)
```

Εγκατάσταση:
1. Python 3.11+ (✓ Add to PATH) και `pip install -r requirements.txt`
2. `python scripts/bootstrap.py` για το αρχικό γέμισμα γραμμών/στάσεων
   (αν υπάρχει ήδη βάση, δεν χρειάζεται)
3. Task Scheduler: `run_poller.bat` (at startup) και `run_hourly.bat`
   (κάθε ώρα) — **Start in** = ο φάκελος του project
4. GitHub Pages: Settings → Pages → Source: GitHub Actions
   (τρέχει μόνο το `deploy-pages.yml`)

Σημείωση: η βάση `db/athensbus.db` ΔΕΝ ανεβαίνει στο git (όριο 100MB) —
είναι στο `.gitignore`· μόνο τα `docs/data/` JSON συγχρονίζονται.

### Β) Self-hosting (χωρίς GitHub) — φάκελος `deploy/`

Όλο το σύστημα σε δικό σου μηχάνημα: τοπικός web server σερβίρει τη σελίδα
και **η σελίδα ενημερώνεται αμέσως μετά από κάθε compute** (όχι ωριαία,
όχι git). Για προσωπική χρήση, port forwarding, ή VPS hosting.

- **Windows:** δες `deploy/windows/README.md` (start_poller.bat + start_server.bat)
- **Linux:** δες `deploy/linux/README.md` (shell scripts + systemd units)

Ο μηχανισμός: `scripts/serve_site.py` = web server (θύρα 8000, ρυθμιζόμενη)
+ βρόχος δεδομένων κάθε N λεπτά (προεπιλογή 15): sync προγράμματος (με το
ωριαίο/ημερήσιο όριο που ισχύει και στη λειτουργία Α) → compute → generate.
Οι δύο λειτουργίες δεν συγκρούονται — μοιράζονται τον ίδιο κώδικα και βάση.

## Δομή φακέλων

```
scripts/    όλος ο κώδικας (poller, sync, compute, generate, serve)
db/         schema.sql + athensbus.db (τοπικά μόνο, εκτός git)
docs/       το dashboard (index.html, assets/, data/)
deploy/     self-hosting: windows/ και linux/ με οδηγίες
```

## Χρήσιμα αρχεία καταγραφής

- `local_poller.log` — poller (μία γραμμή/λεπτό: passages, ουρές)
- `run_hourly.log` — το ωριαίο pipeline (λειτουργία Α)
- `serve_site.log` — ο self-hosted server (λειτουργία Β)
- Καρτέλα **Pipeline** στη σελίδα — ιστορικό εργασιών

## Δεδομένα & όρια

Πηγή: δημόσιο OASA Telematics API. Ακατέργαστες διελεύσεις: 30 ημέρες
(αυτόματο καθάρισμα)· στατιστικά ημερών: επ' αόριστον. Οι χρόνοι έχουν
εγγενή ανάλυση ~±1′ (στρογγύλεμα λεπτού στα δεδομένα του ΟΑΣΑ).
