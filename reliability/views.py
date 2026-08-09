"""Views for the reliability observatory.

Three endpoints:
    /                      the map page
    /api/segments.geojson  segment performance as GeoJSON
    /api/summary.json      headline figures and coverage

Geometry is stored in EPSG:7856 (GDA2020 / MGA Zone 56) because every distance
calculation in the project needs metres. Web mapping needs EPSG:4326, so the
transform happens at this boundary rather than being baked into storage.

The reprojection is done by PostGIS via a Transform() annotation rather than by
Django's GDAL bindings in Python. Two reasons: it is one round trip instead of
several thousand per-row transforms, and it uses the database's PROJ
installation, which is the one already proven to resolve GDA2020.
"""
import logging
import traceback
from datetime import timedelta

from django.contrib.gis.db.models.functions import Transform
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from reliability.models import Observation, Poll, SegmentPerformance

log = logging.getLogger(__name__)


def map_view(request):
    return render(request, "reliability/map.html")


def segments_geojson(request):
    """Segment performance as GeoJSON in EPSG:4326.

    Only segments meeting the observation threshold are served. A stop with
    four observations and one early bus reads as 75% and would otherwise sit at
    the top of the map's worst-performer list on nothing but noise.
    """
    try:
        peak = request.GET.get("peak")
        route = request.GET.get("route")
        limit = min(int(request.GET.get("limit", 3000)), 8000)

        qs = (SegmentPerformance.objects
              .filter(sufficient_sample=True)
              .annotate(geom4326=Transform("stop__geom", 4326))
              .select_related("stop"))

        if peak in ("true", "false"):
            qs = qs.filter(is_peak=(peak == "true"))
        if route:
            qs = qs.filter(route_short_name=route)

        qs = qs.order_by("otp_pct")[:limit]

        features = []
        for s in qs:
            g = s.geom4326
            if g is None:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point",
                             "coordinates": [round(g.x, 6), round(g.y, 6)]},
                "properties": {
                    "stop_id": s.stop_id,
                    "stop_name": s.stop.stop_name,
                    "route": s.route_short_name,
                    "direction": s.direction_id,
                    "peak": s.is_peak,
                    "otp_pct": round(s.otp_pct, 1),
                    "pct_early": round(s.pct_early, 1),
                    "pct_late": round(s.pct_late, 1),
                    "median_deviation_s": round(s.median_deviation_s),
                    "p90_deviation_s": round(s.p90_deviation_s),
                    "observations": s.observations,
                    "is_terminus": s.is_terminus,
                    "stop_sequence": s.min_stop_sequence,
                    "ewt_s": (round(s.excess_wait_time_s)
                              if s.excess_wait_time_s is not None else None),
                    "bunching_pct": (round(s.bunching_rate_pct, 1)
                                     if s.bunching_rate_pct is not None else None),
                    "sched_headway_s": (round(s.mean_scheduled_headway_s)
                                        if s.mean_scheduled_headway_s is not None
                                        else None),
                    "actual_headway_s": (round(s.mean_actual_headway_s)
                                         if s.mean_actual_headway_s is not None
                                         else None),
                },
            })

        return JsonResponse({"type": "FeatureCollection",
                             "count": len(features),
                             "features": features})

    except Exception as exc:
        # Returning the error as JSON rather than a 500 HTML page means the
        # browser console shows something useful instead of a blank map.
        log.exception("segments_geojson failed")
        return JsonResponse(
            {"error": str(exc), "traceback": traceback.format_exc()[-1500:]},
            status=500)


def summary_json(request):
    """Headline figures plus the coverage statement.

    Coverage is returned alongside the results deliberately. Every figure here
    depends on how much was observed and whether there were gaps, and a
    dashboard that reports performance without reporting its own coverage is
    asking to be trusted rather than checked.
    """
    segs = SegmentPerformance.objects.filter(sufficient_sample=True)
    total_segs = SegmentPerformance.objects.count()

    agg = segs.aggregate(obs=Sum("observations"), ot=Sum("n_on_time"),
                         early=Sum("n_early"), late=Sum("n_late"))
    obs = agg["obs"] or 0

    with_ewt = segs.exclude(excess_wait_time_s__isnull=True)
    worse_than_timetable = with_ewt.filter(excess_wait_time_s__gt=0).count()
    hidden = with_ewt.filter(otp_pct__gte=90, excess_wait_time_s__gt=120).count()

    since = timezone.now() - timedelta(hours=24)
    polls = Poll.objects.filter(polled_at__gte=since)
    last = Poll.objects.filter(ok=True).order_by("-polled_at").first()

    window = segs.first()

    return JsonResponse({
        "window": {
            "start": window.window_start if window else None,
            "end": window.window_end if window else None,
        },
        "volume": {
            "observations_stored": Observation.objects.count(),
            "trips": Observation.objects.values("trip_id").distinct().count(),
            "routes": Observation.objects.values("route_id").distinct().count(),
        },
        "performance": {
            "segments_usable": segs.count(),
            "segments_total": total_segs,
            "observations_analysed": obs,
            "otp_pct": round(100 * (agg["ot"] or 0) / obs, 1) if obs else None,
            "early_pct": round(100 * (agg["early"] or 0) / obs, 1) if obs else None,
            "late_pct": round(100 * (agg["late"] or 0) / obs, 1) if obs else None,
        },
        "regularity": {
            "segments_with_headway_data": with_ewt.count(),
            "worse_than_timetable": worse_than_timetable,
            "punctual_but_irregular": hidden,
        },
        "coverage": {
            "polls_24h": polls.count(),
            "failed_polls_24h": polls.filter(ok=False).count(),
            "last_poll": last.polled_at.isoformat() if last else None,
            "note": ("Figures describe the observation window only and are not "
                     "comparable to published TfNSW punctuality statistics, "
                     "which use different definitions and source systems."),
        },
    })