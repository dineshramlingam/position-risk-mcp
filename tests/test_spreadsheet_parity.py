"""The server must reproduce 'Position Sizing Calculator for the E-Mini (ES)' v4.5 exactly
when costs are off.

The sheet builds a table of risk % for 1..100 contracts x 1..6 point stops
(risk% = PS * points * $50 / account) and picks the size with
VLOOKUP(fixed_risk, %column, ..., approximate) = the largest PS whose risk% <= fixed risk.
"""
import os
from decimal import Decimal

import pytest

from position_risk_mcp.sizing import spreadsheet_size

POINT_VALUE = 50   # K7
ACCOUNT = 55000    # H5
FIXED_RISK = 0.01  # N8
STOPS = [1, 2, 3, 4, 5, 6]  # E10..O10
MAX_ROWS = 100     # C12..C111


def sheet_vlookup(account, stop_pts, risk_frac, point_value=POINT_VALUE):
    """Literal replica of the spreadsheet's table + approximate VLOOKUP. Returns None for #N/A."""
    found = None
    for ps in range(1, MAX_ROWS + 1):
        risk_pct = Decimal(ps) * Decimal(str(stop_pts)) * Decimal(point_value) / Decimal(str(account))
        if risk_pct <= Decimal(str(risk_frac)):
            found = ps
        else:
            break
    return found


def test_sheet_values_for_55k_at_1pct():
    expected = {1: 11, 2: 5, 3: 3, 4: 2, 5: 2, 6: 1}
    for stop, n in expected.items():
        assert sheet_vlookup(ACCOUNT, stop, FIXED_RISK) == n
        assert spreadsheet_size(ACCOUNT, stop, 1.0, POINT_VALUE) == n


@pytest.mark.parametrize("account", [5000, 10000, 25000, 55000, 100000, 250000])
@pytest.mark.parametrize("risk_pct", [0.5, 1.0, 1.5, 2.0])
@pytest.mark.parametrize("stop", STOPS)
def test_matches_sheet_everywhere(account, risk_pct, stop):
    sheet = sheet_vlookup(account, stop, risk_pct / 100)
    ours = min(spreadsheet_size(account, stop, risk_pct, POINT_VALUE), MAX_ROWS)
    assert ours == (sheet or 0)   # sheet shows #N/A where we return 0


def test_engine_reproduces_sheet_with_costs_off(engine):
    expected = {1: 11, 2: 5, 3: 3, 4: 2, 5: 2, 6: 1}
    for stop, n in expected.items():
        r = engine.calculate_position_size(symbol="ES", side="long", entry=6500, stop=6500 - stop,
                                           include_costs=False)
        assert r["contracts"] == n, (stop, r)
        assert r["risk_per_contract"] == stop * 50


def test_sheet_risk_table_cells():
    # E12 = 1 contract * 1 pt * 50 = $50 ; F12 = 50/55000
    assert Decimal(1) * 1 * 50 == 50
    assert round(50 / 55000, 6) == round(0.000909090909, 6)
    # Tick column D: point value * tick size * PS
    assert 50 * 0.25 * 1 == 12.5


@pytest.mark.skipif(not os.environ.get("POSITION_RISK_XLSX"), reason="set POSITION_RISK_XLSX to the original sheet")
def test_inputs_read_from_original_workbook():
    openpyxl = pytest.importorskip("openpyxl")
    ws = openpyxl.load_workbook(os.environ["POSITION_RISK_XLSX"]).active
    account, point_value, tick, fixed = ws["H5"].value, ws["K7"].value, ws["K8"].value, ws["N8"].value
    stops = [ws[c].value for c in ("E10", "G10", "I10", "K10", "M10", "O10")]
    assert ws["K6"].value == "ES" and tick == 0.25
    for s in stops:
        assert spreadsheet_size(account, s, fixed * 100, point_value) == (sheet_vlookup(account, s, fixed, point_value) or 0)
