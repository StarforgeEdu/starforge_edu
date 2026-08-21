"""Reports URLConf (mounted at /api/v1/reports/ by config/urls.py).

Order matters: the ``runs``/``schedules`` routers are listed before the library
router so ``reports/runs/`` and ``reports/schedules/`` resolve to their own
viewsets rather than the library's ``reports/<pk>/`` retrieve pattern.
"""

from rest_framework.routers import SimpleRouter

from apps.reports.views import ReportRunViewSet, ReportScheduleViewSet, ReportViewSet

runs_router = SimpleRouter()
runs_router.register("runs", ReportRunViewSet, basename="report-runs")

schedules_router = SimpleRouter()
schedules_router.register("schedules", ReportScheduleViewSet, basename="report-schedules")

library_router = SimpleRouter()
library_router.register("", ReportViewSet, basename="reports")

urlpatterns = runs_router.urls + schedules_router.urls + library_router.urls
