#!/usr/bin/env python3
"""Cut the map's places out of a Geofabrik extract, for the place copy that
services/osm-place-copy.js answers searches from.

Keeps the kinds the app asks for (one "key=value" per line in TAGS), and of
those only named places, because the app drops places with no name. Each place is
an Overpass element in "out center" shape with only the tags the app reads.
A node keeps its point. A way's centre is the middle of the bounding box of
its nodes, as Overpass computes it, whatever shape the way has; a relation's,
of its member ways and nodes. A way or relation also carries that box
as "b" [south, west, north, east], so the server can keep a way any part
of whose box is in the circle (Overpass keeps a way any part of which is),
and it goes in every square its box touches. A relation goes in the squares
its members touch, each copy carrying the boxes of its members in that
square as "bs": the server keeps it when one of them is in the circle, as
Overpass keeps a relation one of whose members is. One gzipped JSON list per
kind per square, the same bytes when nothing in it changed.

The manifest also carries the region's name and, with --poly, its outline:
the .poly Geofabrik cut the extract by, so everything inside it is in the
copy (outer rings and "!" holes, [lng, lat]). The server tells by it which
regions' copies a search needs (services/osm-place-copy.js).

With --precise (North America's, the first copy) it carries as well:
- coverage: the boundary of the countries the copy is for (COVERAGE: the
  United States, Canada and Mexico, all three whole in Geofabrik's North
  America extract) as lines. A way two of them share is inside the copy and
  left out, so the lines are the outline of the three together. The server
  counts crossings to tell whether a search's centre is inside the copy.
- border: points at most 2 km apart along every boundary those countries
  share with a country outside them (found in the extract: every
  admin_level=2 boundary relation it holds besides theirs, e.g. Guatemala,
  Belize, Russia, Greenland), plus the coasts of Cuba, the Bahamas and
  Saint-Pierre and Miquelon (FOREIGN, fetched once from OpenStreetMap;
  those countries are not in the extract).
  Within reach of one the copy would miss places across it, so the server
  asks Overpass.

One copy per Geofabrik extract (scripts/places-copy/regions.json), each cut
once a week (.github/workflows/places-copy.yml).

  python3 -I scripts/places-copy/cut.py EXTRACT.osm.pbf scripts/places-copy/app-tags.txt WORK_DIR \
      [--region north-america] [--poly EXTRACT.poly] [--precise | --no-precise] [--deg 0.5]

--precise is the default for north-america only.

app-tags.txt lists every kind the app asks Overpass for (lib/services/
osm_places_service.dart); update it whenever the app gains a kind.

Writes WORK_DIR/out/<version>/ and WORK_DIR/summary.json. The version is the data's date as Geofabrik
names it (YYMMDD), then a random suffix, so a version is never written twice.
Needs osmium-tool.
"""
import argparse
import gzip
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections import defaultdict

OSMIUM = shutil.which('osmium') or 'osmium'
HERE = os.path.dirname(os.path.abspath(__file__))
FOREIGN = os.path.join(HERE, 'foreign-coast.json')
# The tags the app reads (lib/services/osm_places_service.dart); the key of
# each kind is kept too.
READ_TAGS = {'name', 'addr:street', 'addr:city', 'addr:state', 'phone', 'website',
             'opening_hours', 'cuisine'}
# The countries the copy is for (admin_level=2 boundary relations).
COVERAGE = {148838: 'United States', 1428125: 'Canada', 114686: 'Mexico'}
EARTH_M = 6371008.8
M_PER_DEG = math.pi / 180 * EARTH_M
SIMPLIFY_M = 500
SPACING_M = 2000
# Points the copy must call inside, and outside, or the cut is refused.
INSIDE = {'La Jolla': (32.85, -117.27), 'Miami': (25.76, -80.19), 'Kansas': (38.5, -98.0),
          'Detroit': (42.33, -83.05), 'Honolulu': (21.31, -157.86), 'Anchorage': (61.22, -149.9),
          'Key West': (24.555, -81.78), 'Seattle': (47.61, -122.33), 'Maine': (44.8, -68.8),
          'Tijuana': (32.52, -117.04), 'Windsor': (42.30, -83.02), 'Vancouver': (49.28, -123.12),
          'Toronto': (43.65, -79.38), 'Montreal': (45.50, -73.57), 'Halifax': (44.65, -63.57),
          'Whitehorse': (60.72, -135.05), 'Iqaluit': (63.75, -68.52), 'Mexico City': (19.43, -99.13),
          'Cancun': (21.16, -86.85), 'Monterrey': (25.69, -100.32)}
