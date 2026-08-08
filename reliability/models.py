from django.db import models

# Create your models here.
from django.contrib.gis.db import models


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

    # Seconds since the start of the service day, NOT a time of day.
    # GTFS legitimately contains values like 25:14:00 for trips running past
    # midnight. A TimeField would reject or wrap those, silently deleting every
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

class HarvestRun(models.Model):
    """Heartbeat. Coverage gaps are the one error that can't be fixed later,
    so every poll is recorded and gaps become visible instead of silent."""
    started_at = models.DateTimeField(auto_now_add=True)
    polls = models.IntegerField(default=0)
    observations = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)
    last_feed_ts = models.DateTimeField(null=True)


class Observation(models.Model):
    """One realtime arrival record per trip-stop, overwritten on each poll.

    The feed re-reports every active trip every poll with a shrinking
    prediction error. Upserting on the natural key means the row always holds
    the most recent report, which is the closest thing the feed gives us to a
    realised arrival. Writing every poll instead would produce ~50x the rows and
    bias any average towards long-range predictions.
    """
    # Deliberately NOT a ForeignKey to Trip: the feed carries added and
    # replacement trips that don't exist in the static bundle. A FK would
    # reject exactly the trips worth investigating.
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
        ]