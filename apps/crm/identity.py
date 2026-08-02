from __future__ import annotations

import re

from apps.students.models import StudentProfile
from core.privacy import private_fingerprint

_NON_DIGIT = re.compile(r"\D+")


def lead_identity_fingerprints(student: StudentProfile) -> dict[str, str]:
    """Return non-reversible matching keys; never persist normalized raw PII."""

    phone = _NON_DIGIT.sub("", student.phone or "")
    email = (student.email or "").strip().casefold()
    name = " ".join(
        part.strip().casefold()
        for part in (student.first_name, student.middle_name, student.last_name)
        if part and part.strip()
    )
    identity = f"{name}|{student.birthdate.isoformat() if student.birthdate else ''}"
    return {
        "phone_fingerprint": (private_fingerprint(phone, namespace="crm-lead-phone") if phone else ""),
        "email_fingerprint": (private_fingerprint(email, namespace="crm-lead-email") if email else ""),
        "identity_fingerprint": (
            private_fingerprint(identity, namespace="crm-lead-identity") if name and student.birthdate else ""
        ),
    }
