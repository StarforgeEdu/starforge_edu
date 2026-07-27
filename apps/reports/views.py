"""Reports API (D4-LB-5).

Three viewsets: the read-only library (``reports:read``), one-shot runs (create
needs ``reports:write``; reads ``reports:read``), and schedules (``reports:write``
to manage). Role visibility (director all / accountant finance / teacher
enrollment+attendance+grades) is enforced in ``selectors.py`` and the service's
``can_run_report`` gate — not the view.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from apps.reports import selectors, services
from apps.reports.models import Report, ReportRun, ReportSchedule
from apps.reports.serializers import (
    ReportRunCreateSerializer,
    ReportRunReadSerializer,
    ReportScheduleReadSerializer,
    ReportScheduleWriteSerializer,
    ReportSerializer,
)
from core.permissions import DenyWriteForReadOnlyToken, RolePermission, get_user_roles
from core.renderers import StandardEnvelopeRenderer
from core.viewsets import assert_tenant_context


@extend_schema(tags=["reports"])
class ReportViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """The report library, filtered to the caller's allowed roles."""

    serializer_class = ReportSerializer
    permission_classes = [RolePermission]
    renderer_classes = [StandardEnvelopeRenderer]
    resource = "reports"
    queryset = Report.objects.none()  # schema introspection; real qs in get_queryset
    filterset_fields = ("key", "default_format")
    ordering_fields = ("key",)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        assert_tenant_context()

    def get_queryset(self):
        return selectors.scoped_reports(user=self.request.user, roles=get_user_roles(self.request))


@extend_schema(tags=["reports"])
class ReportRunViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """One-shot report runs. POST queues a run (202); GET retrieves status +
    a fresh download URL when done."""

    serializer_class = ReportRunReadSerializer
    permission_classes = [RolePermission, DenyWriteForReadOnlyToken]
    renderer_classes = [StandardEnvelopeRenderer]
    resource = "reports"
    required_perms = {"create": "reports:write"}
    queryset = ReportRun.objects.none()
    filterset_fields = ("report", "status", "format")
    ordering_fields = ("created_at",)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        assert_tenant_context()

    def get_queryset(self):
        return selectors.scoped_runs(user=self.request.user, roles=get_user_roles(self.request))

    @extend_schema(
        summary="Queue a report run",
        request=ReportRunCreateSerializer,
        responses={
            202: ReportRunReadSerializer,
            403: OpenApiResponse(description="report_forbidden envelope"),
            422: OpenApiResponse(description="unknown_report_key / invalid_format envelope"),
        },
        examples=[
            OpenApiExample(
                "Attendance PDF",
                value={"report_key": "attendance", "format": "pdf", "params": {"cohort_id": 3}},
            )
        ],
    )
    def create(self, request, *args, **kwargs):
        ser = ReportRunCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        run = services.create_report_run(
            report_key=ser.validated_data["report_key"],
            fmt=ser.validated_data.get("format"),
            params=ser.validated_data.get("params") or {},
            requested_by=request.user,
            roles=get_user_roles(request),
        )
        return Response(ReportRunReadSerializer(run).data, status=status.HTTP_202_ACCEPTED)


@extend_schema(tags=["reports"])
class ReportScheduleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Recurring report schedules (manage = reports:write)."""

    serializer_class = ReportScheduleReadSerializer
    permission_classes = [RolePermission, DenyWriteForReadOnlyToken]
    renderer_classes = [StandardEnvelopeRenderer]
    resource = "reports"
    required_perms = {"create": "reports:write", "partial_update": "reports:write", "update": "reports:write"}
    queryset = ReportSchedule.objects.none()
    filterset_fields = ("report", "cadence", "is_active")
    ordering_fields = ("created_at",)
    http_method_names = ["get", "post", "patch", "head", "options"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        assert_tenant_context()

    def get_queryset(self):
        return selectors.scoped_schedules(user=self.request.user, roles=get_user_roles(self.request))

    @extend_schema(
        summary="Create a report schedule",
        request=ReportScheduleWriteSerializer,
        responses={201: ReportScheduleReadSerializer},
    )
    def create(self, request, *args, **kwargs):
        ser = ReportScheduleWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        report_key = data.pop("report_key")
        schedule = services.create_schedule(
            report_key=report_key,
            created_by=request.user,
            roles=get_user_roles(request),
            **data,
        )
        return Response(ReportScheduleReadSerializer(schedule).data, status=status.HTTP_201_CREATED)
