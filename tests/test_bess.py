"""Unit tests: run with `pytest -q` from the repository root."""
import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bess import analyser  # noqa: E402
from bess.database import load_database, select_by_c_rate  # noqa: E402
from bess.engine import (  # noqa: E402
    AugEvent,
    Inputs,
    calendar_factor,
    ac_keys,
    compute_case,
    resolve_requirements,
    size_container,
    status_labels,
)

AC_LE33 = {"pcs": 0.985, "lv_cable": 0.998, "idt": 0.99, "interconnect_cable": 0.9995, "measurement": 0.998}
AC_GT33 = {**AC_LE33, "mv_cable": 0.995, "switchgear": 0.999, "main_transformer": 0.995}


def make_inputs(**kw):
    kw.setdefault("ac_user", dict(AC_LE33))
    return Inputs(**kw)


DB_FILE = Path(__file__).resolve().parents[1] / "data" / "BESS_DATABASE.xlsx"


@pytest.fixture(scope="module")
def db():
    return {c.id: c for c in load_database(DB_FILE)}


def test_database_parses_all_sheets(db):
    assert len(db) == 6
    assert set(db) == {
        "7.044MWh_0.25C", "6.626MWh_0.5C", "6.626MWh_0.33C",
        "5.015MWh_0.17C", "5.015MWh_0.33C", "5.015MWh_0.25C",
    }
    for c in db.values():
        assert c.soh[0] == 1.0 and len(c.soh) == len(c.dc_rte)
        assert len(c.temp_labels) == len(c.cal_rows[0])


def test_database_quality_flags(db):
    assert db["7.044MWh_0.25C"].aux_discharge_mw is None
    assert db["6.626MWh_0.33C"].aux_suspect and db["6.626MWh_0.5C"].aux_suspect
    assert not db["5.015MWh_0.25C"].aux_suspect


def test_c_rate_matching(db):
    ids = lambda x: sorted(c.id for c in select_by_c_rate(list(db.values()), x))  # noqa: E731
    assert ids(0.33) == ["5.015MWh_0.33C", "6.626MWh_0.33C"]
    assert ids(0.25) == ["5.015MWh_0.25C", "7.044MWh_0.25C"]
    assert ids(0.5) == ["6.626MWh_0.5C"]
    assert ids(0.17) == ["5.015MWh_0.17C"]
    assert ids(0.31) == ["5.015MWh_0.33C", "6.626MWh_0.33C"]  # nearest


def test_calendar_factor_from_temperature(db):
    c = db["6.626MWh_0.33C"]
    f, d, _ = calendar_factor(c, make_inputs(operating_temp_c=25, fatsat_months=3))
    assert d == [0.0094, 0.0033, 0.0024]
    assert f == pytest.approx((1 - 0.0094) * (1 - 0.0033) * (1 - 0.0024))
    f2, _, _ = calendar_factor(c, make_inputs(fatsat_applicable=False))
    assert f2 == 1.0
    with pytest.raises(ValueError):
        calendar_factor(db["5.015MWh_0.17C"], make_inputs(operating_temp_c=52))


def test_discharge_and_charge_formulas_match_spec(db):
    c = db["6.626MWh_0.33C"]
    inp = make_inputs(c_rate=0.33, availability_pct=98, dod_pct=95, mismatch_constant_pct=1.5,
                 fatsat_applicable=False, aux_required=True, aux_overrides={"discharge": 0.05, "charge": 0.04})
    n = 40
    r = compute_case(c, n, inp)
    chain = math.prod(AC_LE33.values())
    td = 1 / 0.33
    for y in (0, 1, 7):
        g = 6.626 * n * 0.98 * math.sqrt(c.dc_rte[y]) * c.soh[y] * (1 - 0.015) * 0.95
        assert r.dis.loc[y, "Guaranteed DC discharge capacity (MWh)"] == pytest.approx(g)
        excl = g * chain
        assert r.dis.loc[y, "Guaranteed capacity at POI excl. aux (MWh)"] == pytest.approx(excl)
        aux = n * 0.05 * td
        assert r.dis.loc[y, "Guaranteed capacity at POI incl. aux (MWh)"] == pytest.approx(excl - aux)
        ch_excl = g / (chain * c.dc_rte[y])
        assert r.chg.loc[y, "Capacity at POI excl. aux (MWh)"] == pytest.approx(ch_excl)
        assert r.chg.loc[y, "Capacity at POI incl. aux (MWh)"] == pytest.approx(ch_excl + n * 0.04 * td)
        assert r.perf.loc[y, "AC RTE excl. aux (%)"] / 100 == pytest.approx(chain ** 2 * c.dc_rte[y])


