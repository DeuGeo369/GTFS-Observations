from django.contrib.gis.db import models


# ---------------------------------------------------------------- static GTFS
class Route(models.Model):
    route_id = models.CharField(max_length=64, primary_key=True)
    route_short_name = models.CharField(max_length=32, blank=True)
    route_long_name = models.CharField(max_length=255, blank=True)
    route_type = models.IntegerField(null=True)

    def __str__(self):
        return f"{self.route_short_name} ({self.route_id})"


class Stop(models.Model):
    stop_id = models.CharField(max_length=64, primary_key=True)
    stop_name = models.CharField(max_length=255, blank=True)
    # GDA2020 / MGA Zone 56. Stored projected so distances are in metres.
    geom = models.PointField(srid=7856)

    def __str__(self):
        return f"{self.stop_name} ({self.stop_id})"


class Trip(models.Model):
    trip_id = models.CharField(max_length=64, primary_key=True)
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="trips")
    service_id = models.CharField(max_length=64, blank=True)
    direction_id = models.SmallIntegerField(null=True)
    shape_id = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [models.Index(fields=["route", "direction_id"])]


class StopTime(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="stop_times")
    stop = models.ForeignKey(Stop, on_delete=models.CASCADE, related_name="stop_times")
    stop_sequence = models.IntegerField()

    # Seconds since the start of the service day, NOT a time of day. GTFS
    # legitimately contains values like 25:14:00 for trips running past
    # midnight; a TimeField would reject or wrap those, silently deleting every
    # late-night service from the analysis.
    arrival_s = models.IntegerField(null=True)
    departure_s = models.IntegerField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=["trip", "stop_sequence"]),
            models.Index(fields=["stop"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["trip", "stop_sequence"], name="uniq_trip_stopsequence"
            )
        ]


# ------------------------------------------------------------------- realtime
class HarvestRun(models.Model):
    """Run-level heartbeat."""
    started_at = models.DateTimeField(auto_now_add=True)
    polls = models.IntegerField(default=0)
    observations = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)
    last_feed_ts = models.DateTimeField(null=True)


class Poll(models.Model):
    """One row per poll attempt.

    HarvestRun only counts errors, which cannot tell you *when* data was lost.
    Coverage gaps are the one defect that cannot be repaired after the fact, so
    every attempt is timestamped.
    """
    polled_at = models.DateTimeField(auto_now_add=True, db_index=True)
    feed_ts = models.BigIntegerField(null=True)
    rows = models.IntegerField(default=0)
    ok = models.BooleanField(default=True)
    error = models.CharField(max_length=255, blank=True)


class Observation(models.Model):
    """One realtime arrival record per trip-stop, overwritten on each poll."""
    # Deliberately NOT a ForeignKey to Trip: the feed carries added and
    # replacement trips absent from the static bundle, and a FK would reject
    # exactly the trips worth investigating.
    trip_id = models.CharField(max_length=64)
    service_date = models.CharField(max_length=8)
    stop_id = models.CharField(max_length=64)
    stop_sequence = models.IntegerField()

    route_id = models.CharField(max_length=64, blank=True)
    vehicle_id = models.CharField(max_length=64, blank=True)

    arrival_time = models.BigIntegerField(null=True)     # POSIX seconds
    arrival_delay = models.IntegerField(null=True)       # +ve = late
    departure_time = models.BigIntegerField(null=True)
    departure_delay = models.IntegerField(null=True)

    schedule_relationship = models.CharField(max_length=16, blank=True)
    trip_relationship = models.CharField(max_length=16, blank=True)

    feed_ts = models.BigIntegerField()
    poll_count = models.IntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trip_id", "service_date", "stop_id", "stop_sequence"],
                name="uniq_observation",
            )
        ]
        indexes = [
            models.Index(fields=["route_id", "stop_id"]),
            models.Index(fields=["service_date"]),
            models.Index(fields=["route_id", "stop_id", "arrival_time"]),
        ]


