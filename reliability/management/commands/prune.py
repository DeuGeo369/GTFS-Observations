"""Prune raw observations beyond the retention window.

The raw Observation table grows by roughly 300,000 rows a day. Curated results
live in SegmentPerformance, which is small and permanent; the raw rows exist to
rebuild it and to allow re-analysis. Keeping them forever costs storage that,
on a shared database, belongs to something else.

The retention window is therefore also the analysis window, and that is stated
in the coverage note rather than left implicit. Anything that needs to survive
pruning must be aggregated first — which is why this command refuses to run if
the curated table is older than the data it is about to delete.

Usage:
    python manage.py prune --days 14
    python manage.py prune --days 14 --dry-run
"""
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from reliability.models import Observation, Poll, SegmentPerformance


class Command(BaseCommand):
    help = "Delete raw observations older than the retention window"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14,
                            help="retain this many service days (default 14)")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true",
                            help="prune even if aggregation looks stale")

    def handle(self, *args, **opts):
        days = opts["days"]
        cutoff_date = (timezone.localtime() - timedelta(days=days)).strftime("%Y%m%d")

        doomed = Observation.objects.filter(service_date__lt=cutoff_date)
        n = doomed.count()

        self.stdout.write(f"retention window : {days} days")
        self.stdout.write(f"cutoff service   : {cutoff_date}")
        self.stdout.write(f"rows to delete   : {n:,}")
        self.stdout.write(f"rows to keep     : "
                          f"{Observation.objects.count() - n:,}")

        if not n:
            self.stdout.write("nothing to prune")
            return

        # Guard: never delete raw rows that have not been rolled up yet. Without
        # this, a failed aggregation followed by a scheduled prune silently
        # destroys a day of observations with nothing to show for them.
        latest = SegmentPerformance.objects.order_by("-computed_at").first()
        if not opts["force"]:
            if latest is None:
                self.stderr.write(self.style.ERROR(
                    "no aggregation has ever run - refusing to prune. "
                    "Run: python manage.py aggregate"))
                return
            age_h = (timezone.now() - latest.computed_at).total_seconds() / 3600
            if age_h > 6:
                self.stderr.write(self.style.ERROR(
                    f"last aggregation was {age_h:.1f}h ago - refusing to prune. "
                    f"Run aggregate first, or pass --force if you are certain."))
                return
            newest_pruned = doomed.order_by("-service_date").first().service_date
            if latest.window_end < newest_pruned:
                self.stderr.write(self.style.ERROR(
                    f"curated window ends {latest.window_end} but pruning up to "
                    f"{newest_pruned} - aggregate first"))
                return

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("dry run - nothing deleted"))
            return

        # Delete in chunks. A single DELETE of several million rows takes a long
        # lock and bloats the WAL, which on a shared instance affects the other
        # application on it.
        deleted = 0
        while True:
            ids = list(doomed.values_list("id", flat=True)[:50_000])
            if not ids:
                break
            Observation.objects.filter(id__in=ids).delete()
            deleted += len(ids)
            self.stdout.write(f"  deleted {deleted:,} / {n:,}")

        # Poll rows are tiny but unbounded; keep twice the observation window so
        # the coverage history outlives the data it describes.
        poll_cutoff = timezone.now() - timedelta(days=days * 2)
        poll_n, _ = Poll.objects.filter(polled_at__lt=poll_cutoff).delete()

        with connection.cursor() as cur:
            cur.execute("VACUUM ANALYZE reliability_observation;")

        self.stdout.write(self.style.SUCCESS(
            f"pruned {deleted:,} observations and {poll_n:,} poll records, "
            f"vacuumed"))

        with connection.cursor() as cur:
            cur.execute("SELECT pg_size_pretty("
                        "pg_total_relation_size('reliability_observation'));")
            self.stdout.write(f"observation table now: {cur.fetchone()[0]}")
