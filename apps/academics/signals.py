"""Academics domain signals (emit-only today; D3-D audit consumes — TD-9 lists
Grade + ExamResult among audited models).

`grade_changed` fires once when an existing `ExamResult` score is overwritten
(never on first entry). Signature:

    grade_changed.send(
        sender=ExamResult,
        instance=ExamResult,
        old_score=Decimal,
        new_score=Decimal,
        actor_id=int | None,
        schema_name=str,
    )
"""

from __future__ import annotations

import django.dispatch

grade_changed = django.dispatch.Signal()
