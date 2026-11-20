# BESS Sizing Software

Streamlit app implementing the *BESS Sizing Software* specification (BESS_SIZING_2_0) on top of the
product team's container database (`data/BESS_DATABASE.xlsx`).

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository (keep `data/BESS_DATABASE.xlsx` in the repo).
2. On <https://share.streamlit.io> choose **New app**, select the repository and set the main file to `app.py`.
3. Optional (Claude narration in the Analyser): in *Advanced settings -> Secrets* add
   `ANTHROPIC_API_KEY = "sk-ant-..."`. Without it the analyser uses its built-in rule-based explanations.

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | Streamlit UI: Inputs, Results, Analyser, Database tabs |
| `bess/database.py` | Reads the Excel database (finds each table by its label, so sheet layouts may differ) |
| `bess/engine.py` | All sizing formulas, year-wise calculation, verification, margin search |
| `bess/analyser.py` | What-if comparison, free-text parser, engineering explanations, optional Claude |
| `bess/report.py` | Excel export |
| `tests/test_bess.py` | `pytest -q` (formulas checked against hand calculations) |

To change the database, replace `data/BESS_DATABASE.xlsx` (same sheet structure) or upload a new file from the sidebar.

## How the specification is implemented

* **Required energy and power** - the app asks for "Required energy as per project (MWh)" and "Required power as per project (MW)", then calculates C-rate = MW / MWh (or you can enter the C-rate manually). The C-rate selects the container and sets the duration (1 / C-rate) unless you enter durations directly. The required energy is the BOL, EOL or year-0 value, depending on the energy basis selected.
* **Type of requirement** - Energy, Power or Both. Energy: size for MWh (energy checks only). Power: required energy = power x discharge time and only the power check applies. Both: energy and power checks.
* **C-rate** - the app asks for the required power and energy and calculates C-rate = power / (BOL) energy, or you can enter the C-rate manually. The C-rate then selects the container and sets the duration (1 / C-rate) unless you enter durations directly.
* **Energy basis** - BOL (EOL = 0.80 x BOL), EOL (BOL = 1.25 x EOL) or year-wise (MWh or % retention per year). BOL/EOL profiles fall linearly between the two values; the initial container count is based on the BOL (or highest year) energy.
* **POI voltage and AC efficiencies** - no predefined values. <= 33 kV: PCS, LV cable, IDT, interconnecting-point cable, measurement accuracy. > 33 kV: PCS, LV cable, IDT, MV cable, switchgear, main transformer, interconnecting-point cable, measurement accuracy. The product of the entered values is the AC chain used for discharge and charge.
* **Container selection (2, 2.1)** - nearest database C-rate; if several containers share it (e.g. 0.33C: 6.626 and 5.015 MWh) every one is sized and shown.
* **Initial containers (2.2-2.3)** - `ceil(Required energy x (1 + margin) / DC energy per container)`. The margin is searched from 14 % to 20 % (0.1 % steps); the lowest margin whose container count satisfies energy, power and RTE in every year is used. If the range is not enough the search is extended and flagged (can be switched off).
* **Discharge (3-6)** - `Installed x Availability x sqrt(DC RTE) x SOH x Calendar degradation x (1 - Mismatch) x DOD`, times the user-entered AC-chain efficiencies, minus `Aux load x Discharge time`.
* **Charge (7-10)** - DC-side value `/ (AC chain x DC RTE)`, plus `Aux load x Charge time`.
* **RTE (13.6-13.7)** - discharge / charge, excluding and including auxiliaries (excluding aux this equals chain^2 x DC RTE).
* **Verification (12, 15)** - BOL energy, EOL energy, EOL RTE and every year's required energy / RTE / power; green MATCHED / red NOT MATCHED (or TRUE/FALSE).
* **FAT/SAT (16)** - 1-6 months, monthly values from the database for the chosen temperature band (editable).
* **Augmentation (17-21)** - add or replace containers in chosen years; augmented energy per batch, AC MV energy including augmentation, auxiliary and plant-infrastructure power.
* **Analyser (11)** - change inputs in the form or type e.g. "reduce DOD to 90% and SOH by 2%"; before/after tables for BOL and EOL, year-wise comparison, containers needed to restore compliance, and an engineering explanation.

## Interpretations to confirm (the spec is ambiguous here)

1. **Calendar degradation** - the database lists monthly values (months 1-9) per temperature band and the SOH column excludes calendar degradation. The app applies `prod(1 - d_m)` for the FAT/SAT months as a one-off factor kept for the whole project. With FAT/SAT = No the factor is 100 %.
2. **Mismatch loss** is applied as `(1 - loss)` in the DC-side formula of every year (section 11 lists it; section 3.1.5 omits it - set it to 0 % to follow 3.1.5 literally). Availability is also applied to augmented containers.
3. **Required energy by year** - pre-filled from the energy basis (editable). The power check is `net energy at POI / discharge time >= required power`.
4. **AC efficiencies** are no longer read from the database (database AC rows are ignored).
5. **Augmented batch ageing** - SOH / DC RTE by age since augmentation (alternative: same index as project year).
6. **Charging side of augmentation** - section 21 multiplies by the efficiencies; the app divides (as in section 10) so that charging energy rises, not falls, with losses. Plant-infrastructure energy uses charge time on the charge side.
7. **Cycles** - checked as `cycles/day x 365 x years >= required cycles`.
8. **Additional auxiliary / standby energy** (MWh per cycle) is booked on the discharge side by default (selectable).

## Database data-quality notes (shown in the app's Database tab)

* `7.044 MWh 0.25C` has no auxiliary loads - enter them under *Override auxiliary values*.
* `6.626 MWh` aux loads (2.25 and 4.06 "MW" per container) are larger than the container's rated power - they look like kW. A sidebar switch reads them as kW.
* `5.015 MWh 0.17C` has SOH data to year 12 only and no temperature band above 50 °C.
