"""Root URL configuration for SEVPS."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("sevps.api_urls")),
    path("", include("apps.dashboards.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

admin.site.site_header = "SEVPS Control Plane"
admin.site.site_title = "SEVPS"
admin.site.index_title = "Smart Emergency Vehicle Priority System"
