"""Views for the reliability observatory.

    /                      the map page
    /api/segments.geojson  segment performance as GeoJSON
    /api/summary.json      headline figures and coverage

Two decisions here are worth reading before changing anything.

**Sampling must not be biased.** An earlier version ordered by punctuality and
then truncated to a row limit. Once the result set exceeded the limit, the map
served only the worst segments and silently dropped every good one — a map that
looks plausible and is systematically wrong. Any limit is now applied to a
deterministic modulo sample across the whole set, so the served subset has the
same distribution as the full set.

**Classification follows the data, not the target.** Fixed bands anchored at a
95% target give no discrimination when the entire network sits below 95%:
everything renders one colour and the map stops carrying information. Breaks are
computed as quantiles of the values actually being displayed, and the target is
shown as a reference line instead of as the top of the ramp.
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

# NSW convention: on time is no more than 60s early, no more than 359s late.
OTP_TARGET = 95.0


def map_view(request):
    return render(request, "reliability/map.html")


def _quantile_breaks(values, n_classes=5):
    """Equal-count breaks. Returns the interior boundaries only."""
    if not values:
        return []
    ordered = sorted(values)
    breaks = []
    for i in range(1, n_classes):
        idx = int(round(i * len(ordered) / n_classes))
        idx = max(0, min(len(ordered) - 1, idx))
        breaks.append(round(ordered[idx], 1))
    # Collapse duplicates: a heavily tied distribution can produce identical
    # boundaries, which would create empty classes in the legend.
    out = []
    for b in breaks:
        if not out or b > out[-1]:
            out.append(b)
    return out


def segments_geojson(request):
    """Segment performance as GeoJSON in EPSG:4326, with classification breaks.

    Only segments meeting the observation threshold are served. A stop with four
    observations and one early bus reads as 75% and would otherwise sit at the
    top of any worst-performer list on nothing but noise.
    """
    try:
        peak = request.GET.get("peak")
        route = request.GET.get("route")
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

        # Deterministic even sample rather than a truncation. Ordering by
        # primary key keeps it stable between requests; the modulo stride keeps
        # the sample representative rather than skewed to one tail.
        rows = list(qs.order_by("pk"))
        stride = max(1, -(-total // limit))       # ceiling division
        if stride > 1:
            rows = rows[::stride]

        otp_values = [r.otp_pct for r in rows]
        ewt_values = [r.excess_wait_time_s for r in rows
                      if r.excess_wait_time_s is not None]

        features = []
        for s in rows:
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

        above_target = sum(1 for v in otp_values if v >= OTP_TARGET)

        return JsonResponse({
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "matching": total,
                "served": len(features),
                "sampled": stride > 1,
                "sample_stride": stride,
                "otp": {
                    "breaks": _quantile_breaks(otp_values),
                    "min": round(min(otp_values), 1) if otp_values else None,
                    "max": round(max(otp_values), 1) if otp_values else None,
                    "target": OTP_TARGET,
                    "pct_meeting_target": (round(100 * above_target / len(otp_values), 1)
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
    total_segs = SegmentPerformance.objects.count()

    agg = segs.aggregate(obs=Sum("observations"), ot=Sum("n_on_time"),
                         early=Sum("n_early"), late=Sum("n_late"))
    obs = agg["obs"] or 0

    with_ewt = segs.exclude(excess_wait_time_s__isnull=True)

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
            "meeting_target": segs.filter(otp_pct__gte=OTP_TARGET).count(),
            "peak_segments": segs.filter(is_peak=True).count(),
        },
        "regularity": {
            "segments_with_headway_data": with_ewt.count(),
            "worse_than_timetable": with_ewt.filter(
                excess_wait_time_s__gt=0).count(),
            "punctual_but_irregular": with_ewt.filter(
                otp_pct__gte=90, excess_wait_time_s__gt=120).count(),
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