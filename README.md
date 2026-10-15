# nwa_timetabler

Converts Northwest Airlines system timetable PDFs into CSV files of flight segments. Currently only works/tested with with digital nwa.com timetables
(published 2000 and later). Future support may come for older, digitally scanned timetables.

## Running

Requires Python 3 (tested with 3.13) and `pdfplumber`.

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
