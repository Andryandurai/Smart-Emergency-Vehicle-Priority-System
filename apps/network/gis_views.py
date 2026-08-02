"""GIS REST surface (Phase 8).

One catalogue endpoint and one layer endpoint. Adding a layer means adding an
entry to ``gis.LAYERS``; no new view, no new URL, no client change beyond
choosing to display it.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsAuthenticatedRole, PublicRead
from apps.network import gis


class LayerCatalogueView(APIView):
    """``GET /api/v1/network/gis/layers/`` - what can be drawn, and by whom."""

    permission_classes = [PublicRead]

    def get(self, request):
        return Response(
            {
                "layers": gis.catalogue(),
                "basemaps": gis.basemap_providers(),
                "note": (
                    "All geometry is GeoJSON [lon, lat]. Layers marked public "
                    "need no credentials; the rest require a signed-in role."
                ),
            }
        )


class LayerView(APIView):
    """``GET /api/v1/network/gis/layers/<name>/`` - one layer as GeoJSON.

    Permissions are resolved per layer rather than per URL: the road network is
    public infrastructure data, live vehicle positions are not, and both are
    served by this view.
    """

    permission_classes = [PublicRead]

    def get_permissions(self):
        spec = gis.LAYERS.get(self.kwargs.get("name", ""))
        if spec is None or spec.public:
            return [PublicRead()]
        return [IsAuthenticatedRole()]

    def get(self, request, name: str):
        spec = gis.LAYERS.get(name)
        if spec is None:
            return Response(
                {
                    "detail": f"unknown layer {name!r}",
                    "available": sorted(gis.LAYERS),
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        options: dict = {}
        for key, caster in (("limit", int), ("days", int), ("window_days", int)):
            raw = request.query_params.get(key)
            if raw is not None:
                try:
                    options[key] = caster(raw)
                except ValueError:
                    return Response(
                        {"detail": f"{key} must be an integer"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        return Response(gis.build(name, **options))


@api_view(["GET"])
@permission_classes([PublicRead])
def basemaps(request):
    """``GET /api/v1/network/gis/basemaps/`` - tile providers this install can serve."""
    return Response(gis.basemap_providers())
