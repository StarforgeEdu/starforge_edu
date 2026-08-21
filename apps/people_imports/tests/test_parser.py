from __future__ import annotations

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.people_imports.parser import MAX_IMPORT_ROWS, parse_people_file
from core.exceptions import ValidationException


def test_csv_parser_handles_excel_bom_and_preserves_source_rows():
    upload = SimpleUploadedFile(
        "students.csv",
        "First Name,Email\nAziza,aziza@example.test\n".encode("utf-8-sig"),
        content_type="text/csv",
    )

    parsed = parse_people_file(upload)

    assert parsed.headers == ("First Name", "Email")
    assert parsed.rows == [{"First Name": "Aziza", "Email": "aziza@example.test", "__source_row__": "2"}]


def test_xlsx_parser_reads_first_populated_sheet_without_formula_execution():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Teachers"
    sheet.append(["First name", "Phone", "Subjects"])
    sheet.append(["Dilshod", "+998901112233", "English, Speaking"])
    content = BytesIO()
    workbook.save(content)
    workbook.close()
    upload = SimpleUploadedFile(
        "teachers.xlsx",
        content.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    parsed = parse_people_file(upload)

    assert parsed.sheet_name == "Teachers"
    assert parsed.rows[0]["First name"] == "Dilshod"
    assert parsed.rows[0]["Phone"] == "+998901112233"


def test_parser_rejects_more_than_the_bounded_row_limit():
    body = "First name,Email\n" + "\n".join(
        f"Student {index},student{index}@example.test" for index in range(MAX_IMPORT_ROWS + 1)
    )
    upload = SimpleUploadedFile("students.csv", body.encode(), content_type="text/csv")

    with pytest.raises(ValidationException) as caught:
        parse_people_file(upload)

    assert caught.value.code == "too_many_rows"


def test_parser_rejects_legacy_xls_with_a_clear_supported_format_error():
    upload = SimpleUploadedFile("students.xls", b"not-a-workbook")

    with pytest.raises(ValidationException) as caught:
        parse_people_file(upload)

    assert caught.value.code == "unsupported_file_type"
