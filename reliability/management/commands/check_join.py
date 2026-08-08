"""Verify the schedule-to-actual join on completed trips before building anything on it."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand

from reliability.models import Observation, StopTime

SYD = ZoneInfo("Australia/Sydney")


def scheduled_epoch(service_date, arrival_s):
    """Reconstruct the scheduled instant as a POSIX timestamp.

    Midnight local on the service date, plus seconds. This is why arrival_s is
    an integer: a trip with arrival_s = 90840 (25:14:00) correctly lands at
    01:14 the NEXT morning rather than 01:14 the same morning.
    """
    midnight = datetime.strptime(service_date, "%Y%m%d").replace(tzinfo=SYD)
    return (midnight + timedelta(seconds=arrival_s)).timestamp()


class Command(BaseCommand):
    help = "Compare computed deviation against the feed's own delay field"

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="e.g. 20260808")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **opts):
        obs = (Observation.objects
               .filter(service_date=opts["date"], arrival_time__isnull=False)
               .order_by("trip_id", "stop_sequence")[:opts["limit"] * 50])

        matched = unmatched = 0
        print(f"{'trip':>10} {'seq':>4} {'scheduled':>9} {'actual':>9} "
              f"{'mine':>7} {'feed':>7} {'diff':>6}")

        for o in obs:
            st = (StopTime.objects
                  .filter(trip_id=o.trip_id, stop_id=o.stop_id)
                  .first())
            if not st or st.arrival_s is None:
                unmatched += 1
                continue

            sched = scheduled_epoch(o.service_date, st.arrival_s)
            deviation = o.arrival_time - sched
            feed = o.arrival_delay
            diff = deviation - feed if feed is not None else None

            if matched < opts["limit"]:
                print(f"{o.trip_id:>10} {o.stop_sequence:>4} "
                      f"{datetime.fromtimestamp(sched, SYD):%H:%M:%S} "
                      f"{datetime.fromtimestamp(o.arrival_time, SYD):%H:%M:%S} "
                      f"{deviation:>+7.0f} "
                      f"{feed if feed is not None else '-':>7} "
                      f"{diff if diff is not None else '-':>6}")
            matched += 1

        print(f"\nmatched {matched}, unmatched {unmatched} "
              f"({100 * unmatched / max(1, matched + unmatched):.1f}%)")