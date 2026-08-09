"""Morning health check. One command, everything you need to know.

Reports collection volume, per-hour coverage with gaps made explicit, failed
polls, and the current headline performance figures.

Usage:
    python manage.py status
    python manage.py status --hours 48
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from reliability.models import (HarvestRun, Observation, Poll,
                                SegmentPerformance)

# At a 60s interval we expect 60 polls an hour. Below this, something was wrong.
EXPECTED_POLLS_PER_HOUR = 60
THIN_HOUR_THRESHOLD = 45


class Command(BaseCommand):
    help = "Show harvest coverage and current results"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24,
                            help="hours of coverage detail to show")

    def handle(self, *args, **opts):
        now = timezone.now()
        since = now - timedelta(hours=opts["hours"])

        # ---------------------------------------------------- collection
        self.stdout.write(self.style.HTTP_INFO("\n=== COLLECTION ==="))

        obs_total = Observation.objects.count()
        by_date = dict(Observation.objects.values_list("service_date")
                       .annotate(n=Count("id")).order_by("service_date"))

        self.stdout.write(f"observations stored : {obs_total:,}")
        self.stdout.write(f"distinct trips      : "
                          f"{Observation.objects.values('trip_id').distinct().count():,}")
        self.stdout.write(f"distinct routes     : "
                          f"{Observation.objects.values('route_id').distinct().count():,}")
        self.stdout.write("by service date     :")
        for d, n in by_date.items():
            self.stdout.write(f"    {d}  {n:>10,}")

        # ---------------------------------------------------- liveness
        last = Poll.objects.filter(ok=True).order_by("-polled_at").first()
        if last:
            age_min = (now - last.polled_at).total_seconds() / 60
            style = self.style.SUCCESS if age_min < 5 else self.style.ERROR
            self.stdout.write(style(
                f"\nlast successful poll: {age_min:.1f} minutes ago "
                f"({last.rows:,} rows)"))
            if age_min >= 5:
                self.stdout.write(self.style.ERROR(
                    "  HARVESTER IS NOT RUNNING - restart it now"))
        else:
            self.stdout.write(self.style.ERROR("\nno successful polls recorded"))

        # ---------------------------------------------------- coverage
        self.stdout.write(self.style.HTTP_INFO(
            f"\n=== COVERAGE (last {opts['hours']}h) ==="))

        polls = (Poll.objects.filter(polled_at__gte=since)
                 .extra(select={"h": "date_trunc('hour', polled_at)"})
                 .values("h")
                 .annotate(n=Count("id"),
                           good=Count("id", filter=None),
                           rows=Sum("rows"))
                 .order_by("h"))

        # Build an hour-by-hour picture including hours with no polls at all,
        # which is exactly the case a GROUP BY would hide.
        seen = {}
        for p in Poll.objects.filter(polled_at__gte=since).values(
                "polled_at", "ok"):
            hour = p["polled_at"].replace(minute=0, second=0, microsecond=0)
            entry = seen.setdefault(hour, [0, 0])
            entry[0] += 1
            if p["ok"]:
                entry[1] += 1

        gaps = []
        cursor = since.replace(minute=0, second=0, microsecond=0)
        while cursor < now:
            attempts, good = seen.get(cursor, (0, 0))
            local = timezone.localtime(cursor)
            if good == 0:
                gaps.append(local)
                mark = self.style.ERROR("NO DATA")
            elif good < THIN_HOUR_THRESHOLD:
                mark = self.style.WARNING(f"thin ({good}/{EXPECTED_POLLS_PER_HOUR})")
            else:
                mark = f"{good}/{EXPECTED_POLLS_PER_HOUR}"
            self.stdout.write(f"  {local:%a %d %b %H:00}  {mark}")
            cursor += timedelta(hours=1)

        failed = Poll.objects.filter(polled_at__gte=since, ok=False)
        self.stdout.write(
            f"\nfailed polls in window: {failed.count()}")
        for f in failed.order_by("-polled_at")[:5]:
            self.stdout.write(
                f"    {timezone.localtime(f.polled_at):%d %b %H:%M}  {f.error}")

        if gaps:
            self.stdout.write(self.style.ERROR(
                f"\n{len(gaps)} hour(s) with no data - record these in the "
                f"method note coverage statement"))

        runs = HarvestRun.objects.aggregate(p=Sum("polls"), e=Sum("errors"))
        self.stdout.write(f"\nlifetime: {runs['p'] or 0:,} polls, "
                          f"{runs['e'] or 0} errors across "
                          f"{HarvestRun.objects.count()} runs")

        # ---------------------------------------------------- results
        self.stdout.write(self.style.HTTP_INFO("\n=== RESULTS ==="))
        segs = SegmentPerformance.objects.filter(sufficient_sample=True)
        n = segs.count()
        if not n:
            self.stdout.write("no aggregated results yet - run: "
                              "python manage.py aggregate")
            return

        agg = segs.aggregate(obs=Sum("observations"), ot=Sum("n_on_time"),
                             early=Sum("n_early"), late=Sum("n_late"))
        total = agg["obs"] or 1
        window = segs.first()

        self.stdout.write(f"window        : {window.window_start} to "
                          f"{window.window_end}")
        self.stdout.write(f"usable segments: {n:,} of "
                          f"{SegmentPerformance.objects.count():,}")
        self.stdout.write(f"observations   : {total:,}")
        self.stdout.write(f"  on time  {100 * agg['ot'] / total:5.1f}%")
        self.stdout.write(f"  early    {100 * agg['early'] / total:5.1f}%")
        self.stdout.write(f"  late     {100 * agg['late'] / total:5.1f}%")

        self.stdout.write("\nworst 10 segments by on-time percentage:")
        self.stdout.write(f"  {'route':>8} {'dir':>4} {'period':>8} {'OTP':>7} "
                          f"{'obs':>6}  stop")
        for s in segs.select_related("stop").order_by("otp_pct")[:10]:
            period = "peak" if s.is_peak else "off-peak"
            self.stdout.write(
                f"  {s.route_short_name:>8} {s.direction_id if s.direction_id is not None else '-':>4} "
                f"{period:>8} {s.otp_pct:>6.1f}% {s.observations:>6}  "
                f"{s.stop.stop_name[:44]}")