OUTSIDE = {'Havana': (23.13, -82.38), 'Nassau': (25.05, -77.35), 'Guatemala City': (14.63, -90.51),
           'Belize City': (17.50, -88.20), 'Nuuk': (64.18, -51.72), 'Bermuda': (32.30, -64.78),
           'Anadyr': (64.73, 177.51), 'mid-Atlantic': (35.0, -60.0), 'mid-Pacific': (30.0, -140.0)}
# Places whose side is only reported (summary.json), not required.
REPORTED = {'San Juan': (18.47, -66.11), 'Saint-Pierre': (46.78, -56.18), 'Little Diomede': (65.75, -168.95)}


def run(*args):
    subprocess.run([OSMIUM, *args], check=True)


def data_version(pbf):
    out = subprocess.run([OSMIUM, 'fileinfo', '-g', 'header.option.osmosis_replication_timestamp', pbf],
                         check=True, capture_output=True, text=True).stdout.strip()
    if len(out) < 10:
        sys.exit(f'{pbf} has no replication timestamp')
    return out, f'{out[2:4]}{out[5:7]}{out[8:10]}-{secrets.token_hex(3)}'


ESCAPE = re.compile(r'%([0-9a-fA-F]+)%')


def unescape(s):
    return ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), s)


def opl_tags(field):
    tags = {}
    if len(field) > 1:
        for pair in field[1:].split(','):
            k, _, v = pair.partition('=')
            tags[unescape(k)] = unescape(v)
    return tags


WAY_NODE = re.compile(r'n-?\d+x(-?[\d.]+)y(-?[\d.]+)')
# Nodes first, then ways, then relations, each by id, in every square's file.
TYPE_ORDER = {'node': 0, 'way': 1, 'relation': 2}


def extent(points):
    """What the centre and box of (lat, lon) points need: south, north, west,
    east, and west and east again with the western longitudes taken past 180,
    for a place across the date line."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    shifted = [lon + 360 if lon < 0 else lon for lon in lons]
    return min(lats), max(lats), min(lons), max(lons), min(shifted), max(shifted)


def centre_and_box(extents):
    """The middle of the bounding box of every point the extents were made
    from, as Overpass's "out center" computes it, and that box as [south,
    west, north, east]."""
    south = min(e[0] for e in extents)
    north = max(e[1] for e in extents)
    west = min(e[2] for e in extents)
    east = max(e[3] for e in extents)
    mid = (west + east) / 2
    if east - west > 180:
        # Across the date line: the box's middle is found with the
        # western longitudes taken past 180, then brought back.
        mid = (min(e[4] for e in extents) + max(e[5] for e in extents)) / 2
        mid = mid - 360 if mid >= 180 else mid
    centre = {'lat': round((south + north) / 2, 7), 'lon': round(mid, 7)}
    return centre, [round(south, 7), round(west, 7), round(north, 7), round(east, 7)]


def opl_fields(line):
    """An OPL line's id, tags, x, y, way nodes and relation members."""
    fields = line.rstrip('\n').split(' ')
    tags = {}
    x = y = None
    nodes = members = ''
    for field in fields[1:]:
        if field.startswith('T'):
            tags = opl_tags(field)
        elif field.startswith('x') and len(field) > 1:
            x = float(field[1:])
        elif field.startswith('y') and len(field) > 1:
            y = float(field[1:])
        elif field.startswith('N'):
            nodes = field[1:]
        elif field.startswith('M'):
            members = field[1:]
    return int(fields[0][1:]), tags, x, y, nodes, members


def dateline_span(extents):
    """For a place across the date line, the westernmost and easternmost
    longitudes of its points with the western ones taken past 180
    (extent()); None for any other."""
    if max(e[3] for e in extents) - min(e[2] for e in extents) <= 180:
        return None
    return min(e[4] for e in extents), max(e[5] for e in extents)


