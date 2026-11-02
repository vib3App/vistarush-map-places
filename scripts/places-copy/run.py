#!/usr/bin/env python3
"""A day's run of the map's place copy (.github/workflows/places-copy.yml).

  python3 -I scripts/places-copy/run.py plan [--asked "REGION ..."] [--out FILE]
  python3 -I scripts/places-copy/run.py cut WORK_DIR REGION ...

plan: the regions to cut today (scripts/places-copy/regions.json, their day
in UTC), or the ones asked for, written as regions=... to --out (the step's
GITHUB_OUTPUT). In a public repository GitHub's machines cost nothing and
regions.json's "budget" is null: every region runs on its day. With a budget
(a private repository, whose free minutes are counted), a region on a
scheduled day is left for next week when the account's free minutes would
not last the month with it. The minutes are counted from this repository's
jobs this month (GH_TOKEN, the job's own token), each rounded up to a whole
minute as GitHub bills it, a run from the last two weeks of last month run
again this month included, and the month's remaining days keep a reserve.
Other repositories' minutes are not seen by the job's token. When they
cannot be counted, only North America is cut that day. North America is
never left out, nor a region asked for by hand.

cut: every region's outline from Geofabrik, sent as the index the server
reads (upload.py --index; kept as it was if any outline cannot be fetched),
then each region in turn: downloaded (fetch.sh), cut (cut.py), uploaded and
switched to (upload.py), its files deleted before the next. A region that
fails leaves its copy in use as it was and the others go on; the run then
ends with an error, so it shows as failed.
"""
import argparse
import calendar
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'regions.json')
GEOFABRIK = 'https://download.geofabrik.de'
# What routes/proxy-cache-routes.js sends; some servers refuse a request without one.
AGENT = 'VistaRush/1.0 (+https://vistarush.com)'
NAME_RE = re.compile(r'^[a-z][a-z0-9-]{0,48}$')


def load_config():
    with open(CONFIG) as f:
        config = json.load(f)
    for name, r in config['regions'].items():
        if not NAME_RE.match(name) or name == 'index' or not re.fullmatch(r'[a-z0-9/-]{1,80}', r['path']):
            sys.exit(f'regions.json: {name} is not a region')
    return config


def github(path):
    req = urllib.request.Request(f'https://api.github.com{path}', headers={
        'Authorization': f'Bearer {os.environ["GH_TOKEN"]}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': AGENT,
    })
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def when(stamp):
    return datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)


def list_runs(repo, since):
    """Every run made since [since] (YYYY-MM-DD); None past the 1,000 GitHub
    lists for a search like this one, where the count would be short."""
    runs, page = [], 1
    while True:
        got = github(f'/repos/{repo}/actions/runs?created=%3E%3D{since}&per_page=100&page={page}')['workflow_runs']
        runs += got
        if len(got) < 100:
            return runs
        page += 1
        if len(runs) >= 1000:
            return None


def minutes_this_month(repo, now):
    """This repository's job minutes since the 1st (UTC), each job rounded up
    to a whole minute; a job still running counts until now. Runs made in the
    last two weeks of last month are looked at too, so one run again this
    month counts; only those touched this month are opened. Too many runs
    for that: this month's alone."""
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    runs = list_runs(repo, (month - datetime.timedelta(days=14)).strftime('%Y-%m-%d'))
    if runs is None:
        print('::warning::too many runs to look back into last month: this month\'s alone')
        runs = list_runs(repo, month.strftime('%Y-%m-%d'))
    if runs is None:
        raise RuntimeError('more than 1,000 runs this month to count')
    total = 0
    for run in runs:
        if run.get('updated_at') and when(run['updated_at']) < month:
            continue
        for job in github(f'/repos/{repo}/actions/runs/{run["id"]}/jobs?filter=all&per_page=100')['jobs']:
            if not job.get('started_at'):
                continue
            end = when(job['completed_at']) if job.get('completed_at') else now
            if end < month:
                continue
            seconds = (end - when(job['started_at'])).total_seconds()
            if seconds > 0:
                total += math.ceil(seconds / 60)
    return total


