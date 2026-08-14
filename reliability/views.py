"""Views for the reliability observatory.

    /                      the map page
    /api/segments.geojson  segment performance as GeoJSON
    /api/summary.json      headline figures and coverage

Excess wait time can be requested for all days, weekdays only, or weekends only
via ?days=all|weekday|weekend. The default rollup on SegmentPerformance is the
mean across every day held; the filtered variants are computed from DailyHeadway
on request, which is what makes a weekday-versus-weekend comparison possible.

Geometry is stored in EPSG:7856 because every distance calculation needs metres.
Reprojection to EPSG:4326 is done by PostGIS at this boundary rather than by
Django's GDAL bindings in Python: one round trip instead of thousands of per-row
transforms, using the PROJ installation already proven to resolve GDA2020.
"""
import logging
import traceback
from datetime import timedelta

from django.contrib.gis.db.models.functions import Transform
from django.db.models import Avg, Count, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from reliability.models import (DailyHeadway, Observation, Poll,
                                SegmentPerformance)

log = logging.getLogger(__name__)

OTP_TARGET = 95.0


def map_view(request):
    return render(request, "reliability/map.html")


def _quantile_breaks(values, n_classes=5):
    """Equal-count breaks. Returns interior boundaries only."""
    if not values:
        return []
    ordered = sorted(values)
    breaks = []
    for i in range(1, n_classes):
        idx = max(0, min(len(ordered) - 1,
                         int(round(i * len(ordered) / n_classes))))
        breaks.append(round(ordered[idx], 1))
    # Collapse duplicates: a heavily tied distribution would otherwise produce
    # identical boundaries and empty classes in the legend.
    out = []
    for b in breaks:
        if not out or b > out[-1]:
            out.append(b)
    return out


def _headway_by_day_type(day_filter):
    """Per-segment headway means restricted to a day type.

    Returns {} for 'all', in which case the caller uses the rollup already
    stored on SegmentPerformance.
    """
    if day_filter not in ("weekday", "weekend"):
        return {}

    types = ([DailyHeadway.WEEKDAY] if day_filter == "weekday"
             else [DailyHeadway.SATURDAY, DailyHeadway.SUNDAY])

    agg = (DailyHeadway.objects
           .filter(day_type__in=types)
           .values("route_id", "direction_id", "stop_id", "is_peak")
           .annotate(days=Count("service_date", distinct=True),
                     ewt=Avg("excess_wait_time_s"),
                     sched=Avg("mean_scheduled_headway_s"),
                     actual=Avg("mean_actual_headway_s"),
                     bunch=Avg("bunching_rate_pct")))

    return {(a["route_id"], a["direction_id"], a["stop_id"], a["is_peak"]): a
            for a in agg}


