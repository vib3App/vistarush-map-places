#!/usr/bin/env python3
"""Upload a place copy cut by cut.py to the Bunny storage zone, check it is
all there, and only then (with --switch) make it the one the server uses.

  python3 -I scripts/places-copy/upload.py WORK_DIR/out VERSION [--region north-america] [--env .env] \
      [--switch] [--prune-keep 2] [--resume]
  python3 -I scripts/places-copy/upload.py --index scripts/places-copy/regions.json POLY_DIR [--env .env]

Each region's copy has its own folder, places/<region>/<version>/; North
America's, the first, stays where it was, places/<version>/. places/
current.json names the copy in use of each region ("regions"), North
America's alone as well ("version", which servers from before 2026-11-02
read), and the index of every region's outline ("index"). A switch changes
its own region's entry and nothing else.

--index writes that index, places/index/<sha>.json.gz, from each region's
.poly in POLY_DIR (named <region>.poly; every region in regions.json must
have one), and names it in current.json; the index named before it is kept,
any other deleted. The server needs every region's outline, cut or not yet,
to know where the copies it holds end (services/osm-place-copy.js).

The zone's name, password and region come from the env file when one is given
(BUNNY_STORAGE_*, the same ones the server uses), otherwise from the
environment (the nightly GitHub job). Each file is sent with its SHA-256, so
Bunny refuses a damaged upload. The manifest goes after every square and
places/current.json goes last, so the server never sees a copy that is still
arriving.

A version is written once. Uploading into a version folder that already holds
files is refused, unless --resume is given and current.json does not name
that version yet; then files already there at the right size are skipped.
--prune-keep N, after a switch, deletes the region's version folders other
than the one now in use and the one in use before it (a server can still be
reading it), and with N over 2 the N - 2 made last besides. A folder an upload
left half-written goes with the rest. Nothing outside places/ is touched.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = 'places'
VERSION_RE = re.compile(r'^\d{6}-[0-9a-f]{6}$')
REGION_RE = re.compile(r'^[a-z][a-z0-9-]{0,48}$')
# The region of the first copy, whose folders sit in places/ itself.
FIRST = 'north-america'


def env_file(path):
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


class Zone:
    def __init__(self, env):
        self.zone = env['BUNNY_STORAGE_ZONE']
        self.password = env['BUNNY_STORAGE_PASSWORD']
        region = env.get('BUNNY_STORAGE_REGION', 'de')
        self.host = 'storage.bunnycdn.com' if region in ('', 'de') else f'{region}.storage.bunnycdn.com'

    def url(self, path):
        return f'https://{self.host}/{self.zone}/{path}'

    def request(self, path, method='GET', data=None, headers=None, tries=4):
        error = None
        for attempt in range(tries):
            req = urllib.request.Request(self.url(path), data=data, method=method,
                                         headers={'AccessKey': self.password, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    return res.status, res.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 404, b''
                error = f'HTTP {e.code}'
                # A 400 has come back for one file in 25,000 and gone on a
                # retry; only a refused password is final.
                if e.code in (401, 403):
                    break
            except Exception as e:  # network
                error = str(e)
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f'{method} {path}: {error}')

    def put(self, path, data):
        status, _ = self.request(path, 'PUT', data, {
            'Content-Type': 'application/octet-stream',
            'Checksum': hashlib.sha256(data).hexdigest().upper()})
        if status not in (200, 201):
            raise RuntimeError(f'PUT {path}: HTTP {status}')

    def listing(self, path):
        status, body = self.request(path.rstrip('/') + '/')
        return [] if status == 404 else json.loads(body)

    def pointer(self):
        """places/current.json, its regions filled in from a pointer that
        names only North America's version."""
        status, body = self.request(f'{ROOT}/current.json')
        pointer = {} if status == 404 else json.loads(body)
        if not isinstance(pointer.get('regions'), dict):
            pointer['regions'] = {FIRST: pointer['version']} if isinstance(pointer.get('version'), str) else {}
        return pointer

    def set_pointer(self, pointer):
        self.put(f'{ROOT}/current.json', json.dumps(pointer, sort_keys=True).encode())


