# Bus Service Reliability Observatory — Sydney

**Live: https://bsro.sudipdeuja.com.np**

Measures where delivered bus service diverges from the published timetable across
the Sydney network, using the Transport for NSW GTFS-Realtime feed.

The realtime feed publishes predictions continuously but keeps no history — if it
isn't recorded, it's gone. This system records it, matches each observation
against the static timetable, and reports punctuality and headway regularity by
route, direction, stop and time period.

Django · GeoDjango · PostGIS · protobuf · Leaflet · AWS EC2 · GDA2020 / MGA Zone 56

> Independent analysis of TfNSW open data. **Not official TfNSW punctuality
> statistics** — different definitions, timing-point rules and source systems, not
> comparable. Contains data sourced from Transport for NSW under CC BY 4.0.

---

## What it found

*Observation window 8–14 August 2026. Collection ongoing.*

| | |
|---|---|
| Trip-stop observations collected | 8,180,903 |
| Matched to the timetable | 99.3% |
| Distinct trips | 68,502 |
| Routes | 5,944 |
| Segments meeting the 30-observation threshold | 75,971 of 280,538 |
| Observations analysed | 6,547,548 |
| On time | 74.2% |
| **Early** | **16.0%** |
| Late | 9.8% |

### The metric misses failures it should catch

**Seventy segments meet a 90% on-time target while adding more than two minutes
of excess wait time.** Route 794 at Glenmore Park reports 96.5% on-time
punctuality and 3.9 minutes of excess wait. Route 492 on Frederick Street reports
94.4% and 3.9 minutes. By any punctuality report these corridors are passing
comfortably; passengers on them wait nearly four minutes longer than the
timetable implies.

Route 394X along Anzac Parade shows the same divergence more starkly: a scheduled
11.8-minute headway delivered as 14.3 minutes actual, producing 6.4 minutes of
excess wait across five consecutive stops.

Across segments with headway data, **69% cost passengers more waiting time than
the timetable implies**, despite average headways being broadly as scheduled.

This is the case for measuring two things rather than one. Excess wait time is
`E[h²] / 2E[h]` on observed headways minus the same on scheduled headways — for
passengers arriving randomly, that is the expected wait, and it rises with
irregularity even when the mean headway is unchanged. Punctuality alone cannot
distinguish a corridor running evenly from one running in bunched pairs.

### Early running is the dominant compliance failure

Buses arrive early roughly two-thirds more often than they arrive late. Under the
NSW convention — no more than 60 seconds early, no more than 359 seconds late —
both are equally non-compliant, but early departure is worse for the passenger:
someone who arrives at the timetabled minute watches the bus leave without them.
On a corridor with 15-minute headways that is a full headway lost, not a
four-minute inconvenience.

The pattern is concentrated mid-route, not at origins:

| | Segments | Early | Late |
|---|---|---|---|
| Origin stops (sequence 1) | 2,448 | 6.2% | 6.6% |
| Mid-route stops | 68,913 | 16.6% | 9.9% |

This distinction had to be tested before the finding could stand. Early "arrival"
at a route origin usually means a vehicle berthing before its departure time — a
different problem with a different remedy. Origins turned out to be the *least*
early part of the network, the opposite of what would be expected if the finding
were an artefact, which makes the mid-route result stronger for having been
checked.

Worst-performing mid-route segments show sustained early running deep into the
route: route 746 appears at consecutive stops in the Rouse Hill corridor at 93%
early, with median deviations five to seven minutes ahead of schedule. Routes
164, 772, BN1, 861 and 889 show the same signature.

---

## Method

### Collection

`manage.py harvest` polls the GTFS-Realtime Trip Update feed every 60 seconds and
decodes the protobuf payload — around 4.3 MB and 3,000 active vehicles per poll,
rising to 223,000 stop-time updates during weekday peak. That is 1,440 calls a
day against the Open Data Hub's 60,000-per-day quota, and one request per minute
against a five-per-second throttle.

**Upsert on the natural key, not append.** Each poll re-reports every active trip
with a shrinking prediction error. Storing every poll would produce roughly fifty
times the rows and bias any average toward long-range predictions rather than
outcomes. Instead each trip-stop is upserted, so the stored row always holds the
most recent report — the closest thing the feed gives to a realised arrival. This
also makes the harvester restart-safe with no in-memory state.

