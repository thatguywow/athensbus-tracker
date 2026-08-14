"""
vehicle_classification.py — maps an OASA vehicle number to its depot
(αμαξοστάσιο) and vehicle type (τύπος οχήματος).

RULE (fleet reference «Αμαξοστάσια ΝΕΟ»): digit COUNT decides the family —
  • 4-digit numbers are TROLLEYS. First digit → depot:
      2/6/7 → ΡΟΥΦ,  8/9 → Κόκκινος Μύλος
    Type from the trolley full-number ranges below.
  • 5-digit numbers are BUSES. First digit → depot:
      1 Βοτανικός, 3 Πειραιάς, 4 Ράλλη, 5 Μπραχάμι, 6 Ανθούσα, 7 Λιόσια, 9 ΚΤΕΛ
    Type: specific full-number ranges first, then any-depot base ranges
    (base = number without the first digit).
  NOTE: the same numeric range can mean different things per family, e.g.
  2001-2140 → Yutong E12 as a 4-digit trolley AND X2001-X2140 Yutong E12 buses.
"""

from __future__ import annotations

BUS_DEPOTS = {
    "1": "Βοτανικός",
    "3": "Πειραιάς",
    "4": "Ράλλη",
    "5": "Μπραχάμι",
    "6": "Ανθούσα",
    "7": "Λιόσια",
    "9": "ΚΤΕΛ",
}

TROLLEY_DEPOTS = {
    "2": "ΡΟΥΦ",
    "6": "ΡΟΥΦ",
    "7": "ΡΟΥΦ",
    "8": "Κόκκινος Μύλος",
    "9": "Κόκκινος Μύλος",
}

# ── Trolleys (4-digit FULL numbers) ──
TROLLEY_RANGES = [
    (2001, 2140, "Yutong E12 2024"),
    (2241, 2365, "Yutong E12 2026"),
    (6001, 6112, "Neoplan N6014"),
    (7001, 7112, "Vanhool 12m"),
    (8001, 8091, "Neoplan N6216"),
    (9001, 9051, "Neoplan n6221"),
]

# ── Buses: specific-depot ranges (FULL 5-digit number) — checked FIRST ──
FULL_RANGES = [
    (10001, 10220, "Solaris 12m 8,6m"),
    (10541, 10729, "N2"),
    (10954, 10954, "N2"),
    (16005, 16005, "MAN 12m leasing 2020"),
    # Μετακινήθηκαν από 16xxx (Βοτανικός) σε 66xxx/46xxx (Ανθούσα/Ράλλη)
    (66018, 66019, "Solaris Urbino 12m"),
    (46020, 46020, "Solaris Urbino 18m"),
    (66021, 66021, "Solaris Urbino 12m"),
    (66023, 66023, "Solaris Urbino 12m"),
    (16025, 16029, "MAN 12m leasing 2020"),
    (16094, 16132, "Citaro 12m leasing 2020"),
    (30001, 30220, "Solaris 12m 8,6"),
    (30600, 30699, "Solaris 8,6m"),
    (30600, 30700, "Solaris 12m 8,6"),
    (30701, 30991, "Irisbus Agora Diesel 12m"),
    (40401, 40719, "GN"),
    (40821, 40940, "Solaris Urbino 18m"),
    (50701, 50991, "Irisbus Agora Diesel 12m"),
    (56031, 56087, "Volvo 12m leasing 2020"),
    (59701, 59991, "Irisbus Agora Diesel 12m"),
    (60001, 60220, "Solaris 12m 8,6"),
    (60600, 60700, "Solaris 12m 8,6"),
    (69001, 69200, "Irisbus Citelis CNG 12m"),
    (79001, 79200, "Irisbus Citelis CNG 12m"),
]

# ── Buses: any-depot ranges (base = 5-digit number WITHOUT the first digit) ──
BASE_RANGES = [
    (1161, 1260, "Urbanway 18m"),
    (1261, 1460, "Citymood 12m"),
    (2001, 2140, "Yutong E12 2024"),
    (2141, 2240, "Yutong E9"),
    (2241, 2365, "Yutong E12 2026"),
    (4431, 4480, "Citaro C2 leasing 2024"),
    (4481, 4530, "MAN 12m leasing 2024"),
    (4531, 4630, "MAN 18m leasing 2024"),
    (6135, 6138, "MAN 12m leasing 2020"),
    (6139, 6141, "Citaro 12m leasing 2020"),
    (6143, 6151, "MAN 12m leasing 2020"),
    (6154, 6154, "Citaro 12m leasing 2020"),
    (6171, 6175, "Citaro 12m leasing 2020"),
    (6183, 6191, "Solaris Urbino 18m"),
    (6192, 6196, "Citaro 18m leasing 2020"),
    (6198, 6203, "Solaris Urbino 18m"),
    (6206, 6208, "Citaro 18m leasing 2020"),
    (6213, 6215, "Citaro 18m leasing 2020"),
    (6219, 6221, "Solaris Urbino 18m"),
    (6223, 6227, "Solaris Urbino 12m"),
    (6231, 6277, "Irisbus Crossway LE leasing 2020"),
    (6293, 6293, "Solaris Urbino 12m"),
]


def _in_ranges(n: int, ranges) -> str | None:
    for lo, hi, name in ranges:
        if lo <= n <= hi:
            return name
    return None


def classify(vehicle_no: str) -> tuple[str | None, str | None]:
    """Return (depot, vehicle_type) — either may be None if unknown."""
    digits = "".join(ch for ch in str(vehicle_no) if ch.isdigit())
    if not digits:
        return None, None

    # ── 4-digit → trolley ──
    if len(digits) == 4:
        depot = TROLLEY_DEPOTS.get(digits[0])
        try:
            vtype = _in_ranges(int(digits), TROLLEY_RANGES)
        except ValueError:
            vtype = None
        if vtype:
            vtype += " (τρόλεϊ)"
        elif depot:
            vtype = "Τρόλεϊ"
        return depot, vtype

    # ── 5-digit → bus ──
    if len(digits) == 5:
        depot = BUS_DEPOTS.get(digits[0])
        vtype = None
        try:
            vtype = _in_ranges(int(digits), FULL_RANGES)
            if vtype is None:
                vtype = _in_ranges(int(digits[1:]), BASE_RANGES)
        except ValueError:
            pass
        return depot, vtype

    return None, None
