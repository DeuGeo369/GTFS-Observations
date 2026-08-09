"""Aggregate raw observations into per-segment punctuality.

Joins each realtime observation to its scheduled arrival, computes deviation,
classifies it against the NSW punctuality convention, and rolls the results up
to route / direction / stop / peak-period segments.

Run `headways` afterwards to populate the excess wait time fields on the rows
this command creates.

Usage:
    python manage.py aggregate
    python manage.py aggregate --min-obs 10        # lower threshold, small window
    python manage.py aggregate --date 20260808     # single service date
"""
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.db import transaction

from reliability.models import (Observation, Route, SegmentPerformance,
                                StopTime, Trip)

SYD = ZoneInfo("Australia/Sydney")

# NSW bus punctuality convention: no more than 1 minute early, no more than
# 5 minutes 59 seconds late.
EARLY_THRESHOLD = -60
LATE_THRESHOLD = 359

# Weekday peak windows, in minutes past local midnight.
PEAK_WINDOWS = [(6 * 60, 9 * 60 + 30), (15 * 60, 18 * 60 + 30)]

# Beyond this, the record is not a genuinely late bus and is excluded.
IMPLAUSIBLE_S = 7200

DAY_S = 86400


def epoch_for(service_date, arrival_s, day_shift=0):
    """POSIX timestamp for a scheduled arrival on a given service day.

    Local midnight plus seconds. This is why arrival_s is an integer: 90840
    (25:14:00) lands at 01:14 the NEXT morning rather than 01:14 the same
    morning. day_shift moves the assumed service day by whole days.
    """
    midnight = datetime.strptime(service_date, "%Y%m%d").replace(tzinfo=SYD)
    return (midnight + timedelta(days=day_shift, seconds=arrival_s)).timestamp()


def resolve_deviation(service_date, arrival_s, actual_epoch):
    """Deviation in seconds, resolving the after-midnight service-day conflict.

    The GTFS spec defines start_date as the *service day*: a bus arriving at
    00:42 belongs to the previous day's service, with arrival_s of 24:42:00.
    The TfNSW realtime feed instead reports start_date as the calendar date the
    vehicle was operating, so for trips crossing midnight the two conventions
    disagree by exactly one day and a naive join yields a 24-hour deviation.

    Rather than assume which convention applies, both candidate service days
    are evaluated and the one giving the smaller absolute deviation is used.
    The shift is only ever considered when arrival_s >= 86400, so ordinary
    daytime trips cannot be silently moved. Shifted records are counted and
    reported.

    Returns (deviation_seconds, shifted).
    """
    deviation = actual_epoch - epoch_for(service_date, arrival_s)

    if arrival_s >= DAY_S:
        shifted = actual_epoch - epoch_for(service_date, arrival_s, day_shift=-1)
        if abs(shifted) < abs(deviation):
            return shifted, True

    return deviation, False


def is_peak(epoch):
    local = datetime.fromtimestamp(epoch, SYD)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return any(start <= minutes <= end for start, end in PEAK_WINDOWS)