**Only near-arrival updates are persisted.** Most of each poll is predictions for
stops the vehicle will not reach for half an hour, which will be revised dozens of
times before it arrives. Persisting only updates whose predicted arrival falls
within −15/+5 minutes of the present cut write volume from 223,000 to 9,000 rows
per poll and poll duration from 8–11 minutes to under 20 seconds. Less data,
better data: what remains are reports made close enough to the event to
approximate an outcome.

**Coverage is measured, not assumed.** Every poll attempt is logged to a `Poll`
table, successful or not. A gap in observation cannot be repaired after the fact,
so it has to be visible rather than inferred.

### Two data problems found and fixed

**1. GTFS times exceed 24:00:00.** A trip departing late evening has stop times
like `25:14:00`. Stored in a time column these are rejected or wrap to the wrong
day, silently deleting every late-night service. Scheduled times are stored as
integer seconds since service start.

**2. `start_date` does not mean what the GTFS spec says.** The spec defines it as
the *service day*: a bus arriving 00:42 belongs to the previous day's service. The
TfNSW feed reports it as the *calendar date of operation*. For trips crossing
midnight the two disagree by exactly one day, producing a 24-hour deviation on a
naive join — 5% of all observations.

Rather than assume a convention, both candidate service days are evaluated and
the one giving the smaller absolute deviation is used — but only where scheduled
arrival exceeds 24:00:00. That guard prevents any genuinely broken record being
"fixed" by sliding it a day. Shifted records are counted and reported. Implausible
exclusions fell from 5.1% to 0.0%.

### Validation

Independently computed deviation was cross-checked against the feed's own `delay`
attribute across 972 observations: **zero discrepancy**. That validates the
timezone handling, service-date reconstruction and seconds arithmetic together.

### Definitions

**Punctuality** — on time is no more than 60 seconds early and no more than 359
seconds late, per the NSW convention.

**Excess wait time** — `E[h²] / 2E[h]` on observed headways minus the same on
scheduled headways, isolating the cost of irregularity from the cost of an
infrequent timetable.

**Bunching and gaps** — successive vehicles closer than 25% of the scheduled
headway, or further apart than 200% of it.

**Minimum sample** — segments below 30 observations are flagged and excluded from
reporting, never shown as zero. A stop with four observations and one early bus
reads as 75% and would otherwise top any worst-performer list on noise. Of 280,538
segments, 75,971 met the threshold; the excluded count is reported, not hidden.

**Classification follows the data.** Map colour breaks are quantiles of the values
displayed, not fixed bands anchored to the 95% target. When an entire network sits
below target, a target-anchored scale renders everything one colour and carries no
information. The target is shown as a reference instead.

---

## Operations

Running the system for a week surfaced problems that do not appear in a
short-lived project.

**Table bloat.** The observation table is update-heavy by design. Measured at
**59% dead tuples after roughly 1,500 polls** — 2.9M dead against 2M live rows —
with default autovacuum settings unable to keep pace. `VACUUM FULL` reclaimed 53%
of table size (1,248 MB to 588 MB). `autovacuum_vacuum_scale_factor` is set to
0.02 on that table specifically.

**Retention.** At roughly 1.5M rows per day, unbounded collection is not viable on
a small instance. `manage.py prune` enforces a 14-day window on raw observations
while curated metrics persist. It refuses to run when aggregation is stale, so a
failed nightly job cannot cascade into permanent data loss.

**Memory ceiling, and how it is contained.** Headway aggregation across the full
window exceeds available memory on a 2 GB instance — it consumed all RAM and 1.9 GB
of swap before the OOM killer intervened, taking the instance with it on the first
occurrence. Two changes contain it: a `MemoryMax` limit so systemd kills the job
cleanly instead of destabilising the host, and a split schedule where punctuality
aggregation runs every six hours while headway analysis runs once daily against
the previous complete service day (262,588 segment-days, which fits comfortably).
Collection continues uninterrupted either way. Resolving the ceiling properly
means moving the aggregation into SQL rather than Python dictionaries.

**DNS after reboot.** A reboot brought the harvester up before `systemd-resolved`
was ready, producing 38 failed polls and roughly 2.5 hours of lost coverage on 14
August. Fixed with `After=network-online.target`. The gap is recorded in the
coverage statement rather than smoothed over — it cannot be recovered, so it is
reported.

---

## Deployment

Single EC2 t4g.small (ARM) in ap-southeast-2 running PostgreSQL 16 + PostGIS,
Gunicorn behind Caddy with automatic TLS, and four systemd units:

| Unit | Purpose |
|---|---|
| `gtfsobs-web` | Gunicorn, memory-capped, restarts on failure |
| `gtfsobs-harvester` | 60-second poller; restarts within 10s of any crash |
| `gtfsobs-analyse.timer` | Punctuality aggregation and pruning, every 6 hours |
| `gtfsobs-headways.timer` | Headway metrics for the previous service day, daily |

The harvester runs under systemd rather than cron because a missed poll cannot be
backfilled. Deployment is scripted in `deploy/bootstrap.sh`; the database is
created *owned* by the application user, since an app that cannot `VACUUM` its own
update-heavy table will degrade silently.

Approximately AUD $17/month. A single stateful collector fits one small instance; a
container-and-managed-database architecture would cost roughly five times as much
and add failure modes without benefit at this scale. That trade would change if the
system needed to survive an availability-zone failure — it currently would not.

---

## Limitations

- Figures describe the observation window only and are **not comparable to
  published TfNSW punctuality statistics**, which use different definitions,
  timing-point rules and source systems.
- The window includes 9 August 2026, when the City2Surf affected eastern suburbs
  routes and temporary stops operated. Route 379 results in that area reflect the
  event, not normal running.
- The first day is partial (174,450 observations against roughly 1.6M on full days).
- Punctuality figures cover the full window; excess wait time is computed for the
  most recent complete service day only.
- Realtime data reports what the vehicle's system believes. Doors-open time is not
  directly observable.
- 0.7% of observations are added or replacement trips absent from the static
  bundle, reported separately rather than counted as failures.
- Patronage weighting is not applied, so rankings reflect service performance
  rather than customers affected.
- Poll-level coverage logging was introduced partway through collection.
  Observations before that point are included, with coverage evidenced at run
  level only. Of 1,317 logged polls, 38 failed — all within one DNS outage after a
  reboot, since resolved.

---

## Running it

Requires PostgreSQL with PostGIS, Python 3.11+, and a free API key from the
[TfNSW Open Data Hub](https://opendata.transport.nsw.gov.au/).

```bash
python -m venv .venv && .venv\Scripts\activate
python -m pip install -r requirements.txt

psql -U postgres -c "CREATE USER gtfsobs WITH PASSWORD 'gtfsobs';"
psql -U postgres -c "CREATE DATABASE gtfsobs OWNER gtfsobs;"
psql -U postgres -d gtfsobs -c "CREATE EXTENSION postgis;"

echo TFNSW_API_KEY=your_key_here > .env
python manage.py migrate

curl.exe -o gtfs.zip -H "Authorization: apikey %TFNSW_API_KEY%" ^
  https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses
python manage.py load_gtfs gtfs.zip

python manage.py harvest              # leave running
python manage.py aggregate
python manage.py headways --date 20260814
python manage.py status
python manage.py runserver
```

| Command | Purpose |
|---|---|
| `load_gtfs` | Stream a GTFS zip into Route, Stop, Trip, StopTime |
| `harvest` | Poll the realtime feed, upsert observations, log coverage |
| `aggregate` | Join to schedule, compute deviation, roll up to segments |
| `headways` | Excess wait time, bunching and gap rates (`--date` for one day) |
| `prune` | Enforce the raw-observation retention window |
| `diagnose` | Investigate implausible deviations |
| `status` | Coverage by hour, gaps, failed polls, headline figures |

| Endpoint | Returns |
|---|---|
| `/` | Leaflet map, coloured by punctuality or excess wait |
| `/api/segments.geojson` | Segment performance in EPSG:4326 with classification breaks |
| `/api/summary.json` | Headline figures and the coverage statement |

Method note: [`docs/METHOD_NOTE.md`](docs/METHOD_NOTE.md)

---

## Data sources

Transport for NSW Open Data Hub — GTFS-Realtime Trip Updates and static GTFS
timetable, licensed under Creative Commons Attribution 4.0. Use of this data does
not imply endorsement, sponsorship or partnership by Transport for NSW.

Coordinates are transformed to GDA2020 / MGA Zone 56 (EPSG:7856) on load so all
distance calculations are in metres; reprojection to EPSG:4326 happens in PostGIS
at the API boundary. GTFS coordinates are nominally WGS84, and the sub-decimetre
datum difference is immaterial beside bus stop positional accuracy.
