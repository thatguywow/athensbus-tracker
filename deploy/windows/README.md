# Self-hosting σε Windows (χωρίς GitHub)

Τρέχεις **ολόκληρο** το εργαλείο τοπικά: συλλογή δεδομένων + ιστοσελίδα,
χωρίς git, χωρίς GitHub Pages. Η σελίδα ενημερώνεται **αμέσως μετά από κάθε
compute** (προεπιλογή: κάθε 15 λεπτά — ρυθμίζεται).

## Τι τρέχει

| Διεργασία | Τι κάνει |
|---|---|
| `start_poller.bat` | Συλλέγει διελεύσεις από το API του ΟΑΣΑ, συνεχώς |
| `start_server.bat` | Web server για τη σελίδα + κύκλος: sync προγράμματος → compute → ενημέρωση σελίδας |

Οι δύο διεργασίες είναι ανεξάρτητες — μοιράζονται μόνο τη βάση `db/athensbus.db`.

## Εγκατάσταση (μία φορά)

1. **Python 3.11+** από το python.org — στην εγκατάσταση τσέκαρε ✓ *Add Python to PATH*.
2. Άνοιξε Command Prompt στον φάκελο του project και τρέξε:
   ```
   pip install -r requirements.txt
   ```

## Εκκίνηση

Κάνε διπλό κλικ (ή τρέξε από CMD), **και τα δύο**:

1. `deploy\windows\start_poller.bat`
2. `deploy\windows\start_server.bat`

Η σελίδα: **http://localhost:8000** — από άλλη συσκευή του δικτύου:
`http://<IP-του-PC>:8000` (βρες την IP με `ipconfig`).

## Ρυθμίσεις

Άνοιξε το `start_server.bat` με Notepad:
- `set PORT=8000` — η θύρα του server.
- `set CYCLE_MINUTES=15` — κάθε πόσα λεπτά τρέχει compute + ενημέρωση σελίδας.
  Το πρόγραμμα ΟΑΣΑ συγχρονίζεται το πολύ 1 φορά/ώρα ό,τι κι αν βάλεις εδώ.

## Πρόσβαση από το internet (port forwarding)

1. Στο router σου: προώθησε μια εξωτερική θύρα (π.χ. 8000) στην IP του PC, θύρα 8000 (TCP).
2. Στο Windows Firewall: επίτρεψε εισερχόμενες συνδέσεις στη θύρα (Windows Defender
   Firewall → Advanced Settings → Inbound Rules → New Rule → Port → TCP 8000 → Allow).
3. Η σελίδα θα είναι στο `http://<δημόσια-IP>:8000`. Αν η IP σου αλλάζει,
   χρησιμοποίησε δωρεάν dynamic DNS (π.χ. DuckDNS).

> **Ασφάλεια:** ο ενσωματωμένος server σερβίρει ΜΟΝΟ στατικά αρχεία του
> φακέλου `docs/` — δεν εκθέτει τη βάση ή τα scripts. Παρ' όλα αυτά, για
> μόνιμη δημόσια έκθεση σκέψου ένα reverse proxy (π.χ. Caddy) με HTTPS.

## Αυτόματη εκκίνηση με τα Windows (προαιρετικά)

Task Scheduler → Create Task (×2, μία ανά .bat):
- Trigger: **At startup** (ή At log on)
- Action: Start a program → το αντίστοιχο .bat
- **Start in:** ο φάκελος του project (π.χ. `D:\athensbus-tracker`)
- Γενικά: "Run whether user is logged on or not" αν το θες χωρίς παράθυρα.

## Σημαντική σημείωση για VPS

Το telematics API του ΟΑΣΑ **μπλοκάρει IP από datacenters** — ο poller σε
VPS πιθανότατα ΔΕΝ θα παίρνει δεδομένα. Δοκιμασμένο μοτίβο: poller σε σπίτι
(οικιακή σύνδεση) και, αν θες δημόσια σελίδα χωρίς port forwarding, μετέφερε
μόνο το hosting αλλού. Σε καθαρά τοπικό στήσιμο (PC + port forwarding) δεν
υπάρχει κανένα θέμα.

## Πού βλέπω τι γίνεται

- `local_poller.log` — ο poller (γραμμές `two-speed: passages=…`)
- `serve_site.log` — οι κύκλοι δεδομένων του server
- Καρτέλα **Pipeline** στη σελίδα — ιστορικό εργασιών
