"""Finance ledger (D3-A-1): delete the FinanceItem placeholder and create the
real billing models. Hand-written to mirror the model definitions; the
orchestrator may renumber/regenerate against the final model graph.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0001_initial"),
        ("cohorts", "0003_initial"),
        ("org", "0006_centersettings_storage_quota_gb"),
        ("students", "0002_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.DeleteModel(name="FinanceItem"),
        migrations.CreateModel(
            name="FeeSchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("amount_uzs", models.DecimalField(decimal_places=2, max_digits=18)),
                (
                    "billing_period",
                    models.CharField(
                        choices=[("monthly", "Monthly"), ("term", "Term"), ("one_time", "One-time")],
                        default="monthly",
                        max_length=10,
                    ),
                ),
                ("due_day_of_month", models.PositiveSmallIntegerField(default=5)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "cohort",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fee_schedules",
                        to="cohorts.cohort",
                    ),
                ),
            ],
            options={"ordering": ("name",)},
        ),
        migrations.CreateModel(
            name="Invoice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(max_length=32, unique=True)),
                (
                    "period",
                    models.CharField(
                        blank=True,
                        help_text="Billing period key (e.g. '2026-06') for enrollment dedupe.",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("issued", "Issued"),
                            ("partially_paid", "Partially paid"),
                            ("paid", "Paid"),
                            ("void", "Void"),
                            ("overdue", "Overdue"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("issue_date", models.DateField(blank=True, null=True)),
                ("due_date", models.DateField(blank=True, db_index=True, null=True)),
                ("currency", models.CharField(default="UZS", max_length=3)),
                ("total_uzs", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("fx_rate_usd", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("fx_source", models.CharField(blank=True, max_length=32)),
                ("total_usd", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "cohort",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="invoices",
                        to="cohorts.cohort",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "fee_schedule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="invoices",
                        to="finance.feeschedule",
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoices",
                        to="students.studentprofile",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="InvoiceLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.CharField(max_length=255)),
                (
                    "line_type",
                    models.CharField(
                        choices=[
                            ("tuition", "Tuition"),
                            ("material", "Material"),
                            ("penalty", "Penalty"),
                            ("discount", "Discount"),
                            ("other", "Other"),
                        ],
                        default="tuition",
                        max_length=10,
                    ),
                ),
                ("quantity", models.DecimalField(decimal_places=2, default=1, max_digits=8)),
                ("unit_price_uzs", models.DecimalField(decimal_places=2, max_digits=18)),
                ("amount_uzs", models.DecimalField(decimal_places=2, max_digits=18)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lines",
                        to="finance.invoice",
                    ),
                ),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.CreateModel(
            name="Discount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "discount_type",
                    models.CharField(
                        choices=[("sibling", "Sibling"), ("scholarship", "Scholarship"), ("manual", "Manual")],
                        max_length=12,
                    ),
                ),
                ("percent", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("fixed_amount_uzs", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ("valid_from", models.DateField(blank=True, null=True)),
                ("valid_until", models.DateField(blank=True, null=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discounts",
                        to="students.studentprofile",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="PaymentPlan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invoice",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="payment_plan",
                        to="finance.invoice",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="PaymentPlanInstallment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("due_date", models.DateField()),
                ("amount_uzs", models.DecimalField(decimal_places=2, max_digits=18)),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("paid", "Paid"), ("overdue", "Overdue")],
                        default="pending",
                        max_length=8,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="installments",
                        to="finance.paymentplan",
                    ),
                ),
            ],
            options={"ordering": ("due_date",)},
        ),
        migrations.CreateModel(
            name="PaymentAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("payment_id", models.BigIntegerField(db_index=True)),
                ("amount_uzs", models.DecimalField(decimal_places=2, max_digits=18)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="allocations",
                        to="finance.invoice",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="Refund",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("payment_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("amount_uzs", models.DecimalField(decimal_places=2, max_digits=18)),
                ("reason", models.CharField(blank=True, max_length=255)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("requested", "Requested"),
                            ("approved", "Approved"),
                            ("sent_to_provider", "Sent to provider"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="requested",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="refunds",
                        to="finance.invoice",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="CashierShift",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Open"), ("closed", "Closed")],
                        db_index=True,
                        default="open",
                        max_length=8,
                    ),
                ),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("opening_cash_uzs", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("closing_cash_uzs", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ("discrepancy_uzs", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ("notes", models.TextField(blank=True)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cashier_shifts",
                        to="org.branch",
                    ),
                ),
                (
                    "cashier",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cashier_shifts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-opened_at",)},
        ),
        migrations.AddIndex(
            model_name="feeschedule",
            index=models.Index(fields=["is_active", "cohort"], name="finance_fee_is_acti_95ead7_idx"),
        ),
        migrations.AddConstraint(
            model_name="feeschedule",
            constraint=models.CheckConstraint(
                condition=models.Q(amount_uzs__gte=0), name="fee_schedule_amount_non_negative"
            ),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["student", "status"], name="finance_inv_student_96f21e_idx"),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["status", "due_date"], name="finance_inv_status_0e2dc8_idx"),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(total_uzs__gte=0), name="invoice_total_non_negative"
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("period", ""), _negated=True) & models.Q(fee_schedule__isnull=False),
                fields=("student", "fee_schedule", "period"),
                name="invoice_one_per_student_schedule_period",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoiceline",
            constraint=models.CheckConstraint(
                condition=models.Q(amount_uzs__gte=0) | models.Q(line_type="discount"),
                name="invoice_line_negative_only_discount",
            ),
        ),
        migrations.AddIndex(
            model_name="discount",
            index=models.Index(fields=["student", "is_active"], name="finance_dis_student_e81285_idx"),
        ),
        migrations.AddConstraint(
            model_name="discount",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(percent__isnull=False, fixed_amount_uzs__isnull=True)
                    | models.Q(percent__isnull=True, fixed_amount_uzs__isnull=False)
                ),
                name="discount_exactly_one_of_percent_or_fixed",
            ),
        ),
        migrations.AddIndex(
            model_name="paymentplaninstallment",
            index=models.Index(fields=["plan", "status"], name="finance_pay_plan_id_d0a266_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentallocation",
            index=models.Index(fields=["payment_id"], name="finance_pay_payment_c4ed6e_idx"),
        ),
        migrations.AddConstraint(
            model_name="paymentallocation",
            constraint=models.CheckConstraint(
                condition=models.Q(amount_uzs__gt=0), name="allocation_amount_positive"
            ),
        ),
        migrations.AddIndex(
            model_name="refund",
            index=models.Index(fields=["invoice", "state"], name="finance_ref_invoice_277dcc_idx"),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                condition=models.Q(amount_uzs__gt=0), name="refund_amount_positive"
            ),
        ),
        migrations.AddIndex(
            model_name="cashiershift",
            index=models.Index(fields=["cashier", "status"], name="finance_cas_cashier_108bd1_idx"),
        ),
        migrations.AddConstraint(
            model_name="cashiershift",
            constraint=models.UniqueConstraint(
                condition=models.Q(status="open"), fields=("cashier",), name="one_open_shift_per_cashier"
            ),
        ),
    ]
