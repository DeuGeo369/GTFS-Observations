"""Load a TfNSW static GTFS bundle into the database.

Streams straight from the zip. stop_times.txt is ~260 MB and several million
rows, so nothing here reads a whole file into memory.
"""
import csv
import io
import zipfile

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from django.db import transaction

from reliability.models import Route, Stop, StopTime, Trip

BATCH = 10_000


def gtfs_seconds(value):
    """'25:14:00' -> 90840 seconds since service start.

    GTFS deliberately allows hours >= 24 for trips running past midnight.
    Returning an integer keeps those trips instead of wrapping them to the
    wrong day.
    """
    if not value:
        return None
    try:
        h, m, s = value.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def rows(zf, name):
    """Yield dict rows from a member file without loading it all."""
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


class Command(BaseCommand):
    help = "Load a GTFS zip into Route, Stop, Trip and StopTime"

    def add_arguments(self, parser):
        parser.add_argument("zip_path")
        parser.add_argument("--flush", action="store_true",
                            help="delete existing GTFS rows first")

    def handle(self, *args, **opts):
        zf = zipfile.ZipFile(opts["zip_path"])

        if opts["flush"]:
            self.stdout.write("deleting existing rows...")
            StopTime.objects.all().delete()
            Trip.objects.all().delete()
            Stop.objects.all().delete()
            Route.objects.all().delete()

        self.load_routes(zf)
        self.load_stops(zf)
        self.load_trips(zf)
        self.load_stop_times(zf)
        self.stdout.write(self.style.SUCCESS("done"))

    # ------------------------------------------------------------------
    def load_routes(self, zf):
        objs = [
            Route(
                route_id=r["route_id"],
                route_short_name=r.get("route_short_name", "") or "",
                route_long_name=r.get("route_long_name", "") or "",
                route_type=int(r["route_type"]) if r.get("route_type") else None,
            )
            for r in rows(zf, "routes.txt")
        ]
        Route.objects.bulk_create(objs, batch_size=BATCH, ignore_conflicts=True)
        self.stdout.write(f"routes      {len(objs):>10,}")

    def load_stops(self, zf):
        objs = []
        skipped = 0
        for r in rows(zf, "stops.txt"):
            lat, lon = r.get("stop_lat"), r.get("stop_lon")
            if not lat or not lon:
                skipped += 1          # parent stations often have no coords
                continue
            # GTFS coordinates are WGS84; transformed to GDA2020 / MGA56 so
            # every later distance is in metres.
            p = Point(float(lon), float(lat), srid=4326)
            p.transform(7856)
            objs.append(Stop(
                stop_id=r["stop_id"],
                stop_name=(r.get("stop_name") or "")[:255],
                geom=p,
            ))
        Stop.objects.bulk_create(objs, batch_size=BATCH, ignore_conflicts=True)
        self.stdout.write(f"stops       {len(objs):>10,}  (skipped {skipped} without coords)")

    def load_trips(self, zf):
        known_routes = set(Route.objects.values_list("route_id", flat=True))
        objs, orphans = [], 0
        for r in rows(zf, "trips.txt"):
            if r["route_id"] not in known_routes:
                orphans += 1
                continue
            objs.append(Trip(
                trip_id=r["trip_id"],
                route_id=r["route_id"],
                service_id=r.get("service_id", "") or "",
                direction_id=int(r["direction_id"]) if r.get("direction_id") else None,
                shape_id=r.get("shape_id", "") or "",
            ))
        Trip.objects.bulk_create(objs, batch_size=BATCH, ignore_conflicts=True)
        self.stdout.write(f"trips       {len(objs):>10,}  (orphaned {orphans})")

    def load_stop_times(self, zf):
        known_trips = set(Trip.objects.values_list("trip_id", flat=True))
        known_stops = set(Stop.objects.values_list("stop_id", flat=True))

        batch, total, orphans = [], 0, 0
        for r in rows(zf, "stop_times.txt"):
            if r["trip_id"] not in known_trips or r["stop_id"] not in known_stops:
                orphans += 1
                continue
            batch.append(StopTime(
                trip_id=r["trip_id"],
                stop_id=r["stop_id"],
                stop_sequence=int(r["stop_sequence"]),
                arrival_s=gtfs_seconds(r.get("arrival_time")),
                departure_s=gtfs_seconds(r.get("departure_time")),
            ))
            if len(batch) >= BATCH:
                total += self._flush(batch)
                batch = []
                if total % 500_000 == 0:
                    self.stdout.write(f"  ... {total:,} stop_times")
        if batch:
            total += self._flush(batch)

        self.stdout.write(f"stop_times  {total:>10,}  (orphaned {orphans})")

    @staticmethod
    def _flush(batch):
        with transaction.atomic():
            StopTime.objects.bulk_create(batch, batch_size=BATCH,
                                         ignore_conflicts=True)
        return len(batch)