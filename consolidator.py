"""Consolidate the per-timetable CSV files into one file without repeated flights.

Reads every ./csv/*.csv whose name contains the timetable's release date as
YYYY-MM-DD (the files timetabler.py writes) and writes ./csv/consolidated.csv.

A flight is identified by its origin, destination, flight number and aircraft
type. For each flight only the rows from the latest timetable that lists it are
kept, with an extra `year` column giving that timetable's year. If that
timetable lists the flight more than once (different times on different days),
all of those rows are kept.
"""

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_DIR = ROOT / "csv"
OUTPUT_PATH = CSV_DIR / "consolidated.csv"

FIELDS = ["depiata", "arriata", "deptime", "arrtime", "flight_number", "equipment", "frequency", "leg"]
OUTPUT_FIELDS = FIELDS + ["year"]
KEY_FIELDS = ("depiata", "arriata", "flight_number", "equipment")
RELEASE_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def timetable_files():
    """Return (release date, path) for each timetable CSV, oldest first."""
    files = []
    for path in sorted(CSV_DIR.glob("*.csv")):
        if path == OUTPUT_PATH:
            continue
        m = RELEASE_DATE_RE.search(path.stem)
        if not m:
            print(f"Skipping {path.name}: no YYYY-MM-DD release date in its name")
            continue
        files.append((m.group(0), path))
    dates = [d for d, _ in files]
    repeated = sorted({d for d in dates if dates.count(d) > 1})
    if repeated:
        sys.exit(f"More than one CSV has the release date {', '.join(repeated)}; can't tell which is later")
    return sorted(files)


def sort_key(row):
    flight = row["flight_number"]
    return (row["depiata"], row["arriata"], int(flight) if flight.isdigit() else sys.maxsize, flight, row["equipment"])


def main():
    files = timetable_files()
    if not files:
        sys.exit(f"No timetable CSVs found in {CSV_DIR}")

    # key -> (release date, rows from that timetable). Files are read oldest
    # first, so a later timetable replaces an earlier one's rows.
    latest = {}
    total = 0
    for release, path in files:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = [c for c in FIELDS if c not in (reader.fieldnames or [])]
            if missing:
                sys.exit(f"{path.name} is missing the column(s) {', '.join(missing)}")
            rows_by_key = {}
            for row in reader:
                total += 1
                row = {c: row[c] for c in FIELDS}
                row["year"] = release[:4]
                rows_by_key.setdefault(tuple(row[c] for c in KEY_FIELDS), []).append(row)
        for key, rows in rows_by_key.items():
            latest[key] = (release, rows)

    out_rows = sorted((row for _, rows in latest.values() for row in rows), key=sort_key)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Read {total} rows from {len(files)} timetables")
    print(f"Wrote {len(out_rows)} rows ({len(latest)} distinct flights) to {OUTPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