def folder_of(region, version):
    return version if region == FIRST else f'{region}/{version}'


def read_poly(path):
    """Geofabrik's .poly, as cut.py reads it."""
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


def upload_index(zone, config_path, poly_dir):
    """Every region's outline in one file, named by its contents, then named
    in current.json; the index in use before it is kept (a server can still
    be reading it), any other deleted, one an earlier run sent but never
    named included."""
    with open(config_path) as f:
        regions = json.load(f)['regions']
    polys = {}
    for name in sorted(regions):
        if not REGION_RE.match(name):
            sys.exit(f'{name} is not a region name')
        polys[name] = read_poly(os.path.join(poly_dir, f'{name}.poly'))
    body = json.dumps({'regions': polys}, sort_keys=True, separators=(',', ':')).encode()
    name = f'index/{hashlib.sha256(body).hexdigest()[:16]}.json.gz'
    if zone.pointer().get('index') == name:
        print(f'the index of {len(polys)} outlines is unchanged: {name}', flush=True)
        return
    zone.put(f'{ROOT}/{name}', gzip.compress(body, compresslevel=9, mtime=0))
    # Read just before it is written, as a switch does.
    pointer = zone.pointer()
    before = pointer.get('index') or ''
    pointer['index'] = name
    zone.set_pointer(pointer)
    print(f'current.json now names {name}: {len(polys)} outlines', flush=True)
    kept = {name.split('/', 1)[1], before.split('/', 1)[-1]}
    for e in zone.listing(f'{ROOT}/index'):
        if not e.get('IsDirectory') and e['ObjectName'] not in kept:
            status, _ = zone.request(f'{ROOT}/index/{e["ObjectName"]}', 'DELETE')
            print(f'deleted old index {e["ObjectName"]}: HTTP {status}', flush=True)


def versionof(folder):
    return folder.rsplit('/', 1)[-1]


