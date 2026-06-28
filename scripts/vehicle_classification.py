"""
vehicle_classification.py — maps an OASA vehicle number to its depot
(αμαξοστάσιο) and vehicle type (τύπος οχήματος).

RULE (per the fleet reference):
  • The FIRST digit of the vehicle number is the depot (0-9; no depot 2).
  • Vehicle type is found by:
      1) Checking the FULL number against "specific depot" ranges
         (Συγκεκριμένα αμαξοστάσια), then
      2) Stripping the first digit and checking the remaining base against the
         "any depot" ranges (Με οποιοδήποτε αμαξοστάσιο).

Verified against observed vehicles: 79085→Λιόσια/Irisbus Citelis CNG,
10562→Βοτανικός/N2, 54493→Μπραχάμι/MAN 12m leasing 2024,
59727→Μπραχάμι/Irisbus Agora Diesel.
"""

from __future__ import annotations

DEPOTS = {
    "0": "ΡΟΥΦ",
    "1": "Βοτανικός",
    "3": "Πειραιάς",
    "4": "Ράλλη",
    "5": "Μπραχάμι",
    "6": "Ανθούσα",
    "7": "Λιόσια",
    "8": "Κόκκινος Μύλος",
    "9": "ΚΤΕΛ",
}

# ── Specific-depot ranges (full vehicle number) — checked FIRST ──
FULL_RANGES = [
    (10001, 10220, "Solaris 8,6m"),
    (10541, 10729, "N2"),
    (10954, 10954, "N2"),
    (16005, 16005, "MAN 12m leasing 2020"),
    (16018, 16019, "Solaris Urbino 12m"),
    (16020, 16020, "Solaris Urbino 18m"),
    (16021, 16021, "Solaris Urbino 12m"),
    (16023, 16023, "Solaris Urbino 12m"),
    (16025, 16029, "MAN 12m leasing 2020"),
    (16094, 16132, "Citaro 12m leasing 2020"),
    (30600, 30680, "Solaris 8,6m"),
    (30701, 30981, "Irisbus Agora Diesel 12m"),
    (40401, 40719, "GN"),
    (40821, 40940, "Solaris Urbino 18m"),
    (50701, 50981, "Irisbus Agora Diesel 12m"),
    (56031, 56087, "Volvo 12m leasing 2020"),
    (59701, 59981, "Irisbus Agora Diesel 12m"),
    (60001, 60220, "Solaris 12m 8,6"),
    (60600, 60680, "Solaris 12m 8,6"),
    (69001, 69200, "Irisbus Citelis CNG 12m"),
    (79001, 79200, "Irisbus Citelis CNG 12m"),
]

# ── Trolley-only base ranges (base = number WITHOUT first digit) ──
# These trolley models appear only at trolley depots (ΡΟΥΦ=0, Κόκκινος Μύλος=8).
# Listed in the fleet file as 06001-06112 and 09001-09051 (the leading 0 was the
# depot). Kept trolley-only so ΚΤΕΛ (9XXXX) buses aren't misclassified.
TROLLEY_BASE_RANGES = [
    (6001, 6112, "Neoplan N6014"),
    (9001, 9051, "Neoplan n6221"),
]

# ── Any-depot ranges (base = vehicle number WITHOUT the first depot digit) ──
BASE_RANGES = [
    (1161, 1260, "Urbanway 18m"),
    (1261, 1460, "Citymood 12m"),
    (2001, 2140, "Yutong E12"),
    (2141, 2240, "Yutong E9"),
    (4431, 4480, "Citaro C2 leasing 2024"),
    (4481, 4530, "MAN 12m leasing 2024"),
    (4531, 4630, "MAN 18m leasing 2024"),
    (6135, 6138, "MAN 12m leasing 2020"),
    (6139, 6141, "Citaro 12m leasing 2020"),
    (6143, 6151, "MAN 12m leasing 2020"),
    (6154, 6154, "Citaro 12m leasing 2020"),
    (6171, 6175, "Citaro leasing 2020"),
    (6183, 6191, "Solaris Urbino 18m"),
    (6192, 6196, "Citaro 18m leasing 2020"),
    (6198, 6203, "Solaris Urbino 18m"),
    (6206, 6208, "Citaro 18m leasing 2020"),
    (6213, 6215, "Citaro 18m leasing 2020"),
    (6219, 6221, "Solaris Urbino 18m"),
    (6223, 6227, "Solaris Urbino 12m"),
    (6231, 6277, "Irisbus Crossway LE leasing 2020"),
    (6293, 6293, "Solaris Urbino 12m"),
    (7001, 7112, "Vanhool 12m"),
    (8001, 8091, "Neoplan N6216"),
]


TROLLEY_DEPOTS = {"0", "8"}   # ΡΟΥΦ, Κόκκινος Μύλος — trolley depots


def _in_ranges(value: int, ranges) -> str | None:
    for lo, hi, name in ranges:
        if lo <= value <= hi:
            return name
    return None


def classify(vehicle_no) -> tuple[str | None, str | None]:
    """
    Returns (depot_name, vehicle_type) for a vehicle number.
    Either element may be None if it can't be determined.

    Depots 0 (ΡΟΥΦ) and 8 (Κόκκινος Μύλος) are trolley depots: any vehicle
    there is a trolley, so the type is marked accordingly — "(τρόλεϊ)" is
    appended to a detected model, or the type is "Τρόλεϊ" if none is found.
    """
    if vehicle_no is None:
        return None, None
    digits = "".join(ch for ch in str(vehicle_no) if ch.isdigit())
    if not digits:
        return None, None

    first = digits[0]
    depot = DEPOTS.get(first)

    try:
        full = int(digits)
    except ValueError:
        return depot, None

    # 1) Specific-depot ranges (full number)
    vtype = _in_ranges(full, FULL_RANGES)

    # 2) Any-depot ranges (strip first digit → base)
    if vtype is None and len(digits) >= 2:
        try:
            base = int(digits[1:])
            vtype = _in_ranges(base, BASE_RANGES)
            # 3) Trolley-only models, only at trolley depots (ΡΟΥΦ, Κόκκινος Μύλος)
            if vtype is None and first in TROLLEY_DEPOTS:
                vtype = _in_ranges(base, TROLLEY_BASE_RANGES)
        except ValueError:
            pass

    # Trolley depots: whatever model is there is a trolley model
    if first in TROLLEY_DEPOTS:
        vtype = f"{vtype} (τρόλεϊ)" if vtype else "Τρόλεϊ"

    return depot, vtype


if __name__ == "__main__":
    for v in ["79085", "10562", "54493", "59727", "11161", "31261", "12001"]:
        print(v, "→", classify(v))