def plan(a):
    config = load_config()
    regions = config['regions']
    now = datetime.datetime.now(datetime.timezone.utc)
    if a.asked.strip():
        chosen = a.asked.split()
        for name in chosen:
            if name not in regions:
                sys.exit(f'{name} is not in regions.json')
        guarded = False
    else:
        today = (now.weekday() + 1) % 7  # 0 = Sunday, as cron counts
        chosen = [name for name, r in regions.items() if r['day'] == today]
        guarded = True

    budget = config['budget']
    if budget is None:
        # Free minutes (a public repository): no guard.
        if a.out:
            with open(a.out, 'a') as f:
                f.write(f'regions={" ".join(chosen)}\n')
        print(f'regions: {" ".join(chosen) or "none"}')
        return
    used = None
    if os.environ.get('GH_TOKEN') and os.environ.get('GITHUB_REPOSITORY'):
        try:
            used = minutes_this_month(os.environ['GITHUB_REPOSITORY'], now)
        except Exception as e:
            print(f'::warning::the minutes used this month could not be counted ({e}): only North America runs')
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    later = range(now.day + 1, days_in_month + 1)
    sundays = sum(1 for d in later if datetime.date(now.year, now.month, d).weekday() == 6)
    reserve = budget['reservePerDay'] * len(later) + budget['reserveForNorthAmerica'] * sundays
    if used is not None:
        print(f'{used} of {budget["monthlyMinutes"]} free minutes used this month; '
              f'{reserve} kept for the {len(later)} days left ({sundays} Sundays)')

    counted = used is not None or not (os.environ.get('GH_TOKEN') and os.environ.get('GITHUB_REPOSITORY'))
    todo = []
    for name in chosen:
        est = regions[name]['est']
        # Without a token (a run by hand on a computer) there is no guard;
        # with one whose count failed, nothing but North America.
        fits = (used is None and counted) or (used is not None and used + est + reserve <= budget['monthlyMinutes'])
        if fits or not guarded or regions[name].get('precise'):
            todo.append(name)
            if used is not None:
                used += est
            print(f'  {name}: about {est} min')
        else:
            why = f'{used} used, {reserve} kept' if used is not None else 'the minutes could not be counted'
            print(f'::warning::{name} left for next week: about {est} min, {why}')
    if a.out:
        with open(a.out, 'a') as f:
            f.write(f'regions={" ".join(todo)}\n')
    print(f'regions: {" ".join(todo) or "none"}')


def fetch(url, path, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': AGENT})
            with urllib.request.urlopen(req, timeout=60) as res, open(path, 'wb') as f:
                shutil.copyfileobj(res, f)
            return True
        except Exception as e:
            print(f'{url}: {e}', flush=True)
            time.sleep(3 * (attempt + 1))
    return False


def step(*args):
    print(f'$ {" ".join(args)}', flush=True)
    subprocess.run(args, check=True)


def cut(a):
    config = load_config()
    regions = config['regions']
    for name in a.regions:
        if name not in regions:
            sys.exit(f'{name} is not in regions.json')
    polys = os.path.join(a.work, 'polys')
    os.makedirs(polys, exist_ok=True)
    missing = [name for name, r in regions.items()
               if not fetch(f'{GEOFABRIK}/{r["path"]}.poly', os.path.join(polys, f'{name}.poly'))]
    if missing:
        print(f'::warning::no outline for {", ".join(missing)}: the index stays as it was', flush=True)
    else:
        try:
            step(sys.executable, '-I', os.path.join(HERE, 'upload.py'), '--index', CONFIG, polys)
        except subprocess.CalledProcessError:
            print('::warning::the index of outlines was not sent: it stays as it was', flush=True)

    results = []
    for name in a.regions:
        r = regions[name]
        started = time.time()
        pbf = os.path.join(a.work, 'osm', f'{name}.osm.pbf')
        work = os.path.join(a.work, f'cut-{name}')
        print(f'\n=== {name} ({r["path"]})', flush=True)
        try:
            poly = os.path.join(polys, f'{name}.poly')
            if name in missing:
                raise RuntimeError('its outline could not be fetched')
            os.makedirs(os.path.dirname(pbf), exist_ok=True)
            step('bash', os.path.join(HERE, 'fetch.sh'), f'{GEOFABRIK}/{r["path"]}-latest.osm.pbf', pbf)
            size = os.path.getsize(pbf)
            step(sys.executable, '-I', os.path.join(HERE, 'cut.py'), pbf, os.path.join(HERE, 'app-tags.txt'), work,
                 '--region', name, '--poly', poly, '--precise' if r.get('precise') else '--no-precise')
            os.remove(pbf)
            with open(os.path.join(work, 'summary.json')) as f:
                summary = json.load(f)
            step(sys.executable, '-I', os.path.join(HERE, 'upload.py'), os.path.join(work, 'out'), summary['version'],
                 '--region', name, '--concurrency', '64', '--switch', '--prune-keep', '2')
            results.append((name, 'live', summary['version'], round(size / 1e9, 2), summary['places'], summary['files'],
                            round((time.time() - started) / 60, 1)))
        except Exception as e:
            print(f'::error::{name}: {e}', flush=True)
            results.append((name, 'failed', '', '', '', '', round((time.time() - started) / 60, 1)))
        finally:
            shutil.rmtree(work, ignore_errors=True)
            for path in (pbf, f'{pbf}.md5'):  # fetch.sh leaves the checksum it read
                if os.path.exists(path):
                    os.remove(path)

    table = ['| region | result | version | GB | places | square files | minutes |', '|---|---|---|---|---|---|---|']
    table += ['| ' + ' | '.join(str(v) for v in row) + ' |' for row in results]
    print('\n' + '\n'.join(table), flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write('\n'.join(table) + '\n')
    if any(row[1] == 'failed' for row in results):
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--asked', default='')
    p.add_argument('--out')
    c = sub.add_parser('cut')
    c.add_argument('work')
    c.add_argument('regions', nargs='+')
    a = ap.parse_args()
    plan(a) if a.command == 'plan' else cut(a)


if __name__ == '__main__':
    main()
