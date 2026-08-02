"""Server-rendered operator UIs.

Django templates + Leaflet + a small vanilla-JS WebSocket client, rather than
a separate SPA build.  For a control-room tool that has to be deployable
inside a municipal network with no Node toolchain, that trade is worth
making: one process serves the API, the sockets and the screens.
"""
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from apps.core.enums import EmergencyCategory, PriorityLevel, TripStage
from apps.dispatch.models import EmergencyTrip
from apps.fleet.models import EmergencyVehicle
from apps.hospitals.models import Hospital
from apps.hospitals.rules import DEFAULT_RULES


def _map_context() -> dict:
    return {
        "map_center": settings.SEVPS["MAP_CENTER"],
        "map_zoom": settings.SEVPS["MAP_ZOOM"],
    }


def operations(request):
    """Emergency Operations Dashboard (feature 4.11)."""
    return render(
        request,
        "dashboards/operations.html",
        {
            **_map_context(),
            "active_trips": EmergencyTrip.objects.active()
            .select_related("vehicle", "destination_hospital")
            .count(),
            "vehicles_online": EmergencyVehicle.objects.online().count(),
            "hospitals": Hospital.objects.filter(is_active=True).count(),
            "stages": TripStage.choices,
        },
    )


def hospital_dashboard(request, code: str):
    """Hospital Preparedness Dashboard (feature 4.7)."""
    hospital = get_object_or_404(Hospital, code__iexact=code, is_active=True)
    return render(
        request,
        "dashboards/hospital.html",
        {**_map_context(), "hospital": hospital},
    )


def hospital_index(request):
    return render(
        request,
        "dashboards/hospital_index.html",
        {"hospitals": Hospital.objects.filter(is_active=True).select_related("capacity_row")},
    )


def analytics(request):
    """AI Traffic Analytics Dashboard (features 4.8 and 4.9)."""
    return render(request, "dashboards/analytics.html", _map_context())


def paramedic(request, callsign: str | None = None):
    """Paramedic application - the Layer 5 / Layer 6 entry point.

    Mirrors what the crew sees on the mobile app: pick a category, get the
    recommended hospital, watch the corridor and the siren directive follow.
    """
    vehicle = (
        get_object_or_404(EmergencyVehicle, callsign__iexact=callsign) if callsign else None
    )
    trip = vehicle.active_trip if vehicle else None
    return render(
        request,
        "dashboards/paramedic.html",
        {
            **_map_context(),
            "vehicle": vehicle,
            "trip": trip,
            "vehicles": EmergencyVehicle.objects.all(),
            "categories": [
                {
                    "code": r["category"],
                    "label": r["display_name"],
                    "required": ", ".join(
                        f.replace("_", " ").title() for f in r["required_facilities"]
                    )
                    or "Emergency department",
                    "level": int(r["default_priority_level"]),
                }
                for r in DEFAULT_RULES
            ],
            "priority_levels": PriorityLevel.choices,
            "emergency_categories": EmergencyCategory.choices,
        },
    )


def driver_view(request):
    """Driver alert receiver - simulates the road user's app / nav overlay."""
    return render(request, "dashboards/driver.html", _map_context())


def board_wall(request):
    """Digital road display board wall - what each VMS sign is showing."""
    return render(request, "dashboards/boards.html", _map_context())


# ---------------------------------------------------------------------------
# React console (Phase 4)
# ---------------------------------------------------------------------------
def service_worker(request):
    """Serve the push service worker from the site root.

    It has to be here rather than bundled as a Vite asset for one hard reason:
    a service worker's scope cannot rise above its own URL. Served from
    ``/static/`` it would control ``/static/`` — the only part of the site with
    no pages in it — and would never receive a push for the console. Served
    from ``/`` it controls everything.

    ``Service-Worker-Allowed`` is set for the same reason, and the response is
    marked no-cache: a stale worker keeps delivering with old logic long after
    a deploy, and there is no user-visible symptom until an alert renders wrong.
    """
    path = Path(settings.BASE_DIR) / "static" / "js" / "sw.js"
    if not path.exists():  # pragma: no cover - only if the file is deleted
        return HttpResponse("// service worker missing", content_type="application/javascript")
    response = HttpResponse(path.read_bytes(), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache, max-age=0"
    return response


def spa_index(request, *args, **kwargs):
    """Serve the built React console for any non-API route.

    The server-rendered screens above are deliberately kept and remain
    reachable under /legacy/. They are the fallback for kiosk and embedded
    displays with no JavaScript build pipeline, and they are how the platform
    stays usable if the SPA build is not present.
    """
    index = Path(settings.BASE_DIR) / "frontend" / "dist" / "index.html"
    if not index.exists():
        return HttpResponse(
            "<h1>SEVPS console not built</h1>"
            "<p>Run <code>npm --prefix frontend install &amp;&amp; "
            "npm --prefix frontend run build</code>, "
            "or use the dev server with <code>npm --prefix frontend run dev</code>.</p>"
            '<p>The server-rendered screens remain available at <a href="/legacy/">/legacy/</a>.</p>',
            status=501,
            content_type="text/html",
        )
    return HttpResponse(index.read_bytes(), content_type="text/html")