def places(pbf, kinds, work):
    """Every named place of a kind, as (element, its kinds, its squares' box,
    its parts' boxes or None, its span across the date line or None).

    Nodes, ways and relations. A relation is a place drawn in several
    pieces, as Disneyland, the Bellagio and many large parks are, its tags on
    the relation: before 2026-11-01 the copy, like the app's searches, left
    them out. Its centre and box come from its direct members, ways and
    nodes, whatever their own tags (a park's outline often carries
    leisure=park and no name); nested relations are not followed, as
    Overpass's around does not. Each member's box is one of the relation's
    parts: Overpass counts a relation in a circle when one of its members is,
    and a single box round a park in many pieces would take in every search
    between them.
    """
    poi = os.path.join(work, 'poi.osm.pbf')
    expr = os.path.join(work, 'kinds.txt')
    with open(expr, 'w') as f:
        f.writelines(f'nwr/{k}\n' for k in sorted(kinds))
    # With each relation come its members, and their nodes (osmium adds
    # referenced objects unless told not to).
    run('tags-filter', pbf, '-e', expr, '-o', poi, '--overwrite')
    keep = READ_TAGS | {k.split('=')[0] for k in kinds}
    unlocated = 0

    # The named relations first (osmium writes relations last), so the main
    # pass knows which ways and nodes they are drawn with.
    rel_opl = os.path.join(work, 'relations.opl')
    run('cat', poi, '-t', 'relation', '-f', 'opl', '-o', rel_opl, '--overwrite')
    relations = []
    want_ways, want_nodes = set(), set()
    with open(rel_opl, encoding='utf-8') as f:
        for line in f:
            oid, tags, _, _, _, members = opl_fields(line)
            mine = sorted(k for k in (f'{a}={b}' for a, b in tags.items()) if k in kinds)
            if not mine or not tags.get('name'):
                continue
            ways, nodes = [], []
            # Members as "w123@outer": a type, an id, then the role.
            for member in members.split(',') if members else []:
                at = member.find('@')
                if at > 1 and member[0] in 'wn':
                    (ways if member[0] == 'w' else nodes).append(int(member[1:at]))
            want_ways.update(ways)
            want_nodes.update(nodes)
            relations.append((oid, {k: v for k, v in tags.items() if k in keep}, mine, ways, nodes))
    os.remove(rel_opl)

    # Member nodes, which add-locations-to-ways leaves out when untagged.
    node_extents = {}
    if want_nodes:
        ids = os.path.join(work, 'member-nodes.txt')
        with open(ids, 'w') as f:
            f.writelines(f'n{n}\n' for n in sorted(want_nodes))
        node_opl = os.path.join(work, 'member-nodes.opl')
        # Exit 1: some are not in the extract. They are left out.
        done = subprocess.run([OSMIUM, 'getid', poi, '-i', ids, '-f', 'opl', '-o', node_opl, '--overwrite'])
        if done.returncode not in (0, 1):
            sys.exit(f'osmium getid failed ({done.returncode})')
        with open(node_opl, encoding='utf-8') as f:
            for line in f:
                oid, _, x, y, _, _ = opl_fields(line)
                if x is not None and y is not None:
                    node_extents[oid] = extent([(y, x)])
        os.remove(ids)
        os.remove(node_opl)

    opl = os.path.join(work, 'poi.opl')
    run('add-locations-to-ways', '--ignore-missing-nodes', poi, '-f', 'opl', '-o', opl, '--overwrite')
    # Everything it holds is in the OPL now; the disk is needed for the squares.
    os.remove(poi)
    way_extents = {}
    with open(opl, encoding='utf-8') as f:
        for line in f:
            kind_of = line[0]
            if kind_of not in 'nw':
                continue
            oid, tags, x, y, nodes, _ = opl_fields(line)
            mine = sorted(k for k in (f'{a}={b}' for a, b in tags.items()) if k in kinds)
            place = bool(mine and tags.get('name'))
            if kind_of == 'n':
                if not place:
                    continue
                if x is None or y is None:
                    unlocated += 1
                    continue
                lat, lon = round(y, 7), round(x, 7)
                kept = {k: v for k, v in tags.items() if k in keep}
                yield {'type': 'node', 'id': oid, 'lat': lat, 'lon': lon, 'tags': kept}, mine, (lat, lon, lat, lon), None, None
                continue
            wanted = oid in want_ways
            if not place and not wanted:
                continue
            points = [(float(py), float(px)) for px, py in WAY_NODE.findall(nodes)]
            if not points:
                if place:
                    unlocated += 1
                continue
            shape = extent(points)
            if wanted:
                way_extents[oid] = shape
            if place:
                centre, box = centre_and_box([shape])
                kept = {k: v for k, v in tags.items() if k in keep}
                yield ({'type': 'way', 'id': oid, 'center': centre, 'tags': kept, 'b': box}, mine, tuple(box), None,
                       dateline_span([shape]))
    os.remove(opl)

    for oid, kept, mine, ways, nodes in relations:
        shapes = [way_extents[w] for w in ways if w in way_extents]
        shapes += [node_extents[n] for n in nodes if n in node_extents]
        if not shapes:
            unlocated += 1
            continue
        centre, box = centre_and_box(shapes)
        parts = sorted({tuple(centre_and_box([s])[1]) for s in shapes})
        yield ({'type': 'relation', 'id': oid, 'center': centre, 'tags': kept, 'b': box}, mine, tuple(box), parts,
               dateline_span(shapes))
    if unlocated:
        print(f'{unlocated} named places had no location and were left out', file=sys.stderr)


