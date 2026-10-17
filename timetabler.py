"""Convert Northwest Airlines system timetable PDFs into CSV files.

Reads every PDF in ./timetables and writes ./csv/<pdf name>.csv with the columns
depiata,arriata,deptime,arrtime,flight_number,equipment,frequency,leg

PDF file names must contain the timetable's release date as YYYY-MM-DD.
"""

import csv
import difflib
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pdfminer.pdftypes
import pdfplumber

ROOT = Path(__file__).resolve().parent
TIMETABLE_DIR = ROOT / "timetables"
CSV_DIR = ROOT / "csv"
OCR_CACHE_DIR = ROOT / "ocr_cache"

FIELDS = ["depiata", "arriata", "deptime", "arrtime", "flight_number", "equipment", "frequency", "leg"]
EXCLUDED_EQUIPMENT = {"AIR", "KLM"}

# Some pages have trailing junk after the ASCII85 "~>" end-of-data marker, which
# pdfminer's decoder rejects. Cut the stream off at the marker before decoding.
_ascii85decode = pdfminer.pdftypes.ascii85decode


def _lenient_ascii85decode(data: bytes) -> bytes:
    end = data.find(b"~>")
    return _ascii85decode(data[:end] if end >= 0 else data)


pdfminer.pdftypes.ascii85decode = _lenient_ascii85decode

# Each page is two side-by-side columns, each headed
# "FM/TO DEP ARR FLT EQP FRQ ST MI" (some years add a meal column, ML, before MI).
# Column positions differ between years, so they are read from each page's header.
HEADER_LABELS = ["FM/TO", "DEP", "ARR", "FLT", "EQP", "FRQ", "ST", "ML", "MI"]
OPTIONAL_LABELS = {"ML"}
LINE_TOLERANCE = 2.0     # words whose tops differ by less than this share a line
# Adjacent fields are sometimes printed with almost no gap between them
# ("810a1015a", "KLMX246"), so words are split on small gaps.
WORD_GAP = 1.0

TIME = r"\d{1,4}[apnm]"
# Airport code, departure and arrival; these can run together ("CDG1135a", "810a1015a").
ROUTE_RE = re.compile(rf"^([A-Z]{{3}}) ?({TIME}) ?({TIME})$")
FREQUENCY_RE = re.compile(r"^(X?[1-7]+)?$")
# "LGWRes:" also occurs. On scans a speck may follow the code ("Butte, MT BTM E"),
# and "Res:" may be lost before the phone number ("Mexico City, Mexico MEX 202-4444").
CITY_RE = re.compile(r"^(?=.*[a-z]).+? ?([A-Z]{3})(?: ?Res:.*| \S| [\d(][\d() -]*)?$")
# 1990 puts the code first: "GPT Gulfport/Biloxi", "HNL Honolulu Res: (808) 955-2255", or just "HLN".
CODE_FIRST_CITY_RE = re.compile(r"^([A-Z]{3})(?: +[A-Z][a-z].*)?$")
# "Above Eff. 03/09", "Above Disc. 03/08", "Above Eff. 03/09 and Disc. 03/28"
NOTE_RE = re.compile(r"^Above (?:Eff\. (\d\d/\d\d)|Disc\. (\d\d/\d\d)|Eff\. (\d\d/\d\d) and Disc\. (\d\d/\d\d))$")
OCR_FMTO_RE = re.compile(r"^F[MN]/T[O0]$")
RELEASE_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
ALL_DAYS = frozenset(range(1, 8))


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float = 0.0
    bottom: float = 0.0

    @property
    def middle(self):
        return (self.top + self.bottom) / 2


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
    """Flights that don't fit the merge or leg rules, collected for review."""
    issues: dict = field(default_factory=lambda: defaultdict(dict))

    def add(self, kind, key, lines):
        """Record a flight (identified by key) under kind, shown as the given lines."""
        self.issues[kind][key] = lines


def entry_lines(group):
    return [f"{e.depiata}: {e.text}" + (f"   [{e.note}]" if e.note else "") for e in group]


def row_line(r):
    stops = f" {r['stops']} stop(s)" if r["stops"] else ""
    return (f"{r['depiata']}-{r['arriata']} {r['deptime']} {r['arrtime']} {r['flight_number']} "
            f"{r['equipment']} {r['frequency'] or 'daily'}{stops}")


