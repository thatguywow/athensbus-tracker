# Self-hosting σε Linux (χωρίς GitHub)

Τρέχεις **ολόκληρο** το εργαλείο σε Linux μηχάνημα (τοπικό ή server):
συλλογή δεδομένων + ιστοσελίδα, χωρίς git/GitHub. Η σελίδα ενημερώνεται
**αμέσως μετά από κάθε compute** (προεπιλογή κάθε 15′ — ρυθμίζεται).

## ⚠️ Πρώτα διάβασε αυτό (VPS)

Το telematics API του ΟΑΣΑ **μπλοκάρει IP από datacenters**. Ο poller σε
VPS (Hetzner, DigitalOcean, AWS κ.λπ.) πιθανότατα ΔΕΝ θα παίρνει δεδομένα.
Λειτουργικά μοτίβα:
- **Όλα σε σπίτι/γραφείο** (οικιακή IP) + port forwarding → δουλεύει πλήρως.
- **Υβριδικό**: poller σε οικιακή σύνδεση, VPS μόνο για hosting — απαιτεί
  μεταφορά της βάσης/των JSON προς το VPS (rsync/syncthing) και είναι εκτός
  αυτού του οδηγού.
Δοκίμασε πρώτα από το μηχάνημά σου: `python3 -c "import sys; sys.path.insert(0,'scripts'); import oasa_client as o; print(len(o.get_stop_arrivals('10001') or []))"` —
αν γυρνάει αριθμό χωρίς error, η IP σου περνάει.

## Εγκατάσταση

```bash
sudo apt update && sudo apt install -y python3 python3-pip   # Debian/Ubuntu
cd /opt && sudo git clone <repo-url> athensbus-tracker        # ή αντιγραφή φακέλου
cd athensbus-tracker
pip3 install -r requirements.txt
```

## Εκκίνηση (χειροκίνητα, για δοκιμή)

Σε δύο τερματικά (ή με `tmux`):
```bash
deploy/linux/start_poller.sh
deploy/linux/start_server.sh
```
Σελίδα: `http://<IP>:8000`. Ρυθμίσεις με env: `PORT=8080 CYCLE_MINUTES=10 deploy/linux/start_server.sh`

## Μόνιμη λειτουργία με systemd (προτεινόμενο)

```bash
# Διόρθωσε User= και WorkingDirectory= μέσα στα .service αρχεία, μετά:
sudo cp deploy/linux/athensbus-poller.service /etc/systemd/system/
sudo cp deploy/linux/athensbus-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now athensbus-poller athensbus-server

# Παρακολούθηση:
journalctl -u athensbus-poller -f
journalctl -u athensbus-server -f
```
Επανεκκινούν αυτόματα σε σφάλμα ή reboot.

## Δίκτυο

- Τοπικό μηχάνημα πίσω από router: προώθησε TCP θύρα στο router (π.χ. 8000 →
  IP μηχανήματος) και άνοιξε το firewall: `sudo ufw allow 8000/tcp`.
- Δημόσιο URL με HTTPS: βάλε μπροστά ένα reverse proxy (Caddy: 2 γραμμές
  Caddyfile — `your.domain { reverse_proxy localhost:8000 }`).

> Ο ενσωματωμένος server σερβίρει ΜΟΝΟ στατικά αρχεία του `docs/` — η βάση
> και τα scripts δεν εκτίθενται.

## Χωρητικότητα & συντήρηση (μικροί δίσκοι, π.χ. VPS 20GB)

Ενδεικτική κατανάλωση: βάση **~0,5-2GB** σε πλήρη λειτουργία (οι ακατέργαστες
διελεύσεις καθαρίζονται αυτόματα στις 30 ημέρες), logs **≤15MB** συνολικά
(αυτόματο rotation 5MB × 2 backups ανά αρχείο). Σε VPS 20GB (OS ~5GB) το
περιθώριο είναι άνετο.

Το SQLite όμως δεν «επιστρέφει» χώρο στο λειτουργικό — το αρχείο μένει στο
μέγιστο μέγεθος που έπιασε. Προαιρετική μηνιαία συρρίκνωση:
```bash
sudo systemctl stop athensbus-poller athensbus-server
python3 scripts/vacuum_db.py
sudo systemctl start athensbus-poller athensbus-server
```
(Το docstring του `scripts/vacuum_db.py` έχει έτοιμη γραμμή cron.)

## Πού βλέπω τι γίνεται

- `journalctl -u athensbus-poller -f` ή `local_poller.log`
- `journalctl -u athensbus-server -f` ή `serve_site.log`
- Καρτέλα **Pipeline** στη σελίδα