def flat(points, lat0):
    k = M_PER_DEG * math.cos(math.radians(lat0))
    return [(lon * k, lat * M_PER_DEG) for lon, lat in points]


def simplify(points, tolerance_m):
    """Douglas-Peucker on a flat map around the line's middle latitude; ends kept."""
    if len(points) < 3:
        return points
    lat0 = sum(p[1] for p in points) / len(points)
    xy = flat(points, lat0)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        worst, at = -1.0, None
        for i in range(a + 1, b):
            px, py = xy[i]
            if length2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > worst:
                worst, at = d, i
        if at is not None and worst > tolerance_m:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(points, keep) if k]


def densify(points, spacing_m):
    out = [points[0]]
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
        k = math.cos(math.radians((lat1 + lat2) / 2))
        d = math.hypot((lon2 - lon1) * k, lat2 - lat1) * M_PER_DEG
        steps = max(1, math.ceil(d / spacing_m))
        for s in range(1, steps + 1):
            out.append((lon1 + (lon2 - lon1) * s / steps, lat1 + (lat2 - lat1) * s / steps))
    return out


def country_relations(pbf, work):
    """Every admin_level=2 relation in the extract: id -> (tags, way members)."""
    opl = os.path.join(work, 'countries.opl')
    run('tags-filter', pbf, 'r/admin_level=2', '--omit-referenced', '-f', 'opl', '-o', opl, '--overwrite')
    found = {}
    with open(opl) as f:
        for line in f:
            fields = line.strip().split(' ')
            if not fields or not fields[0].startswith('r'):
                continue
            tags = next((opl_tags(x) for x in fields if x.startswith('T')), {})
            ways = set()
            for field in fields:
                if field.startswith('M'):
                    for m in field[1:].split(','):
                        if m.startswith('w'):
                            ways.add(int(m[1:].split('@')[0]))
            found[int(fields[0][1:])] = (tags, ways)
    return found


def way_lines(pbf, work, ids, name):
    """Each way's line, by id (lon, lat points)."""
    ids_file = os.path.join(work, f'{name}-ids.txt')
    with open(ids_file, 'w') as f:
        f.writelines(f'w{w}\n' for w in sorted(ids))
    sub = os.path.join(work, f'{name}.osm.pbf')
    # getid exits 1 when some ids are not in the extract (members far outside
    # it); what it found is still written, and the callers check what matters.
    subprocess.run([OSMIUM, 'getid', pbf, '-r', '-i', ids_file, '-o', sub, '--overwrite'])
    opl = os.path.join(work, f'{name}.opl')
    run('add-locations-to-ways', '--ignore-missing-nodes', sub, '-f', 'opl', '-o', opl, '--overwrite')
    lines = {}
    with open(opl) as f:
        for line in f:
            if not line.startswith('w'):
                continue
            fields = line.split(' ')
            nodes = next((x for x in fields if x.startswith('N')), 'N')[1:]
            pts = [(float(px), float(py)) for px, py in WAY_NODE.findall(nodes)]
            if len(pts) >= 2:
                lines[int(fields[0][1:])] = pts
    return lines


def crossings_inside(segments, lat, lon):
    """Even-odd count of the boundary crossing a ray east of the point."""
    inside = False
    for (x1, y1), (x2, y2) in segments:
        if (y1 > lat) != (y2 > lat):
            x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if x > lon:
                inside = not inside
    return inside


