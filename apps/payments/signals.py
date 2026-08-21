"""Payments domain signals (emit-only; D3-C notifications + D3-D audit consume).

Both fire exactly ONCE per state transition (guarded on the previous status in
services.py) inside ``transaction.on_commit`` — listeners never see uncommitted
rows (CODE-GUIDE §7). Flat-primitive kwargs.

    payment_completed.send(
        sender=Payment,
        payment_id=int,
        invoice_id=int | None,
        student_id=int | None,
        amount_uzs=str,        # Decimal serialized as str (exact, JSON-safe)
        schema_name=str,
    )
    payment_failed.send(  # same kwargs
        sender=Payment, payment_id, invoice_id, student_id, amount_uzs, schema_name
    )
"""

from __future__ import annotations

import django.dispatch

payment_completed = django.dispatch.Signal()
payment_failed = django.dispatch.Signal()
