"""Finance domain signals (emit-only; D3-C notifications consumes — TD-9/§17).

Both are sent from `services` inside `transaction.on_commit`, so receivers never
observe uncommitted rows. Payload kwargs are flat primitives + the schema name so
receivers can fan out Celery work across tenant contexts.

`invoice_issued` fires once when an invoice transitions to `issued`:

    invoice_issued.send(
        sender=Invoice,
        invoice_id=int,
        student_id=int,
        schema_name=str,
    )

`payment_reminder` fires once per overdue/unpaid invoice per reminder cycle
(the `late_payment_reminders` beat task; dedupe via Lane C
`dedupe_key=f"finance.payment_reminder:{invoice_id}:{date}"`):

    payment_reminder.send(
        sender=Invoice,
        invoice_id=int,
        student_id=int,
        schema_name=str,
    )
"""

from __future__ import annotations

import django.dispatch

invoice_issued = django.dispatch.Signal()
payment_reminder = django.dispatch.Signal()