def boundaries(pbf, work):
    relations = country_relations(pbf, work)
    for r, name in COVERAGE.items():
        if not relations.get(r, ({}, set()))[1]:
            sys.exit(f'the extract lacks {name} (r{r}): no copy')
    # A way in two of the countries (their shared border, on land or at sea)
    # is inside the copy: counted twice, it drops out of the outline.
    times = defaultdict(int)
    for r in COVERAGE:
        for w in relations[r][1]:
            times[w] += 1
    outline = {w for w, n in times.items() if n % 2 == 1}
    shared = set()
    neighbours = []
    for r, (tags, ways) in relations.items():
        if r in COVERAGE or tags.get('boundary') != 'administrative':
            continue
        common = outline & ways
        if common:
            shared |= common
            neighbours.append(tags.get('name:en') or tags.get('name') or f'r{r}')
    # A "neighbour" holding much of the outline is no neighbour (a second
    # relation for the same land): taking it would make the whole coast a
    # border and leave nearly every search to Overpass.
    largest = max((len(outline & ways) for r, (tags, ways) in relations.items()
                   if r not in COVERAGE and tags.get('boundary') == 'administrative'), default=0)
    if largest > len(outline) * 0.25:
        sys.exit(f'a relation outside the copy shares {largest} of its {len(outline)} outline ways')
    lines = way_lines(pbf, work, outline, 'boundary')
    missing_shared = shared - set(lines)
    if missing_shared:
        sys.exit(f'{len(missing_shared)} shared border ways are not in the extract, e.g. {sorted(missing_shared)[:5]}')
    # The boundary as the server will read it: simplified lines, their ends
    # kept so the rings stay closed. A line across the date line is dropped
    # (the server leaves searches near it to Overpass anyway).
    coverage = []
    for wid, pts in lines.items():
        s = [(round(x, 5), round(y, 5)) for x, y in simplify(pts, SIMPLIFY_M)]
        if any(abs(b[0] - a[0]) > 180 for a, b in zip(s, s[1:])):
            # OpenStreetMap splits lines at the date line; one that crosses it
            # would break the rings the server counts. Stop instead of
            # dropping it.
            sys.exit(f'boundary way w{wid} crosses the date line')
        coverage.append(s)
    segments = [(a, b) for line in coverage for a, b in zip(line, line[1:])]
    wrong = [n for n, (la, lo) in INSIDE.items() if not crossings_inside(segments, la, lo)]
    wrong += [n for n, (la, lo) in OUTSIDE.items() if crossings_inside(segments, la, lo)]
    if wrong:
        sys.exit(f'the boundary puts these on the wrong side: {wrong}')
    border = []
    for wid in shared:
        border += densify(simplify(lines[wid], SIMPLIFY_M), SPACING_M)
    with open(FOREIGN) as f:
        foreign = json.load(f)
    for line in foreign['lines'] + [lines[w] for w in shared]:
        if any(abs(b[0] - a[0]) > 180 for a, b in zip(line, line[1:])):
            sys.exit('a border line crosses the date line')
    for line in foreign['lines']:
        border += densify(simplify([tuple(p) for p in line], SIMPLIFY_M), SPACING_M)
    points = sorted({(round(x, 5), round(y, 5)) for x, y in border})
    return {
        'coverage': sorted(COVERAGE.values()), 'neighbours': sorted(neighbours),
        'reportedInside': {n: crossings_inside(segments, la, lo) for n, (la, lo) in REPORTED.items()},
        'sharedWays': len(shared), 'foreignLines': len(foreign['lines']), 'coverageLines': len(coverage),
        'boundaryWaysNotInExtract': len(outline - set(lines)),
        'coveragePoints': sum(len(l) for l in coverage),
    }, [[list(p) for p in line] for line in coverage], [list(p) for p in points]


def read_poly(path):
    """Geofabrik's .poly: a name line, then rings (a name line, "lon lat"
    lines, END; a ring whose name starts with "!" is a hole), then END."""
    with open(path) as f:
        lines = [line.strip() for line in f]
    outer, holes = [], []
    ring, hole, ended = None, False, False
    for line in lines[1:]:
        if not line:
            continue
        if ring is None:
            if line == 'END':
                ended = True
                break
            ring, hole = [], line.startswith('!')
            continue
        if line == 'END':
            if len(ring) < 3:
                sys.exit(f'{path}: a ring of {len(ring)} points')
            (holes if hole else outer).append(ring)
            ring = None
            continue
        x, y = (float(v) for v in line.split()[:2])
        if not (-180 <= x <= 180 and -90 <= y <= 90):
            sys.exit(f'{path}: {x} {y} is not a point')
        ring.append([round(x, 6), round(y, 6)])
    if not ended or not outer:
        sys.exit(f'{path} is not a .poly file')
    return {'outer': outer, 'holes': holes}


