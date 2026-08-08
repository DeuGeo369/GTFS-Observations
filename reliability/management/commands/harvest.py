"""Poll the TfNSW GTFS-Realtime bus feed and upsert trip-stop observations.

The feed re-reports every active trip on every poll, with the prediction error
shrinking as the vehicle approaches. Rather than storing every poll (roughly 50x
the rows, and it would bias any average towards long-range predictions), each
trip-stop is upserted on its natural key so the row always holds the most recent
report. That is the closest thing the feed gives us to a realised arrival.

Usage:
    python manage.py harvest --once            # single poll, for testing
    python manage.py harvest                   # continuous, 60s interval
    python manage.py harvest --interval 30     # faster polling
"""
import os
import time
from datetime import datetime, timezone

import requests
from django.core.management.base import BaseCommand
from django.db import transaction
from google.transit import gtfs_realtime_pb2

from reliability.models import HarvestRun, Observation

URL = "https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses"

STOP_REL = {0: "SCHEDULED", 1: "SKIPPED", 2: "NO_DATA", 3: "UNSCHEDULED"}
TRIP_REL = {0: "SCHEDULED", 1: "ADDED", 2: "UNSCHEDULED", 3: "CANCELED",
            5: "REPLACEMENT"}

UPDATE_FIELDS = [
    "route_id", "vehicle_id", "arrival_time", "arrival_delay",
    "departure_time", "departure_delay", "schedule_relationship",
    "trip_relationship", "feed_ts",
]


class Command(BaseCommand):
    help = "Harvest GTFS-Realtime trip updates into the Observation table"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60,
                            help="seconds between polls (default 60)")
        parser.add_argument("--once", action="store_true",
                            help="poll once and exit")

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
        self.stdout.write(f"harvest run {run.id} started; Ctrl+C to stop")

        try:
            while True:
                started = time.time()
                try:
                    written, dupes, no_seq = self.poll(session, run)
                    msg = (f"{datetime.now():%H:%M:%S}  {written:>6,} rows  "
                           f"(poll {run.polls}, cumulative {run.observations:,})")
                    if dupes:
                        msg += f"  [{dupes} duplicate keys collapsed]"
                    if no_seq:
                        msg += f"  [{no_seq} without stop_sequence]"
                    self.stdout.write(msg)
                except Exception as exc:
                    run.errors += 1
                    run.save(update_fields=["errors"])
                    self.stderr.write(self.style.WARNING(f"poll failed: {exc}"))

                if opts["once"]:
                    break

                time.sleep(max(0.0, opts["interval"] - (time.time() - started)))
        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS(
                f"\nstopped after {run.polls} polls, "
                f"{run.observations:,} observations, {run.errors} errors"))

    # ------------------------------------------------------------------
    def poll(self, session, run):
        resp = session.get(URL, timeout=45)
        resp.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)

        age = datetime.now(timezone.utc).timestamp() - feed.header.timestamp
        if age > 300:
            self.stderr.write(self.style.WARNING(
                f"feed header is {age:.0f}s old - possible upstream outage"))

        # Deduplicate within the poll. Postgres cannot update the same row twice
        # in one ON CONFLICT statement, and the feed does contain repeated keys:
        # trips where stop_sequence is absent (protobuf then returns 0 for all
        # of them), and loop routes that call at the same stop more than once.
        # Last occurrence wins.
        seen = {}
        total_updates = 0
        no_sequence = 0

        for entity in feed.entity:
            tu = entity.trip_update
            if not tu.stop_time_update:
                continue

            trip = tu.trip
            vehicle = tu.vehicle.id if tu.HasField("vehicle") else ""
            trip_rel = TRIP_REL.get(trip.schedule_relationship, "")

            for stu in tu.stop_time_update:
                total_updates += 1
                if not stu.HasField("stop_sequence"):
                    no_sequence += 1

                arr = stu.arrival if stu.HasField("arrival") else None
                dep = stu.departure if stu.HasField("departure") else None

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
        duplicates = total_updates - len(rows)

        with transaction.atomic():
            Observation.objects.bulk_create(
                rows,
                batch_size=5000,
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

        return len(rows), duplicates, no_sequence