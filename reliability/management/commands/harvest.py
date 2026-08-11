"""Poll the TfNSW GTFS-Realtime bus feed and upsert trip-stop observations.

Each poll returns a prediction for *every* remaining stop on *every* active
trip — around 223,000 stop-time updates during weekday peak. The great majority
are predictions for stops the vehicle will not reach for half an hour or more,
and every one of them will be re-reported dozens of times before the bus
actually arrives.

Writing all of them is discarded work: it multiplied database write volume by
roughly five, pushed each poll past eight minutes, and generated dead tuples
faster than autovacuum could reclaim them. It also produced *worse* data, since
distant predictions sat in the table until a later poll happened to overwrite
them.

So only updates whose predicted arrival falls inside a window around the present
moment are persisted. Those are the reports made close enough to the event to
approximate a realised arrival. Anything further out is skipped and will be
picked up on a later poll as the vehicle approaches.

Usage:
    python manage.py harvest --once
    python manage.py harvest
    python manage.py harvest --window 900 --interval 60
    python manage.py harvest --no-filter        # old behaviour, for comparison
"""
import os
import time
from datetime import datetime, timezone

import requests
from django.core.management.base import BaseCommand
from django.db import transaction

from google.transit import gtfs_realtime_pb2

from reliability.models import HarvestRun, Observation, Poll

URL = "https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses"

STOP_REL = {0: "SCHEDULED", 1: "SKIPPED", 2: "NO_DATA", 3: "UNSCHEDULED"}
TRIP_REL = {0: "SCHEDULED", 1: "ADDED", 2: "UNSCHEDULED", 3: "CANCELED",
            5: "REPLACEMENT"}

UPDATE_FIELDS = [
    "route_id", "vehicle_id", "arrival_time", "arrival_delay",
    "departure_time", "departure_delay", "schedule_relationship",
    "trip_relationship", "feed_ts",
]

# Look further back than forward. A stop-time that has just passed carries the
# best available estimate of what actually happened; one predicted 15 minutes
# out will be revised many times before it matters.
LOOKBACK_S = 900
LOOKAHEAD_S = 300


class Command(BaseCommand):
    help = "Harvest GTFS-Realtime trip updates into the Observation table"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--lookback", type=int, default=LOOKBACK_S,
                            help="seconds behind now to accept (default 900)")
        parser.add_argument("--lookahead", type=int, default=LOOKAHEAD_S,
                            help="seconds ahead of now to accept (default 300)")
        parser.add_argument("--no-filter", action="store_true",
                            help="persist every stop-time update, as before")
        parser.add_argument("--batch", type=int, default=10000)

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        key = os.environ.get("TFNSW_API_KEY")
        if not key:
            self.stderr.write(self.style.ERROR(
                "TFNSW_API_KEY is not set. Put it in the .env file in the "
                "project root, or set it in this shell."))
            return

        session = requests.Session()
        session.headers["Authorization"] = f"apikey {key}"

        run = HarvestRun.objects.create()
        mode = "all updates" if opts["no_filter"] else (
            f"-{opts['lookback']}s / +{opts['lookahead']}s window")
        self.stdout.write(f"harvest run {run.id} started ({mode}); Ctrl+C to stop")

        try:
            while True:
                started = time.time()
                try:
                    stats = self.poll(session, run, opts)
                    elapsed = time.time() - started
                    self.stdout.write(
                        f"{datetime.now():%H:%M:%S}  "
                        f"{stats['written']:>7,} written  "
                        f"{stats['skipped']:>7,} skipped  "
                        f"{elapsed:>5.1f}s  "
                        f"(poll {run.polls}, total {run.observations:,})")
                    if elapsed > opts["interval"]:
                        self.stderr.write(self.style.WARNING(
                            f"  poll took longer than the {opts['interval']}s "
                            f"interval - effective polling rate is degraded"))
                except Exception as exc:
                    run.errors += 1
                    run.save(update_fields=["errors"])
                    Poll.objects.create(ok=False, error=str(exc)[:255])
                    self.stderr.write(self.style.WARNING(f"poll failed: {exc}"))

                if opts["once"]:
                    break
                time.sleep(max(0.0, opts["interval"] - (time.time() - started)))

        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS(
                f"\nstopped after {run.polls} polls, "
                f"{run.observations:,} observations, {run.errors} errors"))

    # ------------------------------------------------------------------
    def poll(self, session, run, opts):
        resp = session.get(URL, timeout=45)
        resp.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)

        now = datetime.now(timezone.utc).timestamp()
        age = now - feed.header.timestamp
        if age > 300:
            self.stderr.write(self.style.WARNING(
                f"feed header is {age:.0f}s old - possible upstream outage"))

        earliest = now - opts["lookback"]
        latest = now + opts["lookahead"]
        use_filter = not opts["no_filter"]

        # Deduplicate within the poll: Postgres cannot update the same row twice
        # in one ON CONFLICT statement, and the feed repeats keys for loop routes
        # that call at the same stop more than once. Last occurrence wins.
        seen = {}
        total = skipped = 0

        for entity in feed.entity:
            tu = entity.trip_update
            if not tu.stop_time_update:
                continue

            trip = tu.trip
            vehicle = tu.vehicle.id if tu.HasField("vehicle") else ""
            trip_rel = TRIP_REL.get(trip.schedule_relationship, "")

            for stu in tu.stop_time_update:
                total += 1

                arr = stu.arrival if stu.HasField("arrival") else None
                dep = stu.departure if stu.HasField("departure") else None

                when = (arr.time if arr and arr.time else
                        dep.time if dep and dep.time else None)

                if use_filter:
                    # No timestamp at all means nothing can be said about when
                    # this happened, so it cannot contribute to punctuality.
                    if when is None or not (earliest <= when <= latest):
                        skipped += 1
                        continue

                key = (trip.trip_id, trip.start_date or "", stu.stop_id,
                       stu.stop_sequence)

                seen[key] = Observation(
                    trip_id=key[0],
                    service_date=key[1],
                    stop_id=key[2],
                    stop_sequence=key[3],
                    route_id=trip.route_id or "",
                    vehicle_id=vehicle,
                    arrival_time=arr.time if arr and arr.time else None,
                    arrival_delay=(arr.delay if arr and arr.HasField("delay")
                                   else None),
                    departure_time=dep.time if dep and dep.time else None,
                    departure_delay=(dep.delay if dep and dep.HasField("delay")
                                     else None),
                    schedule_relationship=STOP_REL.get(
                        stu.schedule_relationship, ""),
                    trip_relationship=trip_rel,
                    feed_ts=feed.header.timestamp,
                )

        rows = list(seen.values())

        if rows:
            with transaction.atomic():
                Observation.objects.bulk_create(
                    rows,
                    batch_size=opts["batch"],
                    update_conflicts=True,
                    unique_fields=["trip_id", "service_date", "stop_id",
                                   "stop_sequence"],
                    update_fields=UPDATE_FIELDS,
                )

        run.polls += 1
        run.observations += len(rows)
        run.last_feed_ts = datetime.fromtimestamp(feed.header.timestamp,
                                                  timezone.utc)
        run.save(update_fields=["polls", "observations", "last_feed_ts"])

        Poll.objects.create(feed_ts=feed.header.timestamp, rows=len(rows),
                            ok=True)

        return {"written": len(rows), "skipped": skipped, "total": total,
                "duplicates": total - skipped - len(rows)}