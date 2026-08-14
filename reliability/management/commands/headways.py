"""Headway regularity: excess wait time, bunching and gaps, per service day.

Punctuality answers "did the bus meet its timetable". It does not answer "how
long did a passenger actually wait", and on frequent services those are different
questions. A corridor can hit its on-time target while running in bunched pairs
with long gaps between them, and every metric based on scheduled adherence will
call that acceptable.

Excess wait time is the passenger-experienced measure. For passengers arriving
randomly, which they do once headways fall below about ten minutes, expected wait
is E[h^2] / 2E[h], not h/2. That formula punishes irregularity: the same mean
headway delivered unevenly costs real minutes. EWT is the observed value minus
the scheduled value, isolating the cost of the irregularity itself rather than
the cost of an infrequent timetable.

Results are written one row per segment per service day into DailyHeadway, then
rolled up onto SegmentPerformance as a mean across days. Processing a day at a
time keeps memory flat; storing days separately means weekday and weekend service
can be compared rather than blended.

    python manage.py headways                    # any day not yet computed
    python manage.py headways --date 20260813    # one specific day
    python manage.py headways --all              # recompute every day held
    python manage.py headways --rollup-only      # refresh the rollup, no reprocessing
"""
from collections import defaultdict
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Avg, Count, Sum

from reliability.models import (DailyHeadway, Observation, SegmentPerformance,
                                StopTime, Trip)
from reliability.management.commands.aggregate import (is_peak,
                                                       resolve_deviation)

# Successive vehicles closer than this fraction of the scheduled headway are
# bunched; further apart than the gap ratio is a gap.
BUNCHING_RATIO = 0.25
GAP_RATIO = 2.0

# Headways beyond this are a service break (overnight, or the end of the peak)
# rather than a delivery failure, and including them would swamp the statistic.
MAX_HEADWAY_S = 3600


def expected_wait(headways):
    """E[h^2] / 2E[h] in seconds.

    For a perfectly regular service this equals h/2. Irregularity pushes it up,
    which is the entire point: it is what the passenger actually feels.
    """
    if len(headways) < 2:
        return None
    total = sum(headways)
    if total <= 0:
        return None
    return sum(h * h for h in headways) / (2 * total)


def day_type_for(service_date):
    d = datetime.strptime(service_date, "%Y%m%d")
    return (DailyHeadway.SATURDAY if d.weekday() == 5 else
            DailyHeadway.SUNDAY if d.weekday() == 6 else
            DailyHeadway.WEEKDAY)