def percentile(values, p):
    """Nearest-rank percentile. No numpy dependency for one number."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return float(ordered[k])


class Command(BaseCommand):
    help = "Roll observations up into SegmentPerformance"

    def add_arguments(self, parser):
        parser.add_argument("--min-obs", type=int, default=30,
                            help="minimum observations for a usable segment")
        parser.add_argument("--date", default=None,
                            help="restrict to one service date, e.g. 20260808")

    def handle(self, *args, **opts):
        qs = Observation.objects.filter(arrival_time__isnull=False)
        if opts["date"]:
            qs = qs.filter(service_date=opts["date"])

        total = qs.count()
        if not total:
            self.stderr.write("no observations to aggregate")
            return
        self.stdout.write(f"observations to process: {total:,}")

        # ------------------------------------------------------- lookups
        # Load the schedule for only the trips we actually observed. Loading all
        # 3.6M stop_times would be wasteful; querying per observation would be
        # hundreds of thousands of round trips. This is the middle path.
        trip_ids = set(qs.values_list("trip_id", flat=True).distinct())
        self.stdout.write(f"distinct trips observed: {len(trip_ids):,}")

        # Keyed on stop_sequence as well as stop_id. Only about 0.2% of
        # trip/stop pairs repeat (loop routes), but where they do the naive key
        # silently matches the wrong scheduled time.
        sched = {}
        n_sched = 0
        st_qs = (StopTime.objects
                 .filter(trip_id__in=trip_ids, arrival_s__isnull=False)
                 .values_list("trip_id", "stop_id", "stop_sequence", "arrival_s"))
        for trip_id, stop_id, seq, arrival_s in st_qs.iterator(chunk_size=50_000):
            sched[(trip_id, stop_id, seq)] = arrival_s
            sched.setdefault((trip_id, stop_id, None), arrival_s)   # fallback
            n_sched += 1
        self.stdout.write(f"scheduled stop times loaded: {n_sched:,}")

        trip_meta = dict(
            Trip.objects.filter(trip_id__in=trip_ids)
            .values_list("trip_id", "direction_id")
        )
        route_names = dict(Route.objects.values_list("route_id", "route_short_name"))

        # ------------------------------------------------------- accumulate
        buckets = defaultdict(list)

        # Lowest stop_sequence seen per segment. Sequence 1 means the segment is
        # a route origin, where an "early arrival" is usually a vehicle berthing
        # before its departure time rather than a service leaving early. That is
        # a different problem with a different remedy, so the distinction has to
        # survive into the reporting instead of being averaged away.
        min_seq = {}

        matched = unmatched = implausible = shifted_count = 0
        dates_seen = set()

        fields = ("trip_id", "service_date", "stop_id", "stop_sequence",
                  "route_id", "arrival_time", "trip_relationship",
                  "schedule_relationship")
        for row in qs.values(*fields).iterator(chunk_size=20_000):
            if row["schedule_relationship"] == "SKIPPED":
                continue
            if row["trip_relationship"] == "CANCELED":
                continue

            arrival_s = sched.get(
                (row["trip_id"], row["stop_id"], row["stop_sequence"]))
            if arrival_s is None:
                arrival_s = sched.get((row["trip_id"], row["stop_id"], None))
            if arrival_s is None:
                unmatched += 1        # added or replacement trip
                continue

            deviation, shifted = resolve_deviation(
                row["service_date"], arrival_s, row["arrival_time"])
            if shifted:
                shifted_count += 1

            if abs(deviation) > IMPLAUSIBLE_S:
                implausible += 1
                continue

            key = (row["route_id"],
                   trip_meta.get(row["trip_id"]),
                   row["stop_id"],
                   is_peak(row["arrival_time"]))

            buckets[key].append(deviation)
            seq = row["stop_sequence"]
            if seq is not None:
                current = min_seq.get(key)
                min_seq[key] = seq if current is None else min(current, seq)

            matched += 1
            dates_seen.add(row["service_date"])

        processed = matched + unmatched + implausible
        pct = lambda n: 100 * n / max(1, processed)  # noqa: E731
        self.stdout.write(
            f"\nmatched      {matched:>10,}\n"
            f"unmatched    {unmatched:>10,}  ({pct(unmatched):.1f}%) "
            f"- added/replacement trips not in the static bundle\n"
            f"implausible  {implausible:>10,}  ({pct(implausible):.1f}%) "
            f"- |deviation| > 2h, excluded\n"
            f"day-shifted  {shifted_count:>10,}  ({pct(shifted_count):.1f}%) "
            f"- after-midnight trips, service day resolved\n"
            f"segments     {len(buckets):>10,}\n"
        )

        # ------------------------------------------------------- build rows
        window_start, window_end = min(dates_seen), max(dates_seen)
        min_obs = opts["min_obs"]
        rows = []

        for key, devs in buckets.items():
            route_id, direction_id, stop_id, peak = key
            n = len(devs)
            early = sum(1 for d in devs if d < EARLY_THRESHOLD)
            late = sum(1 for d in devs if d > LATE_THRESHOLD)
            on_time = n - early - late

            lowest_seq = min_seq.get(key)

            rows.append(SegmentPerformance(
                route_id=route_id,
                route_short_name=route_names.get(route_id, ""),
                direction_id=direction_id,
                stop_id=stop_id,
                is_peak=peak,
                observations=n,
                n_on_time=on_time,
                n_early=early,
                n_late=late,
                otp_pct=100 * on_time / n,
                pct_early=100 * early / n,
                pct_late=100 * late / n,
                median_deviation_s=float(median(devs)),
                p90_deviation_s=percentile(devs, 90),
                worst_deviation_s=float(max(devs, key=abs)),
                min_stop_sequence=lowest_seq,
                is_terminus=(lowest_seq is not None and lowest_seq <= 1),
                sufficient_sample=n >= min_obs,
                window_start=window_start,
                window_end=window_end,
            ))

        with transaction.atomic():
            SegmentPerformance.objects.all().delete()
            SegmentPerformance.objects.bulk_create(rows, batch_size=5000)

        usable = [r for r in rows if r.sufficient_sample]
        self.stdout.write(self.style.SUCCESS(
            f"wrote {len(rows):,} segments, {len(usable):,} with >= {min_obs} "
            f"observations ({100 * len(usable) / max(1, len(rows)):.1f}%)"))
        self.stdout.write(f"window: {window_start} to {window_end}")

        if not usable:
            return

        obs_n = sum(r.observations for r in usable)
        self.stdout.write(
            f"\nacross usable segments ({obs_n:,} observations):\n"
            f"  on time {100 * sum(r.n_on_time for r in usable) / obs_n:5.1f}%\n"
            f"  early   {100 * sum(r.n_early for r in usable) / obs_n:5.1f}%\n"
            f"  late    {100 * sum(r.n_late for r in usable) / obs_n:5.1f}%"
        )

        # ---------------------------------------------- terminus breakdown
        # Reported separately because early running at a route origin and early
        # running mid-route are not the same finding. Conflating them would
        # overstate the number of services genuinely departing ahead of time.
        term = [r for r in usable if r.is_terminus]
        mid = [r for r in usable if not r.is_terminus]

        for label, group in (("origin stops   ", term), ("mid-route stops", mid)):
            if not group:
                continue
            n = sum(r.observations for r in group)
            self.stdout.write(
                f"  {label}: {len(group):>5,} segments, "
                f"{100 * sum(r.n_early for r in group) / n:5.1f}% early, "
                f"{100 * sum(r.n_late for r in group) / n:4.1f}% late")

        if mid:
            self.stdout.write("\nworst 10 mid-route segments (origins excluded):")
            self.stdout.write(f"  {'route':>7} {'seq':>4} {'period':>8} {'OTP':>7} "
                              f"{'early':>7} {'median':>8}  stop")
            for r in sorted(mid, key=lambda x: x.otp_pct)[:10]:
                self.stdout.write(
                    f"  {r.route_short_name:>7} {r.min_stop_sequence:>4} "
                    f"{'peak' if r.is_peak else 'off-peak':>8} "
                    f"{r.otp_pct:>6.1f}% {r.pct_early:>6.1f}% "
                    f"{r.median_deviation_s:>+7.0f}s  {r.stop_id}")