def segments_geojson(request):
    """Segment performance as GeoJSON in EPSG:4326, with classification breaks.

    Only segments meeting the observation threshold are served. Any row limit is
    applied as a deterministic modulo sample across the whole ordered set, never
    as a truncation of a sorted list: truncating after ordering by punctuality
    would serve only the worst segments and produce a map that looks plausible
    and is systematically wrong.
    """
    try:
        peak = request.GET.get("peak")
        route = request.GET.get("route")
        days = request.GET.get("days", "all")
        limit = min(int(request.GET.get("limit", 6000)), 12000)

        qs = (SegmentPerformance.objects
              .filter(sufficient_sample=True)
              .annotate(geom4326=Transform("stop__geom", 4326))
              .select_related("stop"))

        if peak in ("true", "false"):
            qs = qs.filter(is_peak=(peak == "true"))
        if route:
            qs = qs.filter(route_short_name=route)

        total = qs.count()
        rows = list(qs.order_by("pk"))
        stride = max(1, -(-total // limit))       # ceiling division
        if stride > 1:
            rows = rows[::stride]

        by_day = _headway_by_day_type(days)

        features, otp_values, ewt_values = [], [], []
        for s in rows:
            g = s.geom4326
            if g is None:
                continue

            if by_day:
                a = by_day.get((s.route_id, s.direction_id,
                                s.stop_id, s.is_peak))
                ewt = a["ewt"] if a else None
                sched = a["sched"] if a else None
                actual = a["actual"] if a else None
                bunch = a["bunch"] if a else None
                hdays = a["days"] if a else None
            else:
                ewt = s.excess_wait_time_s
                sched = s.mean_scheduled_headway_s
                actual = s.mean_actual_headway_s
                bunch = s.bunching_rate_pct
                hdays = s.headway_days

            otp_values.append(s.otp_pct)
            if ewt is not None:
                ewt_values.append(ewt)

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
                    "ewt_s": round(ewt) if ewt is not None else None,
                    "headway_days": hdays,
                    "bunching_pct": round(bunch, 1) if bunch is not None else None,
                    "sched_headway_s": round(sched) if sched is not None else None,
                    "actual_headway_s": round(actual) if actual is not None else None,
                },
            })

        above_target = sum(1 for v in otp_values if v >= OTP_TARGET)

        return JsonResponse({
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "matching": total,
                "served": len(features),
                "sampled": stride > 1,
                "sample_stride": stride,
                "day_filter": days,
                "otp": {
                    "breaks": _quantile_breaks(otp_values),
                    "min": round(min(otp_values), 1) if otp_values else None,
                    "max": round(max(otp_values), 1) if otp_values else None,
                    "target": OTP_TARGET,
                    "pct_meeting_target": (
                        round(100 * above_target / len(otp_values), 1)
                        if otp_values else None),
                },
                "ewt": {
                    "breaks": _quantile_breaks(ewt_values),
                    "min": round(min(ewt_values)) if ewt_values else None,
                    "max": round(max(ewt_values)) if ewt_values else None,
                    "n": len(ewt_values),
                },
            },
        })

    except Exception as exc:
        # JSON rather than a 500 HTML page, so the browser console shows
        # something useful instead of a blank map.
        log.exception("segments_geojson failed")
        return JsonResponse(
            {"error": str(exc), "traceback": traceback.format_exc()[-1500:]},
            status=500)


def summary_json(request):
    """Headline figures plus the coverage statement.

    Coverage is returned alongside the results deliberately. Every figure here
    depends on how much was observed and whether there were gaps, and a
    dashboard reporting performance without reporting its own coverage is asking
    to be trusted rather than checked.
    """
    segs = SegmentPerformance.objects.filter(sufficient_sample=True)
    agg = segs.aggregate(obs=Sum("observations"), ot=Sum("n_on_time"),
                         early=Sum("n_early"), late=Sum("n_late"))
    obs = agg["obs"] or 0

    with_ewt = segs.exclude(excess_wait_time_s__isnull=True)

    by_type = {}
    for dt, label in DailyHeadway.DAY_TYPES:
        q = DailyHeadway.objects.filter(day_type=dt)
        if not q.exists():
            continue
        a = q.aggregate(ewt=Avg("excess_wait_time_s"),
                        bunch=Avg("bunching_rate_pct"),
                        days=Count("service_date", distinct=True),
                        segments=Count("id"))
        by_type[dt] = {
            "days": a["days"],
            "segment_days": a["segments"],
            "mean_ewt_s": round(a["ewt"], 1),
            "mean_bunching_pct": round(a["bunch"], 1),
        }

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
            "segments_total": SegmentPerformance.objects.count(),
            "observations_analysed": obs,
            "otp_pct": round(100 * (agg["ot"] or 0) / obs, 1) if obs else None,
            "early_pct": round(100 * (agg["early"] or 0) / obs, 1) if obs else None,
            "late_pct": round(100 * (agg["late"] or 0) / obs, 1) if obs else None,
            "meeting_target": segs.filter(otp_pct__gte=OTP_TARGET).count(),
            "peak_segments": segs.filter(is_peak=True).count(),
        },
        "regularity": {
            "segments_with_headway_data": with_ewt.count(),
            "headway_service_days": DailyHeadway.objects.values(
                "service_date").distinct().count(),
            "worse_than_timetable": with_ewt.filter(
                excess_wait_time_s__gt=0).count(),
            "punctual_but_irregular": with_ewt.filter(
                otp_pct__gte=90, excess_wait_time_s__gt=120).count(),
            "by_day_type": by_type,
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