# ------------------------------------------------------------------- curated
class SegmentPerformance(models.Model):
    """Aggregated punctuality for one route / direction / stop / period.

    Rebuilt from scratch by `aggregate`. The headway fields on this model are a
    denormalised rollup across every day held in DailyHeadway, recomputed at the
    end of each `headways` run. The per-day detail lives in DailyHeadway; this
    is the summary the map reads by default.
    """
    route_id = models.CharField(max_length=64)
    route_short_name = models.CharField(max_length=32, blank=True)
    direction_id = models.SmallIntegerField(null=True)
    stop = models.ForeignKey(Stop, on_delete=models.CASCADE,
                             related_name="performance")
    is_peak = models.BooleanField()

    # --- punctuality -----------------------------------------------------
    observations = models.IntegerField()
    n_on_time = models.IntegerField()
    n_early = models.IntegerField()
    n_late = models.IntegerField()

    otp_pct = models.FloatField()
    pct_early = models.FloatField()
    pct_late = models.FloatField()

    median_deviation_s = models.FloatField()
    p90_deviation_s = models.FloatField()
    worst_deviation_s = models.FloatField()

    # Lowest stop_sequence seen. Sequence 1 means a route origin, where an
    # "early arrival" is usually a vehicle berthing before its departure time
    # rather than a service leaving early.
    min_stop_sequence = models.IntegerField(null=True)
    is_terminus = models.BooleanField(default=False)

    # --- headway rollup, mean across all days in DailyHeadway -------------
    headway_days = models.IntegerField(null=True)
    headway_observations = models.IntegerField(null=True)
    mean_scheduled_headway_s = models.FloatField(null=True)
    mean_actual_headway_s = models.FloatField(null=True)
    excess_wait_time_s = models.FloatField(null=True)
    bunching_rate_pct = models.FloatField(null=True)
    gap_rate_pct = models.FloatField(null=True)

    sufficient_sample = models.BooleanField()

    window_start = models.CharField(max_length=8)
    window_end = models.CharField(max_length=8)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["route_id", "direction_id", "stop", "is_peak"],
                name="uniq_segment",
            )
        ]
        indexes = [
            models.Index(fields=["otp_pct"]),
            models.Index(fields=["route_short_name"]),
            models.Index(fields=["excess_wait_time_s"]),
        ]

    def __str__(self):
        period = "peak" if self.is_peak else "off-peak"
        return (f"{self.route_short_name} dir {self.direction_id} "
                f"@ {self.stop_id} ({period}): {self.otp_pct:.1f}%")


class DailyHeadway(models.Model):
    """Headway regularity for one segment on one service day.

    Separate from SegmentPerformance for two reasons.

    First, memory. Computing headways across the whole window at once exhausts a
    2 GB instance; one day at a time fits comfortably. Because each day is
    written as its own row rather than overwriting the last, days accumulate
    instead of replacing one another.

    Second, and more usefully, weekday and weekend service are different
    products. Blending them into a single mean hides exactly the contrast worth
    looking at, so `day_type` is stored and the API can filter on it.
    """
    WEEKDAY, SATURDAY, SUNDAY = "weekday", "saturday", "sunday"
    DAY_TYPES = [(WEEKDAY, "Weekday"), (SATURDAY, "Saturday"), (SUNDAY, "Sunday")]

    route_id = models.CharField(max_length=64)
    route_short_name = models.CharField(max_length=32, blank=True)
    direction_id = models.SmallIntegerField(null=True)
    stop = models.ForeignKey(Stop, on_delete=models.CASCADE,
                             related_name="daily_headways")
    is_peak = models.BooleanField()

    service_date = models.CharField(max_length=8, db_index=True)
    day_type = models.CharField(max_length=8, choices=DAY_TYPES, db_index=True)

    intervals = models.IntegerField()
    mean_scheduled_headway_s = models.FloatField()
    mean_actual_headway_s = models.FloatField()
    excess_wait_time_s = models.FloatField()
    bunching_rate_pct = models.FloatField()
    gap_rate_pct = models.FloatField()

    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["route_id", "direction_id", "stop", "is_peak",
                        "service_date"],
                name="uniq_daily_headway",
            )
        ]
        indexes = [
            models.Index(fields=["service_date", "day_type"]),
            models.Index(fields=["route_id", "stop", "service_date"]),
            models.Index(fields=["excess_wait_time_s"]),
        ]

    def __str__(self):
        return (f"{self.route_short_name} @ {self.stop_id} {self.service_date}: "
                f"EWT {self.excess_wait_time_s:.0f}s")