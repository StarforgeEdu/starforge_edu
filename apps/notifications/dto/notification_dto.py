"""Notifications-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreferenceRowDTO:
    event_type: str
    channel: str
    enabled: bool


@dataclass(frozen=True)
class CreateTemplateDTO:
    event_type: str
    channel: str
    locale: str
    body: str
    subject: str = ""
    is_active: bool = True


@dataclass(frozen=True)
class AnnouncementDTO:
    cohort_id: int
    title: str
    body: str
