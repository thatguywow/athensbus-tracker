#!/usr/bin/env bash
# Athens Bus Tracker — poller (συλλογή δεδομένων ΟΑΣΑ). Τρέχει συνεχώς.
cd "$(dirname "$0")/../.."
exec python3 scripts/local_poller.py