def header_fields(page):
    """Return the header row's bottom and, per column, {label: (x0, x1)} of each header label.

    Returns (None, []) for pages without a timetable header (e.g. covers).
    """
    starts = [w for w in page.extract_words() if w["text"] == "FM/TO"]
    if not starts:
        return None, []
    top = starts[0]["top"]
    chars = sorted((c for c in page.chars if abs(c["top"] - top) < 1 and c["text"].strip()),
                   key=lambda c: c["x0"])
    xs = sorted(w["x0"] for w in starts if abs(w["top"] - top) < 1)
    columns = []
    for x, next_x in zip(xs, xs[1:] + [page.width]):
        col_chars = [c for c in chars if x - 1 <= c["x0"] < next_x - 1]
        # Labels can run together ("EQPFRQ"), so find them in the header's characters.
        text = "".join(c["text"] for c in col_chars)
        fields, pos = {}, 0
        for label in HEADER_LABELS:
            i = text.find(label, pos)
            if i < 0:
                if label not in OPTIONAL_LABELS:
                    sys.exit(f"Page {page.page_number}: no {label} in column header {text!r}")
                continue
            fields[label] = (col_chars[i]["x0"], col_chars[i + len(label) - 1]["x1"])
            pos = i + len(label)
        columns.append(fields)
    return max(c["bottom"] for c in chars), columns


def column_lines(page):
    """Yield (words, header fields) for each text line of the page, left column first."""
    body_top, columns = header_fields(page)
    for i, fields in enumerate(columns):
        # Columns meet halfway between one column's MI and the next column's FM/TO.
        x_start = 0 if i == 0 else (columns[i - 1]["MI"][1] + fields["FM/TO"][0]) / 2
        x_end = page.width if i == len(columns) - 1 else (fields["MI"][1] + columns[i + 1]["FM/TO"][0]) / 2
        words = page.crop((x_start, body_top, x_end, page.height)).extract_words(x_tolerance=WORD_GAP)
        words.sort(key=lambda w: (w["top"], w["x0"]))
        line, line_top = [], None
        for w in words:
            if line_top is not None and abs(w["top"] - line_top) >= LINE_TOLERANCE:
                yield sorted(line, key=lambda w: w.x0), fields
                line = []
            if not line:
                line_top = w["top"]
            line.append(Word(w["text"], w["x0"], w["x1"]))
        if line:
            yield sorted(line, key=lambda w: w.x0), fields


def parse_flight(words, fields):
    """Split a flight line into its fields by position under the column header.

    Returns (arriata, deptime, arrtime, flight_number, equipment, frequency, stops),
    or None if the line isn't a flight.
    """
    route, flight, equipment = [], [], []
    frequency = stops = ""
    for w in words:
        if w.x1 <= fields["ARR"][1] + 1:
            route.append(w.text)      # times are right-aligned under DEP and ARR
        elif w.x0 < fields["EQP"][0] - 2:
            flight.append(w.text)     # right-aligned under FLT; "KL685" may split in two
        elif w.x0 < fields["FRQ"][0] - 1:
            equipment.append(w.text)
        elif w.x1 > fields["MI"][0]:
            pass                      # miles, right-aligned under MI
        elif "ML" in fields and w.text.isalpha() and w.x0 >= fields["ML"][0] - 3:
            pass                      # meal code
        elif w.x0 >= fields["ST"][0] - 2:
            stops = w.text
        else:
            frequency = w.text
    m = ROUTE_RE.match(" ".join(route))
    if not m or not flight or len(equipment) != 1:
        return None
    if not FREQUENCY_RE.match(frequency) or not re.fullmatch(r"\d?", stops):
        sys.exit(f"Can't read frequency/stops in: {' '.join(w.text for w in words)!r}")
    return (*m.groups(), "".join(flight), equipment[0], frequency, stops)


# Scanned timetables (1998) are page images, read with OCR. OCR takes about a
# minute a page, so its output is cached in ./ocr_cache/<pdf name>.json.
SCAN_DPI = 400
OCR_VERSION = 2  # bump when the OCR changes, to discard cached OCR
OCR_PARAMS = {
    "Global.log_level": "warning",
    "Global.max_side_len": 4000,   # read the page at full resolution
    "Global.use_cls": False,       # text is upright; the angle classifier turns "X7" into "LX"
    "Det.max_candidates": 20000,   # a page has ~3000 words; the default stops at 1000
    "Det.use_dilation": False,     # dilation merges single characters stacked in the ST/ML columns
}
HEADER_PROBE_HEIGHT = 60  # points; the FM/TO header is near the top of timetable pages
OCR_COLUMN_MARGIN = 3     # a column starts this far left of its FM/TO label
OCR_CODE_INDENT = 3       # airport codes start this far right of the FM/TO label
OCR_LINE_PITCH = 4.87     # points between lines
OCR_LINE_GAP = 2.0        # words on one line differ in height by well under this
# Airport code, departure, arrival, flight number and aircraft; OCR may run any of them together,
# or split a time after its hours in the 1990 typeface ("5 10p", "10 15a"). The aircraft may run
# into the frequency ("AIRX6", "74716").
OCR_TIME = rf"\d{{1,2}} \d\d[apnm]|{TIME}"
OCR_HEAD_RE = re.compile(rf"^([A-Z]{{3}}) ?({OCR_TIME}) ?({OCR_TIME}) ?(\d{{1,4}}) ?([A-Z0-9]{{3}})(?= |$|X|\d)")
# Aircraft types in the scanned timetables; anything else is taken to be misread.
SCAN_EQUIPMENT = {"320", "727", "72S", "744", "747", "757", "AIR", "ARJ", "D10", "D95", "D9S", "DC9",
                  "KLM", "M80", "SF3"}