def stale_versions(listing, kept, newest=0):
    """The version folders of a listing to delete: all but those in [kept]
    and the [newest] made last. By the date each was made, not by name:
    copies cut from the same day's data are named in random order
    (2026-11-01: three named 261031-, and the newest by name was the
    oldest). Only folders named as versions are listed: a region's folder or
    the index never is."""
    folders = sorted((e for e in listing if e.get('IsDirectory') and VERSION_RE.match(e['ObjectName'])),
                     key=lambda e: (e.get('DateCreated') or '', e['ObjectName']))
    names = [e['ObjectName'] for e in folders]
    others = [n for n in names if n not in kept]
    keep = set(kept) | (set(others[-newest:]) if newest > 0 else set())
    return [n for n in names if n not in keep]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out', help='WORK_DIR/out, or with --index the regions.json')
    ap.add_argument('version', help='the version, or with --index the folder of .poly files')
    ap.add_argument('--region', default=FIRST)
    ap.add_argument('--index', action='store_true')
    ap.add_argument('--env')
    ap.add_argument('--concurrency', type=int, default=32)
    ap.add_argument('--switch', action='store_true')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--prune-keep', type=int, default=0)
    a = ap.parse_args()
    zone = Zone(env_file(a.env) if a.env else os.environ)
    if a.index:
        upload_index(zone, a.out, a.version)
        return
    if not VERSION_RE.match(a.version):
        sys.exit(f'{a.version} is not a version name')
    if not REGION_RE.match(a.region) or a.region == 'index':
        sys.exit(f'{a.region} is not a region name')
    folder_path = f'{ROOT}/{folder_of(a.region, a.version)}'
    base = os.path.join(a.out, a.version)
    files = []
    for d, _, names in os.walk(base):
        for n in names:
            rel = os.path.relpath(os.path.join(d, n), base)
            if rel != 'manifest.json.gz':
                files.append(rel)
    files.sort()
    by_folder = {}
    for rel in files:
        folder, name = os.path.split(rel)
        by_folder.setdefault(folder, {})[name] = os.path.getsize(os.path.join(base, rel))

    # A version is written once (see the module note).
    existing = zone.listing(folder_path)
    skip = set()
    if existing:
        if not a.resume:
            sys.exit(f'{folder_path}/ already holds files; cut a new version or pass --resume')
        if zone.pointer()['regions'].get(a.region) == folder_of(a.region, a.version):
            sys.exit(f'{a.version} is live; it cannot be written to')
        for folder, want in by_folder.items():
            have = {e['ObjectName']: e['Length'] for e in zone.listing(f'{folder_path}/{folder}')
                    if not e.get('IsDirectory')}
            skip |= {f'{folder}/{n}' for n, size in want.items() if have.get(n) == size}
    todo = [rel for rel in files if rel not in skip]
    print(f'{len(todo)} of {len(files)} square files to {zone.zone}/{folder_path}/', flush=True)

    started = time.time()
    done = 0

    failed = []

    def send(rel):
        try:
            with open(os.path.join(base, rel), 'rb') as f:
                zone.put(f'{folder_path}/{rel}', f.read())
        except RuntimeError as e:
            failed.append((rel, str(e)))

    with ThreadPoolExecutor(a.concurrency) as pool:
        for _ in pool.map(send, todo):
            done += 1
            if done % 5000 == 0 or done == len(todo):
                print(f'  {done}/{len(todo)} ({done / (time.time() - started):.0f}/s)', flush=True)
    # One more pass, one at a time, for any that failed with the crowd.
    again, failed[:] = list(failed), []
    for rel, _ in again:
        send(rel)
    if failed:
        sys.exit(f'{len(failed)} files would not upload, e.g. {failed[:3]}')

    # Every kind's folder holds what was cut, at the sizes cut.
    for folder, want in sorted(by_folder.items()):
        have = {e['ObjectName']: e['Length'] for e in zone.listing(f'{folder_path}/{folder}')
                if not e.get('IsDirectory')}
        wrong = [n for n, size in want.items() if have.get(n) != size]
        if wrong:
            sys.exit(f'{folder}: {len(wrong)} files missing or the wrong size, e.g. {wrong[:3]}')
    print(f'checked: all {len(files)} squares are in the zone at their sizes', flush=True)

    with open(os.path.join(base, 'manifest.json.gz'), 'rb') as f:
        zone.put(f'{folder_path}/manifest.json.gz', f.read())
    print('manifest uploaded', flush=True)
    if not a.switch:
        return
    # Read just before it is written: every other region's entry, and the
    # index, stay as they are (the job's runs never overlap: one
    # concurrency group).
    pointer = zone.pointer()
    before = versionof(pointer['regions'].get(a.region, ''))
    pointer['regions'][a.region] = folder_of(a.region, a.version)
    if a.region == FIRST:
        pointer['version'] = a.version
    zone.set_pointer(pointer)
    print(f'current.json now names {folder_of(a.region, a.version)} for {a.region}', flush=True)

    if a.prune_keep > 0:
        # North America's versions are the version-named folders of places/
        # itself (a region's folder or the index never looks like one).
        parent = ROOT if a.region == FIRST else f'{ROOT}/{a.region}'
        live = versionof(zone.pointer()['regions'].get(a.region, ''))
        # Not the N made last: a run that failed half-way through its upload
        # would count, and the copy in use before this one would go.
        for old in stale_versions(zone.listing(parent), {live, before}, a.prune_keep - 2):
            status, _ = zone.request(f'{parent}/{old}/', 'DELETE')
            print(f'deleted old version {parent}/{old}: HTTP {status}', flush=True)


if __name__ == '__main__':
    main()
