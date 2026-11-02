# VistaRush map places

The places VistaRush's map search can show (restaurants, cafés, parks, museums and the
other kinds in `app-tags.txt`), cut from OpenStreetMap once a week, region by region, and
uploaded to VistaRush's storage, where the server answers searches from them instead of
waiting on a public Overpass server.

- `scripts/places-copy/regions.json`: the regions (one Geofabrik extract each) and the day
  each is cut (0 = Sunday, UTC).
- `scripts/places-copy/app-tags.txt`: the kinds of place, one OpenStreetMap tag per line.
- `scripts/places-copy/run.py`: a day's run. Picks the regions, sends every region's
  outline, then downloads (`fetch.sh`), cuts (`cut.py`) and uploads (`upload.py`) each one.
- `.github/workflows/places-copy.yml`: runs it every day at 09:40 UTC, or for chosen
  regions by hand (Actions, "Map places copy", Run workflow).

Data © OpenStreetMap contributors, available under the Open Database License:
https://www.openstreetmap.org/copyright. Extracts by Geofabrik: https://download.geofabrik.de.
