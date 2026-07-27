"""Unit tests for StandardEnvelopeRenderer._wrap — the reports-envelope logic,
exercised in isolation (no DB, no HTTP) so the transform is provably correct."""

from __future__ import annotations

from core.renderers import StandardEnvelopeRenderer


def test_wraps_plain_object():
    out = StandardEnvelopeRenderer._wrap({"id": 1, "status": "done"}, 200, view=None)
    assert out == {"success": True, "data": {"id": 1, "status": "done"}}


def test_wraps_plain_list():
    out = StandardEnvelopeRenderer._wrap([1, 2, 3], 200, view=None)
    assert out == {"success": True, "data": [1, 2, 3]}


def test_passes_through_already_enveloped():
    body = {"success": False, "code": "not_found", "message": "gone"}
    assert StandardEnvelopeRenderer._wrap(body, 404, view=None) is body


def test_leaves_non_envelope_error_body_untouched():
    # A 4xx body that isn't our envelope is left for the exception handler to own.
    out = StandardEnvelopeRenderer._wrap({"detail": "nope"}, 400, view=None)
    assert out == {"detail": "nope"}


def test_paginated_dict_becomes_standard_pagination():
    data = {"count": 3, "next": "http://x/?page=2", "previous": None, "results": [1, 2]}
    out = StandardEnvelopeRenderer._wrap(data, 200, view=None)
    assert out["success"] is True
    assert out["data"] == [1, 2]
    pg = out["pagination"]
    assert pg["total"] == 3
    assert pg["page"] == 1
    assert pg["has_next"] is True
    assert pg["has_prev"] is False