def test_augmentation_adds_energy_only_from_its_year(db):
    c = db["5.015MWh_0.25C"]
    base = compute_case(c, 25, make_inputs())
    aug = compute_case(c, 25, make_inputs(aug_enabled=True, aug_events=[AugEvent(5, 5, "Add")]))
    col = "AC MV energy incl. augmentation & aux at POI (MWh)"
    for y in range(5):
        assert aug.dis.loc[y, col] == pytest.approx(base.dis.loc[y, col])
    for y in range(5, 11):
        assert aug.dis.loc[y, col] > base.dis.loc[y, col]
    assert (aug.dis["Augmented containers"].iloc[:5] == 0).all()


def test_replace_reduces_initial_fleet(db):
    c = db["5.015MWh_0.25C"]
    r = compute_case(c, 25, make_inputs(aug_enabled=True, aug_events=[AugEvent(4, 6, "Replace")]))
    assert r.dis["Initial containers"].tolist()[:4] == [25] * 4
    assert r.dis["Initial containers"].tolist()[4:] == [19] * 7
    with pytest.raises(ValueError):
        compute_case(c, 5, make_inputs(aug_enabled=True, aug_events=[AugEvent(4, 6, "Replace")]))


def test_margin_search_stays_in_range_when_possible(db):
    c = db["5.015MWh_0.25C"]
    out = size_container(c, resolve_requirements(make_inputs(basis_energy_mwh=100)))
    assert out.in_range and 14 <= out.margin_applied_pct <= 20
    assert out.result.summary["accepted"]
    assert out.result.n_init == math.ceil(100 * (1 + out.margin_applied_pct / 100) / 5.015)
    assert out.result.inputs.eol_energy_mwh == pytest.approx(80.0)


def test_margin_search_flags_when_range_insufficient(db):
    c = db["5.015MWh_0.25C"]
    out = size_container(c, resolve_requirements(make_inputs(energy_basis="YEARWISE", yearwise_energy_mwh=[100.0] * 11)))
    assert not out.in_range and out.margin_applied_pct > 20 and out.result.summary["sizing_ok"]
    strict = size_container(c, resolve_requirements(make_inputs(energy_basis="YEARWISE", yearwise_energy_mwh=[100.0] * 11, allow_extended_margin=False)))
    assert not strict.result.summary["sizing_ok"]


def test_poi_voltage_selects_parameter_set():
    assert ac_keys(33) == ["pcs", "lv_cable", "idt", "interconnect_cable", "measurement"]
    assert len(ac_keys(33.1)) == 8 and "main_transformer" in ac_keys(66)


def test_chain_uses_user_values_and_requires_them(db):
    c = db["5.015MWh_0.25C"]
    low = compute_case(c, 25, make_inputs())
    high = compute_case(c, 25, make_inputs(poi_voltage_kv=66, ac_user=dict(AC_GT33)))
    assert low.summary["chain_eff_pct"] == pytest.approx(math.prod(AC_LE33.values()) * 100)
    assert high.summary["chain_eff_pct"] == pytest.approx(math.prod(AC_GT33.values()) * 100)
    with pytest.raises(ValueError):
        compute_case(c, 25, make_inputs(poi_voltage_kv=66))  # >33 kV values missing


def test_energy_basis_rules():
    bol = resolve_requirements(make_inputs(energy_basis="BOL", basis_energy_mwh=200))
    assert (bol.bol_energy_mwh, bol.eol_energy_mwh) == (200, pytest.approx(160))
    eol = resolve_requirements(make_inputs(energy_basis="EOL", basis_energy_mwh=200))
    assert (eol.eol_energy_mwh, eol.bol_energy_mwh) == (200, pytest.approx(250))
    yw = resolve_requirements(make_inputs(energy_basis="YEARWISE", yearwise_energy_mwh=[100] + [95] * 5 + [90] * 5))
    assert (yw.bol_energy_mwh, yw.eol_energy_mwh) == (100, 90)
    with pytest.raises(ValueError):
        resolve_requirements(make_inputs(energy_basis="YEARWISE", yearwise_energy_mwh=[100, 90]))


def test_fulfilment_modes(db):
    c = db["5.015MWh_0.25C"]
    pw = resolve_requirements(make_inputs(fulfilment="power", required_power_mw=20))
    assert pw.required_energy_mwh == pytest.approx(80.0)  # 20 MW x 4 h
    r = compute_case(c, 30, pw)
    assert not r.summary["check_energy"] and r.summary["check_power"]
    # energy-only ignores power; power-only ignores energy
    en = compute_case(c, 30, resolve_requirements(make_inputs(fulfilment="energy", basis_energy_mwh=100, required_power_mw=999)))
    assert en.summary["check_energy"] and not en.summary["check_power"]
    both = resolve_requirements(make_inputs(fulfilment="both", basis_energy_mwh=100, required_power_mw=999))
    assert not compute_case(c, 30, both).summary["sizing_ok"]  # 999 MW cannot be met


