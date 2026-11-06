"""Spec: OCR-tolerant parsing helpers (no OCR engine needed, so these are fast).

OCR commonly confuses separators in amounts; the parser must recover the intended value instead
of reading "$3.450.00" as 3.45.
"""
import pytest

from app.ocr import _amount, _rows


@pytest.mark.parametrize("ocr_text, value", [
    ("$3,450.00", 3450.00),
    ("$3,450:00", 3450.00),     # colon misread for the decimal point
    ("$3.450.00", 3450.00),     # thousands separator misread as a dot
    ("$ 3,450.00", 3450.00),
    ("$12,000.50", 12000.50),
    ("$0.00", 0.00),
    ("$12", 12.00),
])
def test_amount_recovers_value_from_noisy_ocr(ocr_text, value):
    assert _amount(ocr_text) == pytest.approx(value)


@pytest.mark.parametrize("not_money", ["Invoice", "PO-26-1004", "", None])
def test_text_without_a_dollar_amount_is_not_money(not_money):
    assert _amount(not_money) is None


def _box(x, y, text):
    return {"x0": x, "x1": x + 50, "y0": y - 8, "y1": y + 8, "yc": y, "text": text, "score": 0.9}


def test_words_on_the_same_line_are_grouped_left_to_right():
    boxes = [_box(400, 101, "$3,450.00"), _box(10, 100, "Subtotal"), _box(10, 140, "Tax"), _box(400, 141, "$0.00")]
    rows = _rows(boxes)
    assert [r["text"] for r in rows] == ["Subtotal  $3,450.00", "Tax  $0.00"]
