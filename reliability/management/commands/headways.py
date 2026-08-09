"""Headway regularity: excess wait time, bunching and gaps.

Punctuality answers "did the bus meet its timetable". It does not answer "how
long did a passenger actually wait", and on frequent services those are
different questions. A corridor can hit its on-time target while running in
bunched pairs with long gaps between them, and every metric based on scheduled
adherence will call that acceptable.

Excess wait time is the passenger-experienced measure. For passengers arriving
randomly (which they do once headways drop below about 10 minutes), expected
wait is E[h^2] / 2E[h], not h/2. That formula punishes irregularity: the same
mean headway delivered unevenly costs real minutes. EWT is the observed value
minus the scheduled value, so it isolates the cost of the irregularity itself
rather than the cost of an infrequent timetable.

Run after `aggregate`, which creates the rows this command updates.

Usage:
    python manage.py headways
    python manage.py headways --min-intervals 5
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from reliability.models import Observation, SegmentPerformance, StopTime, Trip
from reliability.management.commands.aggregate import (is_peak,
                                                       resolve_deviation)

# Successive vehicles closer than this fraction of the scheduled headway are
# bunched; further apart than the gap ratio is a gap.
BUNCHING_RATIO = 0.25
GAP_RATIO = 2.0

# Headways beyond this are almost certainly a service break (overnight, or the
# end of the peak) rather than a delivery failure, and including them would
# swamp the statistic.
MAX_HEADWAY_S = 3600


def expected_wait(headways):
    """E[h^2] / 2E[h] in seconds.

    For a perfectly regular service this equals h/2. Irregularity pushes it
    up, which is the entire point: it is what the passenger actually feels.
    """
    if len(headways) < 2:
        return None
    total = sum(headways)
    if total <= 0:
        return None
    return sum(h * h for h in headways) / (2 * total)


class Command(BaseCommand):
    help = "Compute excess wait time, bunching and gap rates per segment"

    def add_arguments(self, parser):
        parser.add_argument("--min-intervals", type=int, default=5,
                            help="minimum headway intervals for a usable figure")

    def handle(self, *args, **opts):
        segments = SegmentPerformance.objects.all()
        if not segments.exists():
            self.stderr.write("no segments - run: python manage.py aggregate")
            return

        # ---------------------------------------------------------- lookups
        qs = Observation.objects.filter(arrival_time__isnull=False)
        trip_ids = set(qs.values_list("trip_id", flat=True).distinct())

        sched = {}
        for trip_id, stop_id, seq, arrival_s in (
                StopTime.objects
                .filter(trip_id__in=trip_ids, arrival_s__isnull=False)
                .values_list("trip_id", "stop_id", "stop_sequence", "arrival_s")
                .iterator(chunk_size=50_000)):
            sched[(trip_id, stop_id, seq)] = arrival_s
            sched.setdefault((trip_id, stop_id, None), arrival_s)

        trip_dir = dict(Trip.objects.filter(trip_id__in=trip_ids)
                        .values_list("trip_id", "direction_id"))

        self.stdout.write(f"schedule loaded for {len(trip_ids):,} trips")

        # ------------------------------------------------- collect arrivals
        # Group every observed arrival by segment and service date, keeping both
        # the actual and the scheduled instant so the two headway series are
        # built from exactly the same set of vehicles. Comparing an observed
        # headway against a timetable headway drawn from a different set of
        # trips would produce a difference that means nothing.
        arrivals = defaultdict(list)

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
                continue

            deviation, _ = resolve_deviation(
                row["service_date"], arrival_s, row["arrival_time"])
            if abs(deviation) > 7200:
                continue

            actual = row["arrival_time"]
            scheduled = actual - deviation

            key = (row["route_id"], trip_dir.get(row["trip_id"]),
                   row["stop_id"], is_peak(actual), row["service_date"])
            arrivals[key].append((actual, scheduled))

        self.stdout.write(f"arrival series built for {len(arrivals):,} "
                          f"segment-days")

        # ------------------------------------------------- headway series
        # Accumulate across service days per segment. Headways are computed
        # within a single day only - the interval from the last bus on Monday
        # to the first on Tuesday is not a headway.
        series = defaultdict(lambda: {"actual": [], "scheduled": [],
                                      "bunched": 0, "gapped": 0, "n": 0})

        for (route_id, direction, stop_id, peak, _date), events in arrivals.items():
            if len(events) < 2:
                continue
            events.sort()
            seg = series[(route_id, direction, stop_id, peak)]

            for (a1, s1), (a2, s2) in zip(events, events[1:]):
                actual_h = a2 - a1
                sched_h = s2 - s1
                if actual_h <= 0 or sched_h <= 0:
                    continue
                if actual_h > MAX_HEADWAY_S or sched_h > MAX_HEADWAY_S:
                    continue

                seg["actual"].append(actual_h)
                seg["scheduled"].append(sched_h)
                seg["n"] += 1

                ratio = actual_h / sched_h
                if ratio < BUNCHING_RATIO:
                    seg["bunched"] += 1
                elif ratio > GAP_RATIO:
                    seg["gapped"] += 1

        # ------------------------------------------------- write back
        min_intervals = opts["min_intervals"]
        updates, skipped = [], 0

        for segment in segments.iterator(chunk_size=5000):
            key = (segment.route_id, segment.direction_id,
                   segment.stop_id, segment.is_peak)
            seg = series.get(key)

            if not seg or seg["n"] < min_intervals:
                skipped += 1
                continue

            awt = expected_wait(seg["actual"])
            swt = expected_wait(seg["scheduled"])
            if awt is None or swt is None:
                skipped += 1
                continue

            segment.headway_observations = seg["n"]
            segment.mean_actual_headway_s = sum(seg["actual"]) / seg["n"]
            segment.mean_scheduled_headway_s = sum(seg["scheduled"]) / seg["n"]
            segment.excess_wait_time_s = awt - swt
            segment.bunching_rate_pct = 100 * seg["bunched"] / seg["n"]
            segment.gap_rate_pct = 100 * seg["gapped"] / seg["n"]
            updates.append(segment)

        with transaction.atomic():
            SegmentPerformance.objects.bulk_update(
                updates,
                ["headway_observations", "mean_actual_headway_s",
                 "mean_scheduled_headway_s", "excess_wait_time_s",
                 "bunching_rate_pct", "gap_rate_pct"],
                batch_size=2000,
            )

        self.stdout.write(self.style.SUCCESS(
            f"updated {len(updates):,} segments "
            f"({skipped:,} had fewer than {min_intervals} headway intervals)"))

        # ------------------------------------------------- report
        usable = [s for s in updates if s.sufficient_sample]
        if not usable:
            self.stdout.write("no segments with both sufficient observations "
                              "and enough headway intervals yet")
            return

        positive = [s for s in usable if s.excess_wait_time_s > 0]
        self.stdout.write(
            f"\nof {len(usable):,} usable segments, {len(positive):,} "
            f"({100 * len(positive) / len(usable):.0f}%) cost passengers "
            f"more wait than the timetable implies")

        self.stdout.write("\nworst 10 by excess wait time:")
        self.stdout.write(f"  {'route':>7} {'period':>8} {'EWT':>8} {'sched hw':>9} "
                          f"{'actual hw':>10} {'bunch':>7} {'OTP':>7}  stop")
        for s in sorted(usable, key=lambda x: -x.excess_wait_time_s)[:10]:
            self.stdout.write(
                f"  {s.route_short_name:>7} "
                f"{'peak' if s.is_peak else 'off-peak':>8} "
                f"{s.excess_wait_time_s / 60:>6.1f}m "
                f"{s.mean_scheduled_headway_s / 60:>8.1f}m "
                f"{s.mean_actual_headway_s / 60:>9.1f}m "
                f"{s.bunching_rate_pct:>6.1f}% "
                f"{s.otp_pct:>6.1f}%  {s.stop.stop_name[:36]}")

        # The headline comparison: segments that pass on punctuality but fail
        # the passenger. This is the pair of numbers that justifies the metric.
        hidden = [s for s in usable
                  if s.otp_pct >= 90 and s.excess_wait_time_s > 120]
        if hidden:
            self.stdout.write(self.style.WARNING(
                f"\n{len(hidden)} segments meet a 90% on-time target while "
                f"adding over 2 minutes of excess wait - reliability problems "
                f"that punctuality reporting does not surface"))
            for s in sorted(hidden, key=lambda x: -x.excess_wait_time_s)[:5]:
                self.stdout.write(
                    f"    route {s.route_short_name:>6}  OTP {s.otp_pct:5.1f}%  "
                    f"EWT {s.excess_wait_time_s / 60:4.1f}m  "
                    f"{s.stop.stop_name[:40]}")