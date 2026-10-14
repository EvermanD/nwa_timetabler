"""Convert Northwest Airlines system timetable PDFs into CSV files.

Reads every PDF in ./timetables and writes ./csv/<pdf name>.csv with the columns
depiata,arriata,deptime,arrtime,flight_number,equipment,frequency,leg

PDF file names must contain the timetable's release date as YYYY-MM-DD.
"""

import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pdfminer.pdftypes
import pdfplumber

ROOT = Path(__file__).resolve().parent
TIMETABLE_DIR = ROOT / "timetables"
CSV_DIR = ROOT / "csv"

FIELDS = ["depiata", "arriata", "deptime", "arrtime", "flight_number", "equipment", "frequency", "leg"]
EXCLUDED_EQUIPMENT = {"AIR", "KLM"}

# Some pages have trailing junk after the ASCII85 "~>" end-of-data marker, which
# pdfminer's decoder rejects. Cut the stream off at the marker before decoding.
_ascii85decode = pdfminer.pdftypes.ascii85decode


def _lenient_ascii85decode(data: bytes) -> bytes:
    end = data.find(b"~>")
    return _ascii85decode(data[:end] if end >= 0 else data)


pdfminer.pdftypes.ascii85decode = _lenient_ascii85decode

# Each page is two side-by-side columns with the header
# "FM/TO DEP ARR FLT EQP FRQ ST MI". These offsets are relative to the left
# edge of a column and are the same on every page of the timetable.
COLUMN_SPLIT_X = 176.0   # x position separating the left and right columns
RIGHT_COLUMN_OFFSET = 123.5  # right column header sits this far right of the left one
BODY_TOP = 68.0          # just below the column header row
LINE_TOLERANCE = 2.0     # words whose tops differ by less than this share a line
ST_X0 = 156.0 - 2.0      # stop counts start at or after the ST header
MI_X0 = 168.0            # mileage is right-aligned, ending past the MI header

TIME = r"\d{1,4}[apnm]"
FLIGHT_RE = re.compile(rf"^([A-Z]{{3}}) ({TIME}) ({TIME}) (\S+) (\S+)\b")
CITY_RE = re.compile(r"^(?=.*[a-z]).+? ([A-Z]{3})(?: Res:.*)?$")
# "Above Eff. 03/09", "Above Disc. 03/08", "Above Eff. 03/09 and Disc. 03/28"
NOTE_RE = re.compile(r"^Above (?:Eff\. (\d\d/\d\d)|Disc\. (\d\d/\d\d)|Eff\. (\d\d/\d\d) and Disc\. (\d\d/\d\d))$")
RELEASE_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
ALL_DAYS = frozenset(range(1, 8))


@dataclass
class Word:
    text: str
    x0: float
    x1: float


@dataclass
class Entry:
    """One flight line from the PDF, plus the Eff./Disc. note below it (if any)."""
    depiata: str
    arriata: str
    deptime: str
    arrtime: str
    flight_number: str
    equipment: str
    days: frozenset
    stops: str
    text: str
    eff: date = None   # None means effective from the release date
    disc: date = None
    note: str = ""

    @property
    def group_key(self):
        return (self.depiata, self.arriata, self.flight_number)

    @property
    def service_key(self):
        """Entries with the same service_key can be merged into one row."""
        return (self.deptime, self.arrtime, self.equipment, self.stops)


@dataclass
class Report:
    """Flights whose notes don't fit the merge rules, collected for review."""
    issues: dict = field(default_factory=lambda: defaultdict(dict))

    def add(self, kind, group):
        self.issues[kind][id(group)] = group


def column_lines(page):
    """Yield each text line of the page as a list of Words, left column first.

    Word x positions are made relative to the column's left edge so both
    columns can be parsed with the same offsets.
    """
    for x_start, x_end in ((0, COLUMN_SPLIT_X), (COLUMN_SPLIT_X, page.width)):
        offset = 0 if x_start == 0 else RIGHT_COLUMN_OFFSET
        words = page.crop((x_start, BODY_TOP, x_end, page.height)).extract_words()
        words.sort(key=lambda w: (w["top"], w["x0"]))
        line, line_top = [], None
        for w in words:
            if line_top is not None and abs(w["top"] - line_top) >= LINE_TOLERANCE:
                yield sorted(line, key=lambda w: w.x0)
                line = []
            if not line:
                line_top = w["top"]
            line.append(Word(w["text"], w["x0"] - offset, w["x1"] - offset))
        if line:
            yield sorted(line, key=lambda w: w.x0)


def parse_tail(words):
    """Split the words after the equipment column into (frequency, stops, miles)."""
    frequency = stops = miles = ""
    for w in words:
        if w.x1 > MI_X0:
            miles = w.text
        elif w.x0 >= ST_X0:
            stops = w.text
        else:
            frequency = w.text
    return frequency, stops, miles


def parse_days(frequency):
    """'' -> every day, 'X67' -> {1,2,3,4,5}, '345' -> {3,4,5}."""
    if not frequency:
        return ALL_DAYS
    digits = frozenset(int(c) for c in frequency.lstrip("X"))
    return ALL_DAYS - digits if frequency.startswith("X") else digits


