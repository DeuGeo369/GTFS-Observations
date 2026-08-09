"""Investigate observations that produce implausible deviations.

5% exclusion is too large to wave through. This works out whether the cause is
the (trip_id, stop_id) join key colliding on loop routes, a service-date
problem, or something else entirely.

Usage:
    python manage.py diagnose
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand

from reliability.models import Observation, StopTime

SYD = ZoneInfo("Australia/Sydney")
IMPLAUSIBLE_S = 7200


def scheduled_epoch(service_date, arrival_s):
    midnight = datetime.strptime(service_date, "%Y%m%d").replace(tzinfo=SYD)
    return (midnight + timedelta(seconds=arrival_s)).timestamp()


class Command(BaseCommand):
    help = "Explain implausible deviations"

    def handle(self, *args, **opts):
        qs = Observation.objects.filter(arrival_time__isnull=False)
        trip_ids = set(qs.values_list("trip_id", flat=True).distinct())

        # --- 1. how often does one trip call at the same stop twice? --------
        seq_by_pair = defaultdict(list)
        for trip_id, stop_id, seq, arrival_s in (
                StopTime.objects
                .filter(trip_id__in=trip_ids, arrival_s__isnull=False)
                .values_list("trip_id", "stop_id", "stop_sequence", "arrival_s")
                .iterator(chunk_size=50_000)):
            seq_by_pair[(trip_id, stop_id)].append((seq, arrival_s))

        repeats = {k: v for k, v in seq_by_pair.items() if len(v) > 1}
        self.stdout.write(
            f"trip/stop pairs in schedule : {len(seq_by_pair):,}\n"
            f"  visited more than once    : {len(repeats):,} "
            f"({100 * len(repeats) / max(1, len(seq_by_pair)):.2f}%)")

        if repeats:
            worst = max(repeats.items(),
                        key=lambda kv: max(a for _, a in kv[1]) - min(a for _, a in kv[1]))
            spread = max(a for _, a in worst[1]) - min(a for _, a in worst[1])
            self.stdout.write(
                f"  largest schedule spread   : {spread:,}s on trip {worst[0][0]} "
                f"stop {worst[0][1]} (seqs {[s for s, _ in worst[1]]})")

        # --- 2. classify the implausible rows -------------------------------
        # Build both lookups: the naive one and the sequence-aware one.
        naive = {k: v[-1][1] for k, v in seq_by_pair.items()}
        exact = {(t, s, seq): a for (t, s), v in seq_by_pair.items()
                 for seq, a in v}

        reasons = Counter()
        samples = []
        checked = 0

        for row in qs.values("trip_id", "service_date", "stop_id",
                             "stop_sequence", "arrival_time",
                             "arrival_delay").iterator(chunk_size=20_000):
            a_naive = naive.get((row["trip_id"], row["stop_id"]))
            if a_naive is None:
                continue
            checked += 1

            dev_naive = row["arrival_time"] - scheduled_epoch(
                row["service_date"], a_naive)
            if abs(dev_naive) <= IMPLAUSIBLE_S:
                continue

            a_exact = exact.get(
                (row["trip_id"], row["stop_id"], row["stop_sequence"]))

            if a_exact is None:
                reasons["stop_sequence not in schedule"] += 1
            else:
                dev_exact = row["arrival_time"] - scheduled_epoch(
                    row["service_date"], a_exact)
                if abs(dev_exact) <= IMPLAUSIBLE_S:
                    reasons["FIXED by using stop_sequence"] += 1
                else:
                    reasons["still implausible with correct sequence"] += 1
                    if len(samples) < 10:
                        samples.append((row, a_exact, dev_exact))

        self.stdout.write(f"\nchecked {checked:,} matched observations")
        self.stdout.write("implausible causes:")
        for reason, n in reasons.most_common():
            self.stdout.write(f"  {reason:<44} {n:>8,}")

        if samples:
            self.stdout.write("\nsamples still implausible after the fix:")
            self.stdout.write(
                f"{'trip':>12} {'date':>9} {'seq':>4} {'sched':>17} "
                f"{'actual':>17} {'dev_h':>7} {'feed':>8}")
            for row, a_exact, dev in samples:
                sched_dt = datetime.fromtimestamp(
                    scheduled_epoch(row["service_date"], a_exact), SYD)
                act_dt = datetime.fromtimestamp(row["arrival_time"], SYD)
                self.stdout.write(
                    f"{row['trip_id']:>12} {row['service_date']:>9} "
                    f"{row['stop_sequence']:>4} {sched_dt:%Y-%m-%d %H:%M} "
                    f"{act_dt:%Y-%m-%d %H:%M} {dev / 3600:>+7.1f} "
                    f"{row['arrival_delay'] if row['arrival_delay'] is not None else '-':>8}")