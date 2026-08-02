"""Expose selected SEVPS settings to server-rendered dashboards."""
from django.conf import settings


def sevps_settings(request):
    cfg = settings.SEVPS
    return {
        "SEVPS_MAP_CENTER": cfg["MAP_CENTER"],
        "SEVPS_MAP_ZOOM": cfg["MAP_ZOOM"],
        "SEVPS_MAPBOX_TOKEN": cfg["MAPBOX_TOKEN"],
        "SEVPS_CV_MODE": cfg["CV_MODE"],
        "SEVPS_DEBUG": settings.DEBUG,
    }