def format_days(days):
    """Shortest frequency code for a set of days; ties use the X form, as the PDFs do."""
    if days == ALL_DAYS:
        return ""
    listed = "".join(str(d) for d in sorted(days))
    excluded = "X" + "".join(str(d) for d in sorted(ALL_DAYS - days))
    return listed if len(listed) < len(excluded) else excluded


def release_date_of(path):
    m = RELEASE_DATE_RE.search(path.stem)
    if not m:
        sys.exit(f"Can't find a YYYY-MM-DD release date in the file name {path.name}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def note_date(mmdd, release):
    """Turn an 'MM/DD' note date into a date, rolling into the next year if needed."""
    if mmdd is None:
        return None
    month, day = (int(x) for x in mmdd.split("/"))
    d = date(release.year, month, day)
    if (release - d).days > 183:
        d = date(release.year + 1, month, day)
    return d


def parse_pdf(path, release):
    """Return every non-excluded flight line in the PDF as an Entry, with its note attached."""
    entries = []
    depiata = None
    last = None  # the flight line an "Above ..." note refers to

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for words in column_lines(page):
                text = " ".join(w.text for w in words)

                if text.startswith("Above "):
                    m = NOTE_RE.match(text)
                    if not m or last is None:
                        sys.exit(f"Unrecognized note on page {page.page_number}: {text!r}")
                    last.eff = note_date(m.group(1) or m.group(3), release)
                    last.disc = note_date(m.group(2) or m.group(4), release)
                    last.note = text
                    last = None
                    continue

                m = FLIGHT_RE.match(text)
                if m and depiata is not None:
                    frequency, stops, _miles = parse_tail(words[5:])
                    last = Entry(depiata, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5),
                                 parse_days(frequency), stops, text)
                    if last.equipment not in EXCLUDED_EQUIPMENT:
                        entries.append(last)
                    continue

                last = None
                m = CITY_RE.match(text)
                if m:
                    depiata = m.group(1)
                # Page footers, column headers, etc. are ignored.
    return entries


def resolve_group(group, release, report):
    """Collapse all entries for one flight into the rows in effect after its last change.

    The flight is taken as it runs in its final week: the week after its last
    Eff./Disc. date, or, if every entry is discontinued, the week up to and
    including its last day of service (dropped if that is the release date).

    - On each day of that week, the entry operating with the latest effective
      date wins (an entry with no Eff. note is effective from the release date).
    - Days that end up on the same equipment, times and stops merge into one row;
      different times or stops stay as separate rows.
    """
    def eff(e):
        return e.eff or release

    if any(e.disc is None for e in group):
        last_change = max([release] + [d for e in group for d in (e.eff, e.disc) if d])
        week_start = last_change + timedelta(days=1)
    else:
        last_day = max(e.disc for e in group)
        if last_day <= release:
            return []
        week_start = last_day - timedelta(days=6)

    winners = {}
    for d in (week_start + timedelta(days=i) for i in range(7)):
        operating = [e for e in group
                     if eff(e) <= d and (e.disc is None or d <= e.disc) and d.isoweekday() in e.days]
        if not operating:
            continue
        latest = max(eff(e) for e in operating)
        top = [e for e in operating if eff(e) == latest]
        if len({e.service_key for e in top}) > 1:
            report.add("two entries effective the same date fly the same day", group)
        if any(e.equipment != top[0].equipment for e in operating):
            report.add("newer entry replaces a different aircraft on the same day", group)
        winners[d.isoweekday()] = top[0]

    merged = {}
    for day, e in winners.items():
        merged.setdefault(e.service_key, (e, set()))[1].add(day)

    return [{
        "depiata": e.depiata,
        "arriata": e.arriata,
        "deptime": e.deptime,
        "arrtime": e.arrtime,
        "flight_number": e.flight_number,
        "equipment": e.equipment,
        "frequency": format_days(frozenset(days)),
        "leg": "",  # TODO: derive legs for multi-stop flights
    } for e, days in merged.values()]


def resolve(entries, release, report):
    """Group entries by flight (origin, destination, flight number) and resolve each group."""
    groups = defaultdict(list)
    for e in entries:
        groups[e.group_key].append(e)
    rows = []
    for group in groups.values():
        rows.extend(resolve_group(group, release, report))
    return rows


def print_report(report, examples=3):
    for kind, groups in report.issues.items():
        print(f"  {kind}: {len(groups)} flights, e.g.")
        for group in list(groups.values())[:examples]:
            for e in group:
                print(f"      {e.depiata}: {e.text}" + (f"   [{e.note}]" if e.note else ""))
            print()


def main():
    CSV_DIR.mkdir(exist_ok=True)
    pdfs = sorted(TIMETABLE_DIR.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs found in {TIMETABLE_DIR}")
    for pdf_path in pdfs:
        release = release_date_of(pdf_path)
        report = Report()
        rows = resolve(parse_pdf(pdf_path, release), release, report)
        out_path = CSV_DIR / f"{pdf_path.stem}.csv"
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"{pdf_path.name}: wrote {len(rows)} flights to {out_path.relative_to(ROOT)}")
        if report.issues:
            print("Situations not covered by the merge rules:")
            print_report(report)


if __name__ == "__main__":
    main()