def test_project_longer_than_database_is_rejected(db):
    with pytest.raises(ValueError):
        compute_case(db["5.015MWh_0.17C"], 30, make_inputs(project_years=15))


def test_verification_labels():
    import pandas as pd
    s = pd.Series([True, False])
    assert status_labels(s).tolist() == ["MATCHED", "NOT MATCHED"]
    assert status_labels(s, "TRUE/FALSE").tolist() == ["TRUE", "FALSE"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("reduce DOD to 90% and SOH by 2%", {"dod": ("abs", 90), "soh": ("delta", -2)}),
        ("availability 98", {"availability": ("abs", 98)}),
        ("temperature +10", {"temperature": ("delta", 10)}),
        ("aux x1.5", {"aux": ("scale", 1.5)}),
        ("add 3 containers", {"containers": ("delta", 3)}),
        ("set discharge time to 3.5 h", {"discharge_time": ("abs", 3.5)}),
        ("mismatch losses 2%", {"mismatch": ("abs", 2)}),
    ],
)
def test_parse_changes(text, expected):
    got = analyser.parse_changes(text)
    assert {k: (v["mode"], v["value"]) for k, v in got.items()} == expected


def test_analyser_scenario_and_explanation(db):
    c = db["5.015MWh_0.25C"]
    base = compute_case(c, 28, make_inputs())
    ch = analyser.parse_changes("reduce DOD to 90%")
    inp_s, n_s, _ = analyser.apply_changes(base.inputs, 28, ch)
    scen = compute_case(c, n_s, inp_s)
    comp = analyser.compare(base, scen)
    bol = comp["tables"]["BOL (year 0)"].set_index("Parameter")
    assert bol.loc["DC-side guaranteed discharge capacity (MWh)", "Change (%)"] == pytest.approx(-10.0)
    assert bol.loc["AC RTE excl. aux (%)", "Change"] == pytest.approx(0.0, abs=1e-9)
    assert "DOD" in analyser.explain(ch, base, scen, comp)


def test_app_runs_without_exceptions():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=120).run()
    assert not at.exception
    assert any("Enter the AC-side efficiency" in e.value for e in at.error)  # no predefined AC values
    vals = {"pcs": 98.5, "lv_cable": 99.8, "idt": 99.0, "interconnect_cable": 99.95, "measurement": 99.8}
    for k, v in vals.items():
        at.number_input(key=f"ac_{k}").set_value(v)
    at.run()
    assert not at.exception and not at.error
    next(r for r in at.radio if r.label == "C-rate").set_value("Enter C-rate manually").run()
    next(r for r in at.radio if r.label == "C-rate").set_value("Enter C-rate manually").run()
    next(w for w in at.number_input if w.label == "C-rate").set_value(0.33).run()
    assert not at.exception
    at.chat_input[0].set_value("reduce DOD to 90% and SOH by 2%").run()
    assert not at.exception
    assert any("Interpreted as" in m.value for m in at.markdown)


def test_app_high_voltage_and_modes():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=120).run()
    next(w for w in at.number_input if w.label == "POI voltage (kV)").set_value(66.0).run()
    assert not at.exception
    for k in AC_GT33:
        at.number_input(key=f"ac_{k}").set_value(99.8)
    at.run()
    assert not at.exception
    at.radio[0].set_value("Both energy and power fulfilment").run()
    assert not at.exception
    next(r for r in at.radio if r.label == "The required energy entered above is the").set_value("Year-wise energy requirement").run()
    assert not at.exception
    next(r for r in at.radio if r.label == "The required energy entered above is the").set_value("EOL (end of life) energy").run()
    at.radio[0].set_value("Power fulfilment").run()
    assert not at.exception


def test_app_asks_energy_and_power_and_calculates_c_rate():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=120).run()
    labels = [w.label for w in at.number_input]
    assert "Required energy as per project (MWh)" in labels and "Required power as per project (MW)" in labels
    assert "C-rate" not in labels  # calculated, not asked
    next(w for w in at.number_input if w.label.startswith("Required energy")).set_value(200.0).run()
    next(w for w in at.number_input if w.label.startswith("Required power")).set_value(50.0).run()
    assert any("0.250 C" in i.value for i in at.info)
    next(r for r in at.radio if r.label == "C-rate").set_value("Enter C-rate manually").run()
    assert not at.exception and any(w.label == "C-rate" for w in at.number_input)
    at.radio[0].set_value("Power fulfilment").run()
    assert not at.exception
