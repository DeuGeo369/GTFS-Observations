from django.urls import path
from reliability import views

urlpatterns = [
    path("", views.map_view, name="map"),
    path("api/segments.geojson", views.segments_geojson),
    path("api/summary.json", views.summary_json),
]