FLIGHT_LIKE_RE = re.compile(r"\d[apnm].*\d[apnm]\b")  # has two times ("505p550a" too)
SCAN_ISSUES = {"line couldn't be read", "city heading couldn't be read", "note not under a flight",
               "airport isn't one of the timetable's cities", "city isn't any flight's destination",
               "correction not used"}
# Lines the OCR misreads, checked against the scan: {pdf name: {(page, line as read): line as printed}}.
OCR_CORRECTIONS = {
    "NW-schedule-1998-12-18": {
        (8, "DF3Q 905a 1030a 3436 SF3 X67 1 377"): "DBQ 905a 1030a 3436 SF3 X67 1 377",
        (10, "DTW 1745p 924p 1469 D9S X6 370"): "DTW 745p 924p 1469 D9S X6 370",
        (14, "MEM 1500p 655p 5814 SF3 355"): "MEM 500p 655p 5814 SF3 355",
    },
    "NW-schedule-1990-07-01": {
        (11, "MEM 11125a 117p 1158 D9S S 625"): "MEM 1125a 117p 1158 D9S S 625",
        (12, "LAX 950a 955a 747 3 1 D 6788"): "LAX 850a 955a 2 747 146 1 D 6788",
    },
}
OCR_SPECKS_RE = re.compile(r"[.,:;'`~_/-]")  # specks on the scan, read as punctuation
# "DISC. 12/24", "EFF. 12/25" under a flight line, like the later "Above Disc./Eff." notes.
# A speck may sit before the date ("DISC. .7/31").
SCAN_NOTE_RE = re.compile(r"^(?:(EFF|DISC)\.[ .]*(\d\d?/\d\d?))(?: (EFF|DISC)\.[ .]*(\d\d?/\d\d?))?$")


def is_scanned(page):
    """A scanned page is a single image covering the page."""
    return any(im["width"] > 0.8 * page.width and im["height"] > 0.8 * page.height for im in page.images)


def ocr_pages(path, pdf):
    """Return {page number: [[text, x0, top, x1, bottom], ...]} for the PDF's scanned timetable pages.

    Pages without a timetable header (covers, ads) map to an empty list.
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache_path = OCR_CACHE_DIR / f"{path.stem}.json"
    pages = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache["sha256"] == digest and cache["version"] == OCR_VERSION:
            pages = {int(n): boxes for n, boxes in cache["pages"].items()}
    todo = [page for page in pdf.pages if page.page_number not in pages and is_scanned(page)]
    if not todo:
        return pages

    try:
        import numpy as np
        from rapidocr import RapidOCR
    except ImportError:
        sys.exit(f"{path.name} is a scan and needs OCR: pip install -r requirements.txt")
    engine = RapidOCR(params=OCR_PARAMS)
    scale = SCAN_DPI / 72

    def read(image):
        """OCR boxes as [text, x0, top, x1, bottom] in pixels."""
        result = engine(np.array(image), use_det=True, use_cls=False, use_rec=True)  # flags persist between calls
        return [[text, float(min(x for x, _ in box)), float(min(y for _, y in box)),
                 float(max(x for x, _ in box)), float(max(y for _, y in box))]
                for box, text in zip(result.boxes if result.boxes is not None else [], result.txts or [])]

    def split_stacked(image, boxes):
        """Re-read boxes that span several lines one character at a time.

        Single characters stacked in consecutive lines (stops "2", "2", "1" in
        the ST column) can be found as one tall box, and then misread ("121").
        The characters are separated by blank pixel rows, so each is cut out
        and read on its own.
        """
        heights = sorted(b[4] - b[2] for b in boxes)
        line_height = heights[len(heights) // 2]
        pitch = OCR_LINE_PITCH * scale
        gray = np.array(image.convert("L"))
        out = [b for b in boxes if b[4] - b[2] <= 1.6 * line_height]
        for text, x0, top, x1, bottom in boxes:
            if bottom - top <= 1.6 * line_height:
                continue
            # Widened by half a line, so characters cut by the box's edge are whole.
            t, b, l, r = int(top - pitch / 2), int(bottom + pitch / 2), int(x0) - 2, int(x1) + 2
            ink = list((gray[t:b, l:r] < 140).any(axis=1)) + [False]
            start = None
            for i, inked in enumerate(ink):
                if inked and start is None:
                    start = i
                elif not inked and start is not None:
                    c_top, c_bottom, start = t + start, t + i, None
                    middle = (c_top + c_bottom) / 2
                    if c_bottom - c_top < line_height / 2 or any(
                            o[2] < middle < o[4] and o[1] < (l + r) / 2 < o[3] for o in out):
                        continue  # a speck, or already read as part of a line
                    glyph = np.stack([gray[c_top - 4:c_bottom + 4, l:r]] * 3, axis=-1)
                    result = engine(glyph, use_det=False, use_cls=False, use_rec=True)
                    if result.txts:
                        out.append([result.txts[0], x0, float(c_top), x1, float(c_bottom)])
        return out

    OCR_CACHE_DIR.mkdir(exist_ok=True)
    for page in todo:
        image = page.to_image(resolution=SCAN_DPI).original.convert("RGB")
        header = image.crop((0, 0, image.width, int(HEADER_PROBE_HEIGHT * scale)))
        if any(OCR_FMTO_RE.match(t) for b in read(header) for t in b[0].split()):
            print(f"  reading page {page.page_number} of {len(pdf.pages)} with OCR", flush=True)
            boxes = split_stacked(image, read(image))
            pages[page.page_number] = [[text, *(v / scale for v in box)] for text, *box in boxes]
        else:
            pages[page.page_number] = []
        # Saved after every page, so an interrupted run carries on where it stopped.
        cache_path.write_text(json.dumps({"sha256": digest, "version": OCR_VERSION, "pages": pages}),
                              encoding="utf-8")
    return pages


def ocr_words(boxes):
    """Turn OCR boxes into Words, splitting boxes that hold several words ("MSP 1045a", "D 1975").

    A box's width is shared out evenly between its characters to place each word.
    A "|" is the rule between two columns ("MI|FM/TO"), so it separates words too.
    """
    words = []
    for text, x0, top, x1, bottom in boxes:
        char_width = (x1 - x0) / len(text)
        for m in re.finditer(r"[^\s|]+", text):
            words.append(Word(fix_ocr_word(m.group()), x0 + m.start() * char_width, x0 + m.end() * char_width,
                              top, bottom))
    return words


def fix_ocr_word(text):
    """Fix common OCR mix-ups in codes ("GCl", "AiR") and times ("lO05a")."""
    # Accents are specks too ("ANČ").
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    if re.fullmatch(r"[A-Zil]{3}", text):
        return text.translate(str.maketrans("il", "II"))
    if re.fullmatch(r"[\dOlI]{3,4}[apnm]", text):
        return text.translate(str.maketrans("OlI", "011"))
    return text


def ocr_header_fields(words):
    """Return, per column, {label: (x0, x1)} of each header label, plus the header's "bottom".

    The header labels are often misread ("PEP", "FRO", "5T", "Ml"), so they're
    matched by their order after each FM/TO rather than by spelling.
    """
    starts = sorted((w for w in words if OCR_FMTO_RE.match(w.text)), key=lambda w: w.x0)
    columns = []
    for start, next_start in zip(starts, starts[1:] + [None]):
        # The two facing pages of a scan can sit at slightly different heights.
        labels = sorted((part for w in words if abs(w.middle - start.middle) < 2 and w.x0 >= start.x0
                         and (next_start is None or w.x0 < next_start.x0)
                         for part in split_header_word(w)), key=lambda w: w.x0)
        if len(labels) != len(HEADER_LABELS):
            sys.exit(f"Can't read column header {' '.join(w.text for w in labels)!r}")
        fields = {label: (w.x0, w.x1) for label, w in zip(HEADER_LABELS, labels)}
        fields["bottom"] = max(w.bottom for w in labels)
        columns.append(fields)
    return columns


def split_header_word(w):
    """Split header labels printed or read run together ("EQPFRQ") into one Word each."""
    parts = re.findall(r"DEP|ARR|FLT|EQP|FRQ|FRO|ST|5T|ML|MI|Ml", w.text)
    if len(parts) < 2 or "".join(parts) != w.text:
        return [w]
    char_width = (w.x1 - w.x0) / len(w.text)
    words, pos = [], 0
    for part in parts:
        words.append(Word(part, w.x0 + pos * char_width, w.x0 + (pos + len(part)) * char_width, w.top, w.bottom))
        pos += len(part)
    return words


def column_drift(words, fields):
    """Return how far a scanned column drifts right per point down the page.

    Scans are slightly rotated, so a column can end up several points left or
    right of its header by the bottom of the page. The drift is measured along
    the left edge of the airport codes that start each flight line, followed
    down the column from the header.
    """
    x, points = fields["FM/TO"][0] + OCR_CODE_INDENT, []
    for w in sorted(words, key=lambda w: w.middle):
        if w.top > fields["bottom"] and re.match(r"[A-Z]{3}", w.text) and abs(w.x0 - x) < 2.5:
            x = w.x0
            points.append((w.middle, w.x0))
    if len(points) < 10:
        return 0.0
    mean_y = sum(y for y, _ in points) / len(points)
    mean_x = sum(x for _, x in points) / len(points)
    return (sum((y - mean_y) * (x - mean_x) for y, x in points)
            / sum((y - mean_y) ** 2 for y, _ in points))


def ocr_column_lines(words):
    """Like column_lines, for a scanned page's OCR words."""
    columns = ocr_header_fields(words)
    for fields in columns:
        fields["drift"] = column_drift(words, fields)
    # Boxes taller than a line (rules between columns) could join two lines together.
    heights = sorted(w.bottom - w.top for w in words)
    max_height = 1.6 * heights[len(heights) // 2] if heights else 0

    def straightened(w, fields):
        """w as if the page weren't rotated, so it lines up under fields."""
        dx = fields["drift"] * (w.middle - fields["bottom"])
        return Word(w.text, w.x0 - dx, w.x1 - dx, w.top, w.bottom)

    for i, fields in enumerate(columns):
        x_start = fields["FM/TO"][0] - OCR_COLUMN_MARGIN
        # The last column on a page stops after its mileage; there may be an ad beside it.
        x_end = (fields["MI"][1] + OCR_COLUMN_MARGIN if i + 1 == len(columns)
                 else columns[i + 1]["FM/TO"][0] - OCR_COLUMN_MARGIN)
        column = sorted((s for w in words if w.top > fields["bottom"] and w.bottom - w.top < max_height
                         for s in [straightened(w, fields)] if x_start <= (s.x0 + s.x1) / 2 < x_end),
                        key=lambda w: w.middle)
        # The scan is slightly crooked, so a line's words don't share an exact height:
        # a new line starts at the first clear vertical gap.
        line = []
        for w in column:
            if line and w.middle - line[-1].middle > OCR_LINE_GAP:
                yield sorted(line, key=lambda w: w.x0), fields
                line = []
            line.append(w)
        if line:
            yield sorted(line, key=lambda w: w.x0), fields


def parse_ocr_flight(words, fields):
    """Like parse_flight, for a line of OCR words.

    Returns (flight, problem): flight as parse_flight returns it, or None with a
    description of the problem if the line looks like a flight but can't be read.
    """
    text = " ".join(w.text for w in words)
    if not FLIGHT_LIKE_RE.search(text):
        return None, None
    # Flight lines have no punctuation or lone lowercase letters, so those are specks on the scan.
    words = [Word(t, w.x0, w.x1) for w in words
             if (t := OCR_SPECKS_RE.sub("", w.text)) and not re.fullmatch("[a-z]", t)]
    # OCR sometimes repeats a character of the next word at a box's edge ("2 D 8" before
    # "8439"); the repeat lies mostly on top of that word.
    words = [w for w in words
             if not (len(w.text) == 1 and any(len(v.text) > 1 and min(w.x1, v.x1) - max(w.x0, v.x0)
                                              > (w.x1 - w.x0) / 2 for v in words))]
    text = " ".join(w.text for w in words)
    m = OCR_HEAD_RE.match(text)
    if not m:
        return None, "can't read"
    arriata, deptime, arrtime, flight_number, equipment = m.groups()
    deptime, arrtime = deptime.replace(" ", ""), arrtime.replace(" ", "")
    if equipment not in SCAN_EQUIPMENT:
        return None, f"unknown aircraft {equipment!r}"
    for t in (deptime, arrtime):
        if not valid_time(t):
            return None, f"invalid time {t!r}"

    # Everything after the aircraft is placed by position: frequency, stops, meal, miles.
    # Those can run together too ("L1448" is meal L and 1448 miles).
    pos, tail = 0, []
    for w in words:
        start, pos = pos, pos + len(w.text) + 1
        if pos - 1 <= m.end():
            continue
        # The aircraft can run into the frequency ("AIRX6"); only the part after it is tail.
        skip = max(m.end() - start, 0)
        char_width = (w.x1 - w.x0) / len(w.text)
        for part in re.finditer(r"X[1-7]+|[A-Z]+|\d+|\S", w.text[skip:]):
            tail.append(Word(part.group(), w.x0 + (skip + part.start()) * char_width,
                             w.x0 + (skip + part.end()) * char_width))
    frequency, stops = [], []
    for w in tail:
        if w.x0 > fields["MI"][1]:
            pass                                  # beyond the column (the page's edge or gutter)
        elif w.text.isdigit() and w.x1 > fields["MI"][0]:
            pass                                  # miles, right-aligned under MI
        elif w.text.isalpha() and not w.text.startswith("X"):
            pass                                  # meal code
        elif w.x0 < fields["ST"][0] - 3:
            frequency.append(w.text)
        elif w.x0 < fields["ML"][0] - 2:
            stops.append(w.text)
        else:
            return None, f"can't place {w.text!r}"
    frequency, stops = "".join(frequency), "".join(stops)
    if not FREQUENCY_RE.match(frequency):
        return None, f"can't read frequency {frequency!r}"
    if not re.fullmatch(r"\d?", stops):
        return None, f"can't read stops {stops!r}"
    return (arriata, deptime, arrtime, flight_number, equipment, frequency, stops), None


def valid_time(t):
    """'1045a' and '1200n' are valid; '1375p' and '1245n' aren't."""
    digits, suffix = t[:-1], t[-1]
    hours, minutes = int(digits[:-2] or 0), int(digits[-2:])
    if suffix in "nm":
        return digits == "1200"
    return 1 <= hours <= 12 and minutes < 60 and len(digits) >= 3


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


def page_lines(page, ocr_boxes, corrections):
    """Yield (text, flight, problem) for each text line of the page, column by column.

    flight is parse_flight's result. ocr_boxes is the page's OCR output if it's
    a scan; then problem describes a line that looks like a flight but can't be read.
    corrections maps (page number, OCR text) to the line as printed; each one
    used is removed from it.
    """
    if ocr_boxes is None:
        for words, fields in column_lines(page):
            yield " ".join(w.text for w in words), parse_flight(words, fields), None
    else:
        for words, fields in ocr_column_lines(ocr_words(ocr_boxes)):
            text = " ".join(w.text for w in words)
            corrected = corrections.pop((page.page_number, text), None)
            if corrected:
                words, text = corrected_words(words, corrected), corrected
            yield text, *parse_ocr_flight(words, fields)


def corrected_words(words, corrected):
    """Words for a corrected line, placed where the OCR found the words they replace.

    Unchanged words keep their place, a misread word's replacement takes its
    place, words missing from the correction are dropped, and words the OCR
    missed go between their neighbours.
    """
    new = corrected.split()
    result = []
    matcher = difflib.SequenceMatcher(a=[w.text for w in words], b=new, autojunk=False)
    for _, i1, i2, j1, j2 in matcher.get_opcodes():
        if j1 == j2:
            continue  # deleted
        if i1 == i2:
            # Added words go in the gap between their neighbours.
            x0 = words[i1 - 1].x1 if i1 else words[0].x0
            x1 = words[i1].x0 if i1 < len(words) else words[-1].x1
            ref = words[min(i1, len(words) - 1)]
            width = (x1 - x0) / (j2 - j1)
            for k, text in enumerate(new[j1:j2]):
                result.append(Word(text, x0 + k * width, x0 + (k + 1) * width, ref.top, ref.bottom))
            continue
        if i2 - i1 == j2 - j1:
            result.extend(Word(text, w.x0, w.x1, w.top, w.bottom) for w, text in zip(words[i1:i2], new[j1:j2]))
            continue
        # Share the replaced words' span evenly between their replacements.
        x0, x1 = words[i1].x0, words[i2 - 1].x1
        width = (x1 - x0) / (j2 - j1)
        for k, text in enumerate(new[j1:j2]):
            result.append(Word(text, x0 + k * width, x0 + (k + 1) * width, words[i1].top, words[i1].bottom))
    return result


def parse_note(text):
    """Return a schedule change note's (Eff., Disc.) 'MM/DD' dates, or None if text isn't a note."""
    m = NOTE_RE.match(text)
    if m:
        return m.group(1) or m.group(3), m.group(2) or m.group(4)
    m = SCAN_NOTE_RE.match(text)
    if m:
        dates = {m.group(1): m.group(2), m.group(3): m.group(4)}
        return dates.get("EFF"), dates.get("DISC")
    return None


def parse_pdf(path, release, report):
    """Return every non-excluded flight line in the PDF as an Entry, with its note attached."""
    entries = []
    cities = set()
    depiata = None
    last = None         # the flight line a note refers to
    unreadable = False  # the line above was a flight that couldn't be read
    corrections = dict(OCR_CORRECTIONS.get(path.stem, {}))

    with pdfplumber.open(path) as pdf:
        scanned = ocr_pages(path, pdf) if any(is_scanned(p) for p in pdf.pages) else {}
        for page in pdf.pages:
            for text, flight, problem in page_lines(page, scanned.get(page.page_number), corrections):
                where = f"page {page.page_number}, from {depiata}"
                after_unreadable, unreadable = unreadable, False

                note = parse_note(text)
                if note:
                    if last is None:
                        if after_unreadable:
                            continue  # the line it belongs to is already reported
                        if not scanned:
                            sys.exit(f"Note not under a flight on page {page.page_number}: {text!r}")
                        report.add("note not under a flight", (page.page_number, text), [f"{where}: {text}"])
                        continue
                    last.eff, last.disc = (note_date(d, release) for d in note)
                    last.note = text
                    last = None
                    continue
                if text.startswith("Above ") or (scanned and re.match(r"(?i)(EFF|DISC)\b", text)):
                    if not scanned:
                        sys.exit(f"Unrecognized note on page {page.page_number}: {text!r}")
                    report.add("line couldn't be read", (page.page_number, text),
                               [f"{where}: {text}   [unrecognized note]"])
                    last = None
                    continue

                if problem:
                    # Codeshare flights are dropped anyway, so they needn't be checked.
                    if not any(code in text for code in EXCLUDED_EQUIPMENT):
                        report.add("line couldn't be read", (page.page_number, text),
                                   [f"{where}: {text}   [{problem}]"])
                    last = None
                    unreadable = True
                    continue

                if flight and depiata is not None:
                    arriata, deptime, arrtime, flight_number, equipment, frequency, stops = flight
                    last = Entry(depiata, arriata, deptime, arrtime, flight_number, equipment,
                                 parse_days(frequency), stops, text)
                    entries.append(last)
                    continue

                last = None
                m = CODE_FIRST_CITY_RE.match(text) or CITY_RE.match(text)
                if m and not FLIGHT_LIKE_RE.search(text):
                    depiata = m.group(1)
                    cities.add(depiata)
                elif scanned and re.match(r"[A-Z][a-z]+.*, ", text):
                    # Looks like a city heading. Until the next one, flights would be
                    # filed under the previous city.
                    report.add("city heading couldn't be read", (page.page_number, text),
                               [f"{where}: {text}"])
                # Page footers, column headers, etc. are ignored.

    for (page_number, text), corrected in corrections.items():
        # Its line wasn't found; the OCR may have changed since it was written.
        report.add("correction not used", (page_number, text), [f"page {page_number}: {text} -> {corrected}"])

    if scanned:
        # A misread airport code usually isn't one of the timetable's cities,
        # and a misread city code isn't anywhere's destination.
        for e in entries:
            if e.arriata not in cities and e.equipment not in EXCLUDED_EQUIPMENT:
                report.add("airport isn't one of the timetable's cities", e.group_key, entry_lines([e]))
        destinations = {e.arriata for e in entries}
        for city in sorted(cities - destinations):
            report.add("city isn't any flight's destination", city,
                       [f"{city}: " + ", ".join(sorted({e.arriata for e in entries if e.depiata == city}))])
    return [e for e in entries if e.equipment not in EXCLUDED_EQUIPMENT]


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
            report.add("two entries effective the same date fly the same day", group[0].group_key, entry_lines(group))
        if any(e.equipment != top[0].equipment for e in operating):
            report.add("newer entry replaces a different aircraft on the same day", group[0].group_key, entry_lines(group))
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
        "leg": "",
        "stops": e.stops,
    } for e, days in merged.values()]


def resolve(entries, release, report):
    """Group entries by flight (origin, destination, flight number) and resolve each group."""
    groups = defaultdict(list)
    for e in entries:
        groups[e.group_key].append(e)

    # When a flight number is rerouted mid-timetable, its old segments end (every
    # entry has a Disc. date) and new ones start afterwards. Only the new routing is kept.
    new_service = defaultdict(list)  # flight number -> start dates of service with no Disc. date
    for e in entries:
        if e.disc is None:
            new_service[e.flight_number].append(e.eff or release)

    rows = []
    for group in groups.values():
        if all(e.disc for e in group):
            last_day = max(e.disc for e in group)
            if any(start > last_day for start in new_service[group[0].flight_number]):
                continue
        rows.extend(resolve_group(group, release, report))
    return rows


def assign_legs(rows, report):
    """Number the legs of each flight and drop the multi-stop rows.

    A flight that stops on the way is listed once per origin along its route:
    for flight 568 to MIA there is ABQ-MIA with 1 stop and MSP-MIA nonstop.
    Ordering a destination's origins by stops (most first) gives the route,
    ABQ -> MSP -> MIA, and so its legs, ABQ-MSP (1) and MSP-MIA (2). Those legs
    are themselves nonstop rows, so only nonstop rows are kept.

    - A nonstop row that isn't part of a longer route is leg 1.
    - A leg the timetable doesn't sell (no nonstop row for it) still counts:
      YXE -> YQR -> MSP with no YXE-YQR row still makes YQR-MSP leg 2.
    - If two origins have the same stop count to a destination (e.g. BWI and
      CMH both feeding MEM -> JAN), that flight's legs are left blank.
    """
    nonstop = defaultdict(list)                        # (flight, dep, arr) -> rows
    origins = defaultdict(lambda: defaultdict(set))    # (flight, arr) -> stops -> {dep}
    through = defaultdict(list)                        # (flight, arr) -> rows, for the report
    for r in rows:
        stops = int(r["stops"] or 0)
        origins[(r["flight_number"], r["arriata"])][stops].add(r["depiata"])
        through[(r["flight_number"], r["arriata"])].append(r)
        if stops == 0:
            nonstop[(r["flight_number"], r["depiata"], r["arriata"])].append(r)

    unnumbered = set()        # flights whose legs are left blank
    legs = defaultdict(dict)  # (flight, dep, arr) -> {leg number: destination it was derived from}
    for (flight, dest), by_stops in origins.items():
        most = max(by_stops)
        if most == 0:
            continue
        if any(len(deps) > 1 for deps in by_stops.values()):
            unnumbered.add(flight)
            continue
        route = sorted(by_stops, reverse=True)  # stop counts along the route, first origin first
        for s, next_s in zip(route, route[1:] + [None]):
            dep = next(iter(by_stops[s]))
            arr = dest if next_s is None else next(iter(by_stops[next_s]))
            if (flight, dep, arr) in nonstop:
                legs[(flight, dep, arr)][most - s + 1] = dest

    for key, segment_rows in nonstop.items():
        if key[0] in unnumbered:
            continue
        numbers = legs.get(key, {1: None})
        if len(numbers) > 1:
            lines = [row_line(r) for r in segment_rows]
            for leg, dest in numbers.items():
                lines.append(f"  leg {leg} on the route to {dest}:")
                lines.extend("    " + row_line(r) for r in through[(key[0], dest)])
            report.add("same nonstop row is a different leg on different routes", key, lines)
        for r in segment_rows:
            r["leg"] = str(min(numbers))

    return [r for r in rows if not r["stops"]]


def print_report(report, examples=3):
    """Print a few examples of each kind of issue, or all of them for scan reading problems."""
    for kind, flights in report.issues.items():
        if kind in SCAN_ISSUES:
            print(f"  {kind}: {len(flights)} lines, check them against the scan:")
            shown = flights.values()
        else:
            print(f"  {kind}: {len(flights)} flights, e.g.")
            shown = list(flights.values())[:examples]
        for lines in shown:
            for line in lines:
                print(f"      {line}")
            print()


def main():
    CSV_DIR.mkdir(exist_ok=True)
    pdfs = sorted(TIMETABLE_DIR.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs found in {TIMETABLE_DIR}")
    for pdf_path in pdfs:
        release = release_date_of(pdf_path)
        report = Report()
        rows = assign_legs(resolve(parse_pdf(pdf_path, release, report), release, report), report)
        out_path = CSV_DIR / f"{pdf_path.stem}.csv"
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"{pdf_path.name}: wrote {len(rows)} flights to {out_path.relative_to(ROOT)}")
        if report.issues:
            print("Situations not covered by the merge or leg rules:")
            print_report(report)


if __name__ == "__main__":
    main()
