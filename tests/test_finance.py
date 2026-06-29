"""The finance ledger: parsing, storing, summarising, and Excel/CSV in & out.

The snap-a-bill EXTRACTION (OCR + model) is exercised live elsewhere; here we prove the
deterministic core without a network: amounts/dates/categories are cleaned, the ledger stores and
summarises by category and period, and a CSV/XLSX round-trips through import/export.
"""

from __future__ import annotations

import datetime

import pytest

from himmy_app.config import load_config
from himmy_app.finance import (
    CATEGORIES,
    ExpenseStore,
    _clean_category,
    _clean_date,
    _parse_expense_json,
    _to_amount,
)


@pytest.fixture()
def store(tmp_path, monkeypatch) -> ExpenseStore:
    monkeypatch.setenv("HIMMY_APP_DATA_DIR", str(tmp_path / "data"))
    return ExpenseStore(load_config())


# ---- cleaners ---------------------------------------------------------------------------
def test_to_amount():
    assert _to_amount("Rs 1,864.00") == 1864.0
    assert _to_amount("NPR 540") == 540.0
    assert _to_amount(250) == 250.0
    assert _to_amount("free") == 0.0


def test_clean_category():
    assert _clean_category("groceries") == "Groceries"
    assert _clean_category("FOOD") == "Food"
    assert _clean_category("nonsense") == "Other"
    assert _clean_category("") == "Other"


def test_clean_date():
    assert _clean_date("2026-06-28") == "2026-06-28"
    assert _clean_date("garbage") == datetime.date.today().isoformat()
    assert _clean_date("") == datetime.date.today().isoformat()


def test_parse_expense_json():
    assert _parse_expense_json('noise {"amount": 100} tail')["amount"] == 100
    assert _parse_expense_json("not json") is None


# ---- store: add / list / summary / delete -----------------------------------------------
def test_add_and_summary(store):
    today = datetime.date.today().isoformat()
    store.add({"amount": "Rs 1,864", "merchant": "Bhatbhateni", "category": "Groceries", "date": today})
    store.add({"amount": 540, "merchant": "Tazza", "category": "Food", "date": today})
    store.add({"amount": 120, "merchant": "Momo", "category": "food", "date": today})  # case-insensitive

    s = store.summary("month")
    assert s["total"] == 2524.0 and s["count"] == 3 and s["currency"] == "NPR"
    by = {c["category"]: c["total"] for c in s["by_category"]}
    assert by["Groceries"] == 1864.0 and by["Food"] == 660.0   # 540 + 120 merged
    # by_category is sorted by total desc
    assert s["by_category"][0]["category"] == "Groceries"


def test_amount_required_and_category_default(store):
    e = store.add({"amount": 99})            # no merchant/category
    assert e["merchant"] == "Expense" and e["category"] == "Other" and e["currency"] == "NPR"
    assert e["category"] in CATEGORIES


def test_list_filters_and_delete(store):
    today = datetime.date.today().isoformat()
    a = store.add({"amount": 100, "category": "Food", "date": today})
    store.add({"amount": 200, "category": "Transport", "date": today})
    assert len(store.list()) == 2
    assert len(store.list(category="Food")) == 1
    store.delete(a["id"])
    assert len(store.list()) == 1


def test_summary_period_week_excludes_old(store):
    today = datetime.date.today()
    old = (today - datetime.timedelta(days=20)).isoformat()
    store.add({"amount": 1000, "category": "Bills", "date": old})
    store.add({"amount": 50, "category": "Food", "date": today.isoformat()})
    assert store.summary("week")["total"] == 50.0      # the 20-day-old one is outside the week
    assert store.summary("all")["total"] == 1050.0


# ---- Excel / CSV round-trip -------------------------------------------------------------
def test_csv_import(store):
    csv_bytes = (
        "Date,Description,Amount,Category\n"
        "2026-06-01,Petrol Pump,1200,Transport\n"
        "2026-06-02,Pharmacy,450,Health\n"
        "2026-06-03,,0,Skip\n"   # zero amount → skipped
    ).encode("utf-8")
    r = store.import_bytes(csv_bytes, "statement.csv")
    assert r["ok"] and r["imported"] == 2
    assert store.summary("all")["total"] == 1650.0


def test_xlsx_export_roundtrip(store, tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    store.add({"amount": 777, "merchant": "Test", "category": "Shopping",
               "date": datetime.date.today().isoformat()})
    # export writes to ~/Downloads — redirect HOME so the test stays sandboxed
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = store.export(fmt="xlsx")
    assert out["ok"]
    wb = openpyxl.load_workbook(out["path"])
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][0].lower() == "date"           # header
    assert any(r[1] == "Test" and r[2] == 777 for r in rows[1:])
