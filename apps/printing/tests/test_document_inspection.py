from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.printing import document_inspection as inspection


def test_images_have_one_authoritative_page_without_storage_download(monkeypatch):
    monkeypatch.setattr(
        inspection,
        "download_to_path",
        lambda *_args, **_kwargs: pytest.fail("images must not be downloaded for page counting"),
    )
    assert (
        inspection.authoritative_page_count(
            key="tenant/image.png",
            content_type="image/png",
            expected_size_bytes=100,
        )
        == 1
    )


def test_pdf_page_count_uses_bounded_inspector_output(monkeypatch, tmp_path):
    monkeypatch.setattr(inspection.shutil, "which", lambda name, **_kwargs: f"/usr/bin/{name}")

    def run(_command, **kwargs):
        kwargs["stdout"].write(b"Title: safe\nPages:          37\nEncrypted:      no\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(inspection.subprocess, "run", run)
    assert inspection._pdf_page_count(tmp_path / "document.pdf") == 37


def test_pdf_page_count_rejects_encrypted_documents(monkeypatch, tmp_path):
    monkeypatch.setattr(inspection.shutil, "which", lambda name, **_kwargs: f"/usr/bin/{name}")

    def run(_command, **kwargs):
        kwargs["stdout"].write(b"Pages: 2\nEncrypted: yes (print:yes copy:no)\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(inspection.subprocess, "run", run)
    with pytest.raises(inspection.PrintDocumentInspectionError, match="pdf_encrypted"):
        inspection._pdf_page_count(Path(tmp_path) / "document.pdf")
