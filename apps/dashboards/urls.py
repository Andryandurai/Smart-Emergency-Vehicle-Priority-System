"""Dashboard routing.

Two front ends coexist during the migration:

* ``/legacy/...``  the original server-rendered screens (see legacy_urls.py).
* everything else  the React console, served by a catch-all so client-side
  routes survive a hard refresh.

The catch-all is deliberately last and deliberately excludes ``api/``,
``admin/``, ``static/``, ``media/``, ``ws/`` and ``legacy/`` - those are matched
earlier and must never be swallowed by the SPA.
"""
from django.urls import include, path, re_path

from apps.dashboards import views

urlpatterns = [
    # Session sign-in lives under /legacy/ so ownership of /login is
    # unambiguous: the React console handles it client-side with JWT. Leaving
    # a Django view at /login/ while the SPA claimed /login meant the same
    # screen resolved to two different apps depending on a trailing slash.
    path("legacy/", include("apps.dashboards.legacy_urls")),
    # Must precede the catch-all, and must be at the root: see the view.
    path("sw.js", views.service_worker, name="service-worker"),
    re_path(r"^(?!api/|admin/|static/|media/|ws/|legacy/).*$", views.spa_index, name="spa"),
]