def squares_of(element, box, parts, deg):
    """The squares a place goes in, as {square: the parts it carries there}.

    A relation goes in the squares its parts touch, each copy carrying the
    parts in that square; a node or way (parts None) in the squares of its
    box, carrying none. A place across the date line goes in its centre's
    square only, by its box."""
    south, west, north, east = box
    if east - west > 180:
        c = element['center']
        south = north = c['lat']
        west = east = c['lon']
        parts = None
    touching = {}
    for s, w, n, e in parts or [(south, west, north, east)]:
        for la in range(math.floor(s / deg), math.floor(n / deg) + 1):
            for lo in range(math.floor(w / deg), math.floor(e / deg) + 1):
                touching.setdefault(f'{la}_{lo}', []).append([s, w, n, e])
    return {key: inside if parts else None for key, inside in touching.items()}


def write_gz(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    with open(path, 'wb') as f:
        f.write(gzip.compress(data, compresslevel=9, mtime=0))
    return len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pbf')
    ap.add_argument('tags')
    ap.add_argument('work')
    ap.add_argument('--deg', type=float, default=0.5)
    ap.add_argument('--region', default='north-america')
    ap.add_argument('--poly')
    ap.add_argument('--precise', action=argparse.BooleanOptionalAction, default=None)
    a = ap.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,48}', a.region) or a.region == 'index':
        sys.exit(f'{a.region} is not a region name')
    precise = a.precise if a.precise is not None else a.region == 'north-america'
    if not precise and not a.poly:
        sys.exit('a copy cut without --precise needs its region\'s outline (--poly)')
    poly = read_poly(a.poly) if a.poly else None
    kinds = {l.strip() for l in open(a.tags) if l.strip()}
    os.makedirs(a.work, exist_ok=True)
    osm_base, version = data_version(a.pbf)
    out = os.path.join(a.work, 'out', version)

    if precise:
        stats, coverage, border = boundaries(a.pbf, a.work)
    else:
        stats, coverage, border = {}, None, None

    squares = defaultdict(list)
    count = ways = relations = spread = crossing = 0
    # How far from 180° the places across the date line reach. Each is filed
    # in its centre's square only (squares_of), so a search within this of
    # the date line could miss one: the server leaves those to Overpass.
    dateline = 0.0
    for element, mine, box, parts, span in places(a.pbf, kinds, a.work):
        count += 1
        if span:
            crossing += 1
            dateline = max(dateline, 180 - span[0], span[1] - 180)
        if element['type'] == 'way':
            ways += 1
        elif element['type'] == 'relation':
            relations += 1
        where = squares_of(element, box, parts, a.deg)
        if len(where) > 1:
            spread += 1
        for kind in mine:
            for key, inside in where.items():
                squares[(kind, key)].append(element if inside is None else {**element, 'bs': inside})

    by_kind = {k: [] for k in kinds}
    raw = 0
    for (kind, square), elements in squares.items():
        elements.sort(key=lambda e: (TYPE_ORDER[e['type']], e['id']))
        key, value = kind.split('=')
        raw += write_gz(os.path.join(out, key, value, f'{square}.json.gz'), elements)
        by_kind[kind].append(square)
    manifest = {
        'deg': a.deg,
        'osmBase': osm_base,
        'region': a.region,
        'kinds': {k: sorted(v) for k, v in sorted(by_kind.items())},
    }
    if crossing:
        manifest['dateline'] = round(dateline + 0.01, 6)
    if precise:
        manifest['border'] = border
        manifest['coverage'] = coverage
    if poly:
        manifest['poly'] = poly
    write_gz(os.path.join(out, 'manifest.json.gz'), manifest)
    stored = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    summary = {
        'region': a.region, 'precise': precise,
        'version': version, 'osmBase': osm_base, 'places': count, 'ways': ways, 'relations': relations,
        'waysInSeveralSquares': spread, 'files': len(squares), 'acrossDateline': crossing,
        'kindsWithNoPlaces': sorted(k for k, v in by_kind.items() if not v),
        'jsonMB': round(raw / 1048576, 1), 'storedMB': round(stored / 1048576, 1),
        'borderPoints': len(border) if precise else None, **stats,
        'manifestKB': round(os.path.getsize(os.path.join(out, 'manifest.json.gz')) / 1024),
    }
    with open(os.path.join(a.work, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    main()