class Command(BaseCommand):
    help = "Compute per-day headway metrics and roll them up onto segments"

    def add_arguments(self, parser):
        parser.add_argument("--date", default=None,
                            help="one service date, e.g. 20260813")
        parser.add_argument("--all", action="store_true",
                            help="recompute every service date held")
        parser.add_argument("--rollup-only", action="store_true",
                            help="refresh the SegmentPerformance rollup only")
        parser.add_argument("--min-intervals", type=int, default=5,
                            help="minimum headway intervals for a usable figure")

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        if opts["rollup_only"]:
            self.rollup()
            return

        held = sorted(
            Observation.objects.filter(arrival_time__isnull=False)
            .values_list("service_date", flat=True).distinct()
        )
        if not held:
            self.stderr.write("no observations to process")
            return

        if opts["date"]:
            targets = [opts["date"]]
        elif opts["all"]:
            targets = held
        else:
            # Default: only days with no DailyHeadway rows yet. Makes the
            # scheduled run cheap once the backfill is done, and makes repeated
            # invocation harmless.
            done = set(DailyHeadway.objects.values_list("service_date", flat=True)
                       .distinct())
            targets = [d for d in held if d not in done]
            if not targets:
                self.stdout.write("all held service dates already computed")
                self.rollup()
                return

        self.stdout.write(f"service dates to process: {', '.join(targets)}")

        for service_date in targets:
            self.process_day(service_date, opts["min_intervals"])

        self.rollup()
        self.report(opts["min_intervals"])

    # ------------------------------------------------------------------
    def process_day(self, service_date, min_intervals):
        qs = (Observation.objects
              .filter(arrival_time__isnull=False, service_date=service_date))
        n_obs = qs.count()
        if not n_obs:
            self.stderr.write(f"{service_date}: no observations, skipped")
            return

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
        route_names = dict(Trip.objects.filter(trip_id__in=trip_ids)
                           .values_list("route__route_id", "route__route_short_name"))

        # --- collect arrivals ------------------------------------------
        # Both the actual and the scheduled instant are kept so the two headway
        # series come from exactly the same set of vehicles. Comparing an
        # observed headway against a timetable headway drawn from a different
        # set of trips would produce a difference that means nothing.
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
            key = (row["route_id"], trip_dir.get(row["trip_id"]),
                   row["stop_id"], is_peak(actual))
            arrivals[key].append((actual, actual - deviation))

        # --- headway series --------------------------------------------
        dt = day_type_for(service_date)
        rows = []

        for (route_id, direction, stop_id, peak), events in arrivals.items():
            if len(events) < 2:
                continue
            events.sort()

            actual_h, sched_h, bunched, gapped = [], [], 0, 0
            for (a1, s1), (a2, s2) in zip(events, events[1:]):
                ah, sh = a2 - a1, s2 - s1
                if ah <= 0 or sh <= 0 or ah > MAX_HEADWAY_S or sh > MAX_HEADWAY_S:
                    continue
                actual_h.append(ah)
                sched_h.append(sh)
                ratio = ah / sh
                if ratio < BUNCHING_RATIO:
                    bunched += 1
                elif ratio > GAP_RATIO:
                    gapped += 1

            n = len(actual_h)
            if n < min_intervals:
                continue

            awt, swt = expected_wait(actual_h), expected_wait(sched_h)
            if awt is None or swt is None:
                continue

            rows.append(DailyHeadway(
                route_id=route_id,
                route_short_name=route_names.get(route_id, "") or "",
                direction_id=direction,
                stop_id=stop_id,
                is_peak=peak,
                service_date=service_date,
                day_type=dt,
                intervals=n,
                mean_actual_headway_s=sum(actual_h) / n,
                mean_scheduled_headway_s=sum(sched_h) / n,
                excess_wait_time_s=awt - swt,
                bunching_rate_pct=100 * bunched / n,
                gap_rate_pct=100 * gapped / n,
            ))

        arrivals.clear()

        with transaction.atomic():
            DailyHeadway.objects.bulk_create(
                rows, batch_size=5000,
                update_conflicts=True,
                unique_fields=["route_id", "direction_id", "stop",
                               "is_peak", "service_date"],
                update_fields=["route_short_name", "day_type", "intervals",
                               "mean_actual_headway_s", "mean_scheduled_headway_s",
                               "excess_wait_time_s", "bunching_rate_pct",
                               "gap_rate_pct"],
            )

        self.stdout.write(
            f"  {service_date} ({dt}): {n_obs:,} observations -> "
            f"{len(rows):,} segments with >= {min_intervals} intervals")

    # ------------------------------------------------------------------
    def rollup(self):
        """Recompute the SegmentPerformance headway fields from DailyHeadway.

        This is a pure aggregate query with no reprocessing of observations, so
        it is cheap and can run after every `aggregate` rebuild. It is what
        keeps the map populated even though `aggregate` recreates the segment
        rows from scratch.
        """
        agg = (DailyHeadway.objects
               .values("route_id", "direction_id", "stop_id", "is_peak")
               .annotate(days=Count("service_date", distinct=True),
                         n=Sum("intervals"),
                         ewt=Avg("excess_wait_time_s"),
                         sched=Avg("mean_scheduled_headway_s"),
                         actual=Avg("mean_actual_headway_s"),
                         bunch=Avg("bunching_rate_pct"),
                         gap=Avg("gap_rate_pct")))

        lookup = {(a["route_id"], a["direction_id"], a["stop_id"], a["is_peak"]): a
                  for a in agg}
        if not lookup:
            self.stdout.write("no daily headway rows to roll up")
            return

        updates = []
        for seg in SegmentPerformance.objects.iterator(chunk_size=5000):
            a = lookup.get((seg.route_id, seg.direction_id,
                            seg.stop_id, seg.is_peak))
            if not a:
                continue
            seg.headway_days = a["days"]
            seg.headway_observations = a["n"]
            seg.excess_wait_time_s = a["ewt"]
            seg.mean_scheduled_headway_s = a["sched"]
            seg.mean_actual_headway_s = a["actual"]
            seg.bunching_rate_pct = a["bunch"]
            seg.gap_rate_pct = a["gap"]
            updates.append(seg)

            if len(updates) >= 20_000:
                self._flush(updates)
                updates = []
        if updates:
            self._flush(updates)

        self.stdout.write(self.style.SUCCESS(
            f"rollup: {len(lookup):,} segments across "
            f"{DailyHeadway.objects.values('service_date').distinct().count()} "
            f"service days"))

    @staticmethod
    def _flush(updates):
        with transaction.atomic():
            SegmentPerformance.objects.bulk_update(
                updates,
                ["headway_days", "headway_observations", "excess_wait_time_s",
                 "mean_scheduled_headway_s", "mean_actual_headway_s",
                 "bunching_rate_pct", "gap_rate_pct"],
                batch_size=2000,
            )

    # ------------------------------------------------------------------
    def report(self, min_intervals):
        usable = (SegmentPerformance.objects
                  .filter(sufficient_sample=True)
                  .exclude(excess_wait_time_s__isnull=True))
        n = usable.count()
        if not n:
            self.stdout.write("no segments with both sufficient observations "
                              "and enough headway intervals")
            return

        positive = usable.filter(excess_wait_time_s__gt=0).count()
        self.stdout.write(
            f"\nof {n:,} usable segments, {positive:,} ({100 * positive / n:.0f}%) "
            f"cost passengers more wait than the timetable implies")

        # Weekday against weekend, which is the comparison the per-day table
        # exists to make possible.
        self.stdout.write("\nexcess wait by day type:")
        for dt, label in DailyHeadway.DAY_TYPES:
            q = DailyHeadway.objects.filter(day_type=dt)
            if not q.exists():
                continue
            a = q.aggregate(ewt=Avg("excess_wait_time_s"),
                            bunch=Avg("bunching_rate_pct"),
                            n=Count("id"),
                            days=Count("service_date", distinct=True))
            self.stdout.write(
                f"  {label:<9} {a['days']} day(s), {a['n']:>7,} segment-days, "
                f"mean EWT {a['ewt'] / 60:>5.2f} min, "
                f"bunching {a['bunch']:>4.1f}%")

        self.stdout.write("\nworst 10 by mean excess wait time:")
        self.stdout.write(f"  {'route':>7} {'period':>8} {'EWT':>8} {'days':>5} "
                          f"{'bunch':>7} {'OTP':>7}  stop")
        for s in usable.select_related("stop").order_by("-excess_wait_time_s")[:10]:
            self.stdout.write(
                f"  {s.route_short_name:>7} "
                f"{'peak' if s.is_peak else 'off-peak':>8} "
                f"{s.excess_wait_time_s / 60:>6.1f}m "
                f"{s.headway_days:>5} "
                f"{s.bunching_rate_pct:>6.1f}% "
                f"{s.otp_pct:>6.1f}%  {s.stop.stop_name[:34]}")

        hidden = usable.filter(otp_pct__gte=90, excess_wait_time_s__gt=120)
        if hidden.exists():
            self.stdout.write(self.style.WARNING(
                f"\n{hidden.count()} segments meet a 90% on-time target while "
                f"adding over 2 minutes of excess wait"))
            for s in hidden.select_related("stop").order_by("-excess_wait_time_s")[:5]:
                self.stdout.write(
                    f"    route {s.route_short_name:>6}  OTP {s.otp_pct:5.1f}%  "
                    f"EWT {s.excess_wait_time_s / 60:4.1f}m  "
                    f"{s.stop.stop_name[:40]}")