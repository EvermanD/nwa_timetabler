# nwa_timetabler

Converts Northwest Airlines system timetable PDFs into CSV files of flight segments. Works with digital nwa.com timetables
(published 2000 and later) and with scanned printed timetables (tested with the September 1987, April 1990,
July 1990 and December 1998 ones).

## Running

Requires Python 3 (tested with 3.13), `pdfplumber`, and for scanned
timetables `rapidocr` and `onnxruntime`.

```bash
pip install -r requirements.txt
python timetabler.py
```

The script reads every PDF in `timetables/` and writes one CSV per PDF to
`csv/`, named after the PDF (`timetables/NW-schedule-2008-02-14.pdf` becomes
`csv/NW-schedule-2008-02-14.csv`).

Each PDF's file name must contain the timetable's release date as
`YYYY-MM-DD` (e.g. `NW-schedule-2008-02-14.pdf` or
`NW-schedule-2002-06-07-pettit.pdf`). It's used to interpret the
`Above Eff.` / `Above Disc.` notes.

If the script finds a flight it can't resolve under the rules below, it
lists it under "Situations not covered by the merge or leg rules" when it
finishes. The CSV is still written.

## CSV format

```
depiata,arriata,deptime,arrtime,flight_number,equipment,frequency,leg
ABQ,MSP,204p,541p,568,320,X6,1
ABQ,MSP,204p,541p,568,319,6,1
MSP,MIA,710p,1140p,568,320,X126,2
```

| Column | Meaning |
|---|---|
| `depiata` | Departure airport code |
| `arriata` | Arrival airport code |
| `deptime` | Local departure time, as printed (`204p`, `1200n`) |
| `arrtime` | Local arrival time, as printed |
| `flight_number` | Flight number (`568`, `KL685`) |
| `equipment` | Aircraft type code (`320`, `CRJ`, `D9S`) |
| `frequency` | Days of operation, 1 = Monday to 7 = Sunday. Blank = daily, `X6` = every day except Saturday, `123` = Monday to Wednesday only |
| `leg` | Position of this segment in the flight's route (1 = first leg). Blank if it couldn't be determined |

Each row is one nonstop segment.

## How the timetable is interpreted

### Reading the PDF

- Each page has two columns, read left then right. Column positions are
  taken from each page's `FM/TO DEP ARR FLT EQP FRQ ST MI` header, so
  older layouts (including ones with a meal `ML` column) work too.
- A city line such as `Albuquerque, NM ABQ` sets the departure airport for
  the flight lines below it, until the next city line.
- Mileage and meal codes are ignored.
- Codeshare flights (equipment `AIR` or `KLM`) are dropped.

### Scanned timetables

A page that is a single full-page image is read with OCR instead (any text
layer the scan came with is ignored; it's incomplete). This takes about a
minute a page, so the result is saved in `ocr_cache/` and reused until the
PDF changes. Delete the cache file to read the scan again.

- The printed timetables put four columns on each PDF page (two facing
  pages). The headers are often misread (`PEP` for `DEP`) or run together
  (`EQPFRQ`), so columns are found from each `FM/TO` and the labels after
  it, in order. A `|` is the rule between columns.
- City headings come in either order: `Atlanta, GA ATL` (1998) or
  `GPT Gulfport/Biloxi` (July 1990), sometimes just the code (`HLN`, and
  every heading in April 1990).
- In 1987 there are no headings. The header reads `FM TO` and a city's code
  is printed at the start of its first flight line (`CWA MSP 1130a 1224p
  ...`), and again at the top of a column it continues into.
- Scans are slightly rotated, so a column can drift several points sideways
  by the bottom of the page. The drift is measured from the left edge of the
  airport codes and taken out before words are placed under the header.
- Frequency, stops, meal and mileage are told apart by position under the
  header, as in the digital timetables.
- Single characters stacked in consecutive lines (stops `2`, `2`, `1`) can
  be read as one tall word. Those are cut into separate characters and read
  one at a time.
- Punctuation and stray lowercase letters in a flight line are specks on the
  paper and are ignored. So is a single character that OCR repeats from the
  word next to it (`2 D 8` next to `8439`).
- In the 1990 typeface a time can be read with a space after its hours
  (`5 10p`), and the aircraft can run into the frequency (`AIRX6`, `74716`).
- `DISC. 12/24` and `EFF. 12/25` lines are the same as the later
  `Above Disc.` / `Above Eff.` notes. `EFF. 9/28 thru 10/1` (1987) is an
  `Eff.` and a `Disc.` date together. Other notes, such as `NO LOCAL
  TRAFFIC`, are ignored.
- OCR can repeat a flight number's last digit onto the aircraft (`3848
  3AIR`); the repeat is dropped. A Greek `ρ` in a time is read as `p`.
- Only these aircraft types are accepted: `320 727 72S 744 747 757 AIR ARJ
  CVR D10 D95 D9S DC9 KLM M80 SF3`. Anything else is taken to be misread.

OCR can still misread a character. These are listed in the report, all of
them rather than a few examples, to check against the scan (codeshare lines
are left out, since they're dropped anyway):

- `line couldn't be read`: a flight line with an unreadable time, frequency
  or stops, or an unknown aircraft type, or a note that doesn't parse. The
  line is left out of the CSV.
- `city heading couldn't be read`: until the next heading, its flights are
  filed under the previous city.
- `airport isn't one of the timetable's cities`: a destination that never
  appears as a city heading, usually a misread code. The flight is still
  written to the CSV.
- `city isn't any flight's destination`: a city heading whose code never
  appears as a destination, usually a misread code.
- `note not under a flight`: a `DISC.`/`EFF.` line with no flight above it.
- `destination out of alphabetical order`: each city's destinations are
  listed alphabetically, so a step back up the alphabet is a misread code, or
  a city whose code wasn't read (its flights were filed under the city
  before it).
- `flight to its own origin`: usually the same thing.

Once a line has been checked against the scan, add it to `OCR_CORRECTIONS`
in `timetabler.py`, keyed by PDF name, page and the line as the report shows
it, with the line as printed:

```python
(12, "LAX 950a 955a 747 3 1 D 6788"): "LAX 850a 955a 2 747 146 1 D 6788",
```

Unchanged words keep their position on the page, which matters for telling
frequency from stops. A changed word takes the position of the one it
replaces, and an added word goes between its neighbours. If the OCR is
redone and a correction no longer matches a line, it's listed under
`correction not used`.

### Schedule changes (`Above Eff.` / `Above Disc.`)

A note applies to the flight line directly above it. All lines with the
same origin, destination and flight number are treated as one flight, and
the output describes that flight as it runs after its last schedule change:

- Lines with a `Disc.` date are dropped. Lines with no note count as
  effective from the release date.
- On each day of the week, the line with the latest `Eff.` date wins.
- Days that end up with the same aircraft, times and stops are merged into
  one row. Their frequency is written in whichever form is shorter (`X6`
  rather than `123457`, `123` rather than `X4567`). When both forms are the
  same length, the `X` form is used, as the timetables do.
- Different aircraft or different times on different days stay as separate
  rows.
- If every line for a flight is discontinued, the flight is output as it ran
  in its final week, unless:
  - its last day is the release date, or
  - the same flight number starts new service afterwards (a reroute). Only
    the new routing is kept.

### Legs

A flight that stops on the way is listed once per origin along its route.
For flight 568, ABQ→MIA is listed with 1 stop and MSP→MIA nonstop.
Ordering a destination's origins from most stops to none gives the route,
ABQ → MSP → MIA:

- ABQ–MSP is leg 1 and MSP–MIA is leg 2. The multi-stop row (ABQ–MIA) is
  dropped, since its legs are already listed as nonstop rows.
- A nonstop flight that isn't part of a longer route is leg 1.
- A leg the timetable doesn't sell still counts. For example, YXE → YQR →
  MSP with no YXE–YQR line still makes YQR–MSP leg 2.
- If two origins have the same stop count to a destination (e.g. BWI and
  CMH both feeding MEM → JAN on different days), that flight's legs are
  left blank.
