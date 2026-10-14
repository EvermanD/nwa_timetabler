"""Convert Northwest Airlines system timetable PDFs into CSV files.

Reads every PDF in ./timetables and writes ./csv/<pdf name>.csv with the columns
depiata,arriata,deptime,arrtime,flight_number,equipment,frequency,leg
"""

import csv
import re
import sys
from dataclasses import dataclass
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
NOTE_RE = re.compile(r"^Above (?:Eff\.|Disc\.)")


@dataclass
class Word:
    text: str
    x0: float
    x1: float


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


def parse_pdf(path):
    """Return the list of flight rows (dicts keyed by FIELDS) found in the PDF."""
    rows = []
    depiata = None
    pending = None  # last flight seen; held back until we know no "Above ..." note follows it

    def flush():
        nonlocal pending
        if pending is not None and pending["equipment"] not in EXCLUDED_EQUIPMENT:
            rows.append(pending)
        pending = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for words in column_lines(page):
                text = " ".join(w.text for w in words)

                if NOTE_RE.match(text):
                    # Effective/discontinued note: discard the flight directly above it.
                    pending = None
                    continue

                m = FLIGHT_RE.match(text)
                if m and depiata is not None:
                    flush()
                    frequency, _stops, _miles = parse_tail(words[5:])
                    pending = {
                        "depiata": depiata,
                        "arriata": m.group(1),
                        "deptime": m.group(2),
                        "arrtime": m.group(3),
                        "flight_number": m.group(4),
                        "equipment": m.group(5),
                        "frequency": frequency,
                        "leg": "",  # TODO: derive legs for multi-stop flights
                    }
                    continue

                m = CITY_RE.match(text)
                if m:
                    flush()
                    depiata = m.group(1)
                    continue

                # Page footers, column headers, etc. are ignored.
        flush()
    return rows


def main():
    CSV_DIR.mkdir(exist_ok=True)
    pdfs = sorted(TIMETABLE_DIR.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs found in {TIMETABLE_DIR}")
    for pdf_path in pdfs:
        rows = parse_pdf(pdf_path)
        out_path = CSV_DIR / f"{pdf_path.stem}.csv"
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"{pdf_path.name}: wrote {len(rows)} flights to {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
