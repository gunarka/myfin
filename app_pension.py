"""
app_pension.py
Altersvorsorge: Verwaltung der Vorsorge-Bausteine (gesetzlich, betrieblich,
privat, ETF, Immobilie), ein Entgeltpunkte-Rechner für die gesetzliche Rente,
Kapital-Hochrechnung per Zinseszins sowie ein Rentenlücken-Rechner
(Wunsch-Einkommen vs. erwartete Rente + Kapitalentnahme).

Tab „Verwalten“: Bausteine anlegen/bearbeiten/löschen, je Person das Szenario
zur gesetzlichen Rente sowie einzubeziehende weitere Bausteine auswählen,
Annahmen zur Netto-Berechnung je Person pflegen und die Ergebnisse (Netto-
Rente) einsehen – dabei werden ggf. versetzte Rentenbeginn-Zeitpunkte der
einzelnen Bausteine berücksichtigt (ein Baustein zählt erst mit, sobald sein
eigener Rentenbeginn erreicht ist).

Tab „Gesetzliche Rente“: Fakten (Person, Geburtsdatum, Entgeltpunkte-Historie,
aktueller Rentenwert) je Person sowie **personenunabhängige** Szenarien zur
Rentenentwicklung (Renteneintritt/Zugangsfaktor, Rentenfaktor, ggf. festes
Erwerbsminderungs-Startdatum, Rentenwert-Entwicklung, zukünftige Entgelt-
punkte). Ergebnisse werden für eine frei wählbare Person/Szenario-Kombination
dargestellt.

SICHERHEIT: Alle DB-Zugriffe parametrisiert. Reine lokale Berechnung, keine
externen Kurs- oder Rentendaten-Abrufe.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from datetime import date

from app_functions import (
    require_master_password,
    list_saved_users,
    colour_amount,
    # Altersvorsorge-Funktionen
    load_pension_plans, save_pension_plan, update_pension_plan, delete_pension_plan,
    project_capital, required_monthly_saving,
    PENSION_TYPES, STATUS_TYPES,
    # Gesetzliche Rente – Fakten (je Person)
    load_pension_income, save_pension_income_year, update_pension_income, delete_pension_income,
    ensure_pension_income_years, INCOME_START_YEAR,
    durchschnittsentgelt_fuer_jahr, entgeltpunkte_jahr, berechne_gesetzliche_rente,
    regelaltersgrenze_zusatz, berechne_netto_rente,
    KV_ZUSATZBEITRAG_DURCHSCHNITT, PV_KINDERLOS_ZUSCHLAG,
    save_pension_facts, load_pension_facts,
    # Gesetzliche Rente – Szenarien (global) & Auswahl/Annahmen (je Person)
    save_pension_scenario, load_pension_scenario, list_pension_scenarios, delete_pension_scenario,
    set_pension_active_scenario, get_pension_active_scenario,
    save_pension_netto_annahmen, load_pension_netto_annahmen,
    save_pension_baustein_auswahl, load_pension_baustein_auswahl,
    save_pension_zukunft_ep, load_pension_zukunft_ep,
    save_pension_last_context, load_pension_last_context,
    AKTUELLER_RENTENWERT, RENTENFAKTOR_OPTIONS,
    # Spalten-Definitionen
    col_pers, col_pname, col_ptyp, col_panb, col_pmon, col_pwert, col_prente, col_prend,
    col_pbeg, col_status, col_note, col_gjah, col_gind, col_gdur, col_gep,
    DASHBOARD_COLORS, make_plotly_theme,
)

# ── Sicherheits-Gate ──────────────────────────────────────────────────────────
require_master_password()

st.title("🏖️ Altersvorsorge")

# ── Design-System (zentral in app_functions.py, hier nur referenziert) ───────
C = DASHBOARD_COLORS
PLOTLY_THEME = make_plotly_theme()
PLOTLY_CONFIG = {
    "displaylogo": False,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "toImage"],
}
CHART_HEIGHT = 420

# ── Stammdaten ─────────────────────────────────────────────────────────────────
accounts    = list_saved_users()
all_persons = sorted(accounts["Person"].unique().tolist(), key=str.lower) if not accounts.empty else []

today = date.today()
RENTENBEGINN_MIN = date(1950, 1, 1)
RENTENBEGINN_MAX = date(today.year + 80, 1, 1)


def _years_to(d) -> float:
    """Jahre (float) von heute bis zu einem Datum; 0 wenn Datum in der Vergangenheit/leer."""
    if d is None or pd.isna(d):
        return 0.0
    d = pd.Timestamp(d).date()
    return max(0.0, (d - today).days / 365.25)


def _lade_gesetzliche_rente_ergebnis(person: str, scenario_name: str | None):
    """Lädt Fakten der Person und das (globale) Szenario und berechnet daraus
    die gesetzliche Rente. Gibt (result, params, facts) zurück; `result` ist
    None, wenn kein Geburtsdatum oder kein Szenario vorliegt. `params` enthält
    zusätzlich zu den globalen Szenario-Werten den Schlüssel "zukunft_ep" –
    dieser wird je Kombination aus Person **und** Szenario geladen (persönliche
    Angabe + Szenario-Annahme zugleich)."""
    facts = load_pension_facts(person)
    if not facts.get("geburtsdatum") or not scenario_name:
        return None, None, facts
    params = load_pension_scenario(scenario_name) or {}
    zukunft_ep = load_pension_zukunft_ep(person, scenario_name)
    income_df = load_pension_income(person)
    geburtsdatum = date.fromisoformat(facts["geburtsdatum"])
    rentenwert = float(facts.get("rentenwert", AKTUELLER_RENTENWERT))
    ep_override = facts.get("ep_override")
    em_start = (date.fromisoformat(params["erwerbsminderung_start"])
                if params.get("erwerbsminderung_start") else None)
    result = berechne_gesetzliche_rente(
        income_df, geburtsdatum,
        int(params.get("monate_abweichung", 0)),
        float(zukunft_ep) if zukunft_ep is not None else 0.0,
        rentenwert,
        float(params.get("rentenwert_entwicklung", 0.0)),
        RENTENFAKTOR_OPTIONS.get(params.get("rentenfaktor_label", "Altersrente (1,0)"), 1.0),
        ep_override=float(ep_override) if ep_override else None,
        erwerbsminderung_start=em_start,
    )
    return result, {**params, "zukunft_ep": zukunft_ep if zukunft_ep is not None else 0.0}, facts


# ── Tab-Struktur ──────────────────────────────────────────────────────────────
tab_manage, tab_gesetzlich, tab_forecast, tab_gap = st.tabs([
    "📋 Verwalten",
    "🏛️ Gesetzliche Rente",
    "📈 Kapital-Prognose",
    "🎯 Rentenlücke",
])


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 1: Verwalten
# ═══════════════════════════════════════════════════════════════════════════════
with tab_manage:
    # ── Sektion 1: Bausteine erstellen, bearbeiten, löschen ─────────────────
    st.subheader("Vorsorge-Bausteine")
    st.caption(
        "Gesetzliche Rente, bAV, Riester/Rürup, ETF-Sparpläne, Immobilien u.a. "
        "Werte stammen aus der letzten Renteninformation / dem aktuellen Depotauszug "
        "und werden hier manuell gepflegt – kein automatischer Abruf."
    )

    with st.expander("➕ Neuen Vorsorge-Baustein anlegen"):
        with st.form("pension_new", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                p_person = st.selectbox(col_pers.lab, options=[""] + all_persons, key="pn_person")
                p_name   = st.text_input(col_pname.lab, key="pn_name",
                                          placeholder="z. B. „Hauptrente“, „Direktversicherung Firma X“")
                p_typ    = st.selectbox(col_ptyp.lab, options=PENSION_TYPES, key="pn_typ")
                p_anb    = st.text_input(col_panb.lab, key="pn_anb")
            with c2:
                p_mon    = st.number_input(col_pmon.lab, min_value=0.0, value=0.0,
                                            step=10.0, format="%.2f", key="pn_mon")
                p_wert   = st.number_input(col_pwert.lab, min_value=0.0, value=0.0,
                                            step=100.0, format="%.2f", key="pn_wert")
                p_rente  = st.number_input(col_prente.lab, min_value=0.0, value=0.0,
                                            step=10.0, format="%.2f", key="pn_rente")
            with c3:
                p_rend   = st.number_input(col_prend.lab, value=4.0, step=0.5,
                                            format="%.1f", key="pn_rend")
                p_beg    = st.date_input(col_pbeg.lab, value=date(today.year + 30, 1, 1),
                                          min_value=RENTENBEGINN_MIN, max_value=RENTENBEGINN_MAX,
                                          key="pn_beg")
                p_status = st.selectbox(col_status.lab, options=STATUS_TYPES, key="pn_status")
            p_note = st.text_input(col_note.lab, key="pn_note")
            if st.form_submit_button("💾 Speichern", width="stretch"):
                if not p_typ:
                    st.error("Typ ist ein Pflichtfeld.")
                else:
                    save_pension_plan({
                        "person": p_person or None, "name": p_name or None,
                        "typ": p_typ, "anbieter": p_anb or None,
                        "monatl_beitrag": p_mon, "aktueller_wert": p_wert,
                        "erwartete_rente": p_rente, "rendite_pct": p_rend,
                        "rentenbeginn": p_beg, "status": p_status, "note": p_note or None,
                    })
                    st.success("✅ Baustein gespeichert.")
                    st.rerun()

    # ── Bearbeitbare Liste ────────────────────────────────────────────────────
    plans_df = load_pension_plans()

    def _txt(v) -> str:
        """Wandelt NULL/NaN (DuckDB→pandas) in einen leeren String für Text-Widgets."""
        return "" if pd.isna(v) else str(v)

    def _plan_label(pid):
        row = plans_df[plans_df["plan_id"] == pid]
        if row.empty:
            return str(pid)
        row = row.iloc[0]
        label = _txt(row["name"]) or row["typ"]
        person_part = f" – {_txt(row['person'])}" if _txt(row["person"]) else ""
        anbieter_part = f" ({_txt(row['anbieter'])})" if _txt(row["anbieter"]) else ""
        return f"{label}{person_part}{anbieter_part}"

    if plans_df.empty:
        st.info("Noch keine Vorsorge-Bausteine angelegt.")
    else:
        with st.expander("✏️ Baustein bearbeiten"):
            _edit_ids = plans_df["plan_id"].tolist()
            edit_pid = st.selectbox("Baustein auswählen", options=_edit_ids,
                                     format_func=_plan_label, key="pn_edit_select")
            edit_row = plans_df[plans_df["plan_id"] == edit_pid].iloc[0]

            with st.form(f"pension_edit_{edit_pid}"):
                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    _p_opts = [""] + all_persons
                    _p_val = _txt(edit_row["person"])
                    e_person = st.selectbox(
                        col_pers.lab, options=_p_opts,
                        index=_p_opts.index(_p_val) if _p_val in _p_opts else 0,
                        key=f"pe_person_{edit_pid}",
                    )
                    e_name = st.text_input(col_pname.lab, value=_txt(edit_row["name"]),
                                            key=f"pe_name_{edit_pid}")
                    e_typ = st.selectbox(
                        col_ptyp.lab, options=PENSION_TYPES,
                        index=PENSION_TYPES.index(edit_row["typ"]) if edit_row["typ"] in PENSION_TYPES else 0,
                        key=f"pe_typ_{edit_pid}",
                    )
                    e_anb = st.text_input(col_panb.lab, value=_txt(edit_row["anbieter"]),
                                           key=f"pe_anb_{edit_pid}")
                with ec2:
                    e_mon = st.number_input(col_pmon.lab, min_value=0.0,
                                             value=float(edit_row["monatl_beitrag"]),
                                             step=10.0, format="%.2f", key=f"pe_mon_{edit_pid}")
                    e_wert = st.number_input(col_pwert.lab, min_value=0.0,
                                              value=float(edit_row["aktueller_wert"]),
                                              step=100.0, format="%.2f", key=f"pe_wert_{edit_pid}")
                    e_rente = st.number_input(col_prente.lab, min_value=0.0,
                                               value=float(edit_row["erwartete_rente"]),
                                               step=10.0, format="%.2f", key=f"pe_rente_{edit_pid}")
                with ec3:
                    e_rend = st.number_input(col_prend.lab, value=float(edit_row["rendite_pct"]),
                                              step=0.5, format="%.1f", key=f"pe_rend_{edit_pid}")
                    _beg_default = edit_row["rentenbeginn"]
                    if pd.isna(_beg_default):
                        _beg_default = date(today.year + 30, 1, 1)
                    elif isinstance(_beg_default, pd.Timestamp):
                        _beg_default = _beg_default.date()
                    e_beg = st.date_input(
                        col_pbeg.lab, value=_beg_default,
                        min_value=RENTENBEGINN_MIN, max_value=RENTENBEGINN_MAX,
                        key=f"pe_beg_{edit_pid}",
                        help="Frei wählbar – auch weit in der Zukunft oder Vergangenheit.",
                    )
                    e_status = st.selectbox(
                        col_status.lab, options=STATUS_TYPES,
                        index=STATUS_TYPES.index(edit_row["status"]) if edit_row["status"] in STATUS_TYPES else 0,
                        key=f"pe_status_{edit_pid}",
                    )
                e_note = st.text_input(col_note.lab, value=_txt(edit_row["note"]), key=f"pe_note_{edit_pid}")

                if st.form_submit_button("💾 Baustein aktualisieren", width="stretch"):
                    update_pension_plan(int(edit_pid), {
                        "person": e_person or None, "name": e_name or None,
                        "typ": e_typ, "anbieter": e_anb or None,
                        "monatl_beitrag": e_mon, "aktueller_wert": e_wert,
                        "erwartete_rente": e_rente, "rendite_pct": e_rend,
                        "rentenbeginn": e_beg, "status": e_status, "note": e_note or None,
                    })
                    st.success("✅ Baustein aktualisiert.")
                    st.rerun()

        with st.expander("🗑️ Baustein entfernen"):
            del_pid = st.selectbox("Baustein auswählen", options=[None] + plans_df["plan_id"].tolist(),
                                    format_func=lambda pid: "—" if pid is None else _plan_label(pid),
                                    key="pn_delete_select")
            if st.button("🗑️ Entfernen", disabled=del_pid is None, type="primary", key="pn_delete_btn"):
                delete_pension_plan(int(del_pid))
                st.success("✅ Baustein entfernt.")
                st.rerun()

        st.caption(f"{len(plans_df)} Einträge · Bearbeitung/Entfernen einzeln oben oder "
                   "Felder direkt in der Tabelle, anschließend mit „Speichern“ übernehmen.")

        # Anzeige-Reihenfolge explizit erzwingen (unabhängig von der physischen
        # DB-Spaltenreihenfolge, die sich bei per ALTER TABLE nachträglich
        # ergänzten Spalten wie "name" unterscheiden kann): Name direkt neben Person.
        _plan_col_order = ["plan_id", "person", "name", "typ", "anbieter", "monatl_beitrag",
                            "aktueller_wert", "erwartete_rente", "rendite_pct", "rentenbeginn",
                            "status", "note"]
        plans_display = plans_df[_plan_col_order].copy()

        edited = st.data_editor(
            plans_display.style.map(colour_amount, subset=["erwartete_rente"]).format({
                "monatl_beitrag":  "{:,.2f} €",
                "aktueller_wert":  "{:,.2f} €",
                "erwartete_rente": "{:,.2f} €",
                "rendite_pct":     "{:.1f} %",
            }),
            hide_index=True,
            num_rows="fixed",
            disabled=["plan_id"],
            column_config={
                "plan_id":         None,
                "person":          st.column_config.SelectboxColumn(col_pers.lab,
                                       options=[""] + all_persons),
                "name":            st.column_config.TextColumn(col_pname.lab),
                "typ":             st.column_config.SelectboxColumn(col_ptyp.lab,
                                       options=PENSION_TYPES),
                "anbieter":        st.column_config.TextColumn(col_panb.lab),
                "monatl_beitrag":  st.column_config.NumberColumn(col_pmon.lab,
                                       format="%.2f €", step=0.01),
                "aktueller_wert":  st.column_config.NumberColumn(col_pwert.lab,
                                       format="%.2f €", step=0.01),
                "erwartete_rente": st.column_config.NumberColumn(col_prente.lab,
                                       format="%.2f €", step=0.01),
                "rendite_pct":     st.column_config.NumberColumn(col_prend.lab,
                                       min_value=-20.0, max_value=20.0, step=0.5, format="%.1f %%"),
                "rentenbeginn":    st.column_config.DateColumn(col_pbeg.lab, format="DD.MM.YYYY",
                                       min_value=RENTENBEGINN_MIN, max_value=RENTENBEGINN_MAX),
                "status":          st.column_config.SelectboxColumn(col_status.lab,
                                       options=STATUS_TYPES),
                "note":            st.column_config.TextColumn(col_note.lab),
            },
            key="pension_editor",
            width="stretch",
        )

        # Nur die von Streamlit selbst protokollierten Edits verwenden
        # (siehe app_forecast.py – vermeidet Fehlalarme durch SelectboxColumn-Resets)
        _p_edits = st.session_state.get("pension_editor", {}).get("edited_rows", {})

        _p_updates: dict[int, dict] = {}
        for pos, changed in _p_edits.items():
            pid = int(plans_display.iloc[int(pos)]["plan_id"])
            fields = {
                c_name: changed[c_name] for c_name in (
                    "person", "name", "typ", "anbieter", "monatl_beitrag", "aktueller_wert",
                    "erwartete_rente", "rendite_pct", "rentenbeginn", "status", "note",
                ) if c_name in changed
            }
            if fields:
                _p_updates[pid] = fields

        if st.button("💾 Änderungen speichern", disabled=not _p_updates, width="stretch"):
            for pid, fields in _p_updates.items():
                update_pension_plan(pid, fields)
            st.success(f"✅ {len(_p_updates)} aktualisiert.")
            st.rerun()

    st.divider()

    # ── Sektion 2 + 3: Bausteine & Szenario sowie Netto-Annahmen je Person ──
    st.subheader("Bausteine, Szenario & Netto-Annahmen je Person")
    st.caption(
        "Legt je Person fest, welches (im Tab „🏛️ Gesetzliche Rente“ global definierte) "
        "Szenario zur Berechnung herangezogen wird, welche weiteren Vorsorge-Bausteine "
        "(betrieblich, Riester, Rürup) in die Netto-Berechnung einfließen, sowie die "
        "individuellen Annahmen zur Netto-Berechnung (Steuer, KV/PV)."
    )

    if not all_persons:
        st.info("Noch keine Personen angelegt – zuerst ein Konto im Tab „Administrieren“ hinterlegen.")
    else:
        all_scenarios = list_pension_scenarios()
        active_other_plans = load_pension_plans(active_only=True)

        for _person in all_persons:
            with st.expander(f"👤 {_person}"):
                st.markdown("###### Bausteine & Szenario")
                _person_plans = (
                    active_other_plans[
                        (active_other_plans["person"] == _person)
                        & (active_other_plans["typ"].isin(["betrieblich", "Riester", "Rürup"]))
                    ] if not active_other_plans.empty else active_other_plans
                )
                _plan_opts = _person_plans["plan_id"].tolist()
                _saved_ids = load_pension_baustein_auswahl(_person)
                _default_ids = [pid for pid in _saved_ids if pid in _plan_opts]

                _sel_plan_ids = st.multiselect(
                    "Weitere Bausteine einbeziehen (betrieblich/Riester/Rürup)",
                    options=_plan_opts, default=_default_ids,
                    format_func=_plan_label, key=f"pv_plans_{_person}",
                )
                if not _plan_opts:
                    st.caption("→ Keine aktiven betrieblichen/Riester/Rürup-Bausteine für diese Person.")

                _active_sc = get_pension_active_scenario(_person)
                _sc_opts = [None] + all_scenarios
                _sel_scenario = st.selectbox(
                    "Szenario zur gesetzlichen Rente", options=_sc_opts,
                    index=_sc_opts.index(_active_sc) if _active_sc in _sc_opts else 0,
                    format_func=lambda s: "— keins —" if s is None else s,
                    key=f"pv_scenario_{_person}",
                )
                if not all_scenarios:
                    st.caption("→ Noch keine Szenarien angelegt – im Tab „🏛️ Gesetzliche Rente“ erstellen.")

                if st.button("💾 Auswahl speichern", key=f"pv_save_sel_{_person}"):
                    save_pension_baustein_auswahl(_person, _sel_plan_ids)
                    set_pension_active_scenario(_person, _sel_scenario)
                    save_pension_last_context(person=_person)
                    st.success(f"✅ Auswahl für {_person} gespeichert.")
                    st.rerun()

                st.divider()

                st.markdown("###### 🧾 Annahmen zur Netto-Berechnung")
                _na = load_pension_netto_annahmen(_person)
                _na1, _na2, _na3 = st.columns(3)
                with _na1:
                    _na_gfb = st.number_input(
                        "Zukünftige Entwicklung Grundfreibetrag p.a. (%)",
                        value=float(_na.get("grundfreibetrag_entwicklung", 2.0)),
                        step=0.1, format="%.1f", key=f"pv_gfb_{_person}",
                        help="Fortschreibung ab dem letzten amtlichen Wert; wirkt auch auf "
                             "die Soli-Freigrenze.",
                    )
                    _na_abz = st.number_input(
                        "Sonstige Abzüge p.a. (€)", min_value=0.0,
                        value=float(_na.get("sonstige_abzuege", 0.0)),
                        step=50.0, format="%.2f", key=f"pv_abz_{_person}",
                        help="Zusätzliche, frei definierbare Abzüge; KV/PV werden rechts "
                             "automatisch berechnet.",
                    )
                with _na2:
                    _na_kvdr = st.checkbox(
                        "Gesetzlich krankenversichert als Rentner (KVdR)",
                        value=bool(_na.get("kvdr_pflichtversichert", True)), key=f"pv_kvdr_{_person}",
                        help="Nur für KVdR-Pflichtversicherte werden KV/PV-Beiträge von der "
                             "Rente abgezogen (privat Versicherte: Häkchen entfernen).",
                    )
                    _na_kvz = st.number_input(
                        "Zusatzbeitrag Krankenkasse (%)", min_value=0.0,
                        value=float(_na.get("kv_zusatzbeitrag", KV_ZUSATZBEITRAG_DURCHSCHNITT)),
                        step=0.1, format="%.1f", key=f"pv_kvz_{_person}", disabled=not _na_kvdr,
                    )
                with _na3:
                    _na_pvk = st.checkbox(
                        "Kinderlos (Pflegeversicherung-Zuschlag)",
                        value=bool(_na.get("pv_kinderlos", False)), key=f"pv_pvk_{_person}",
                        disabled=not _na_kvdr,
                        help=f"Zuschlag von {PV_KINDERLOS_ZUSCHLAG:.1f} %-Punkten für "
                             "Kinderlose ab 23 Jahren.",
                    )

                if st.button("💾 Annahmen speichern", key=f"pv_save_na_{_person}"):
                    save_pension_netto_annahmen(_person, {
                        "grundfreibetrag_entwicklung": _na_gfb,
                        "sonstige_abzuege": _na_abz,
                        "kvdr_pflichtversichert": _na_kvdr,
                        "kv_zusatzbeitrag": _na_kvz,
                        "pv_kinderlos": _na_pvk,
                    })
                    save_pension_last_context(person=_person)
                    st.success(f"✅ Netto-Annahmen für {_person} gespeichert.")
                    st.rerun()

    st.divider()

    # ── Sektion 4: Ergebnisse ────────────────────────────────────────────────
    st.subheader("Ergebnisse")
    st.caption(
        "Netto-Berechnung auf Basis der oben je Person hinterlegten Auswahl. Weitere "
        "Bausteine werden nur einbezogen, sobald ihr eigener Rentenbeginn erreicht ist – "
        "so werden eventuell versetzte Startzeiten der einzelnen Bausteine berücksichtigt."
    )

    if not all_persons:
        st.info("Noch keine Personen angelegt.")
    else:
        _last_ctx = load_pension_last_context()
        _default_person = _last_ctx.get("person") if _last_ctx.get("person") in all_persons else all_persons[0]
        nb_person = st.selectbox(
            "Person", options=all_persons,
            index=all_persons.index(_default_person), key="pv_erg_person",
        )

        nb_active_name = get_pension_active_scenario(nb_person)
        if not nb_active_name:
            st.warning(f"Für **{nb_person}** ist noch kein Szenario ausgewählt – oben festlegen.")
        else:
            nb_result, nb_params, nb_facts = _lade_gesetzliche_rente_ergebnis(nb_person, nb_active_name)
            if nb_result is None:
                st.warning(
                    f"Für **{nb_person}** fehlt das Geburtsdatum. Im Tab „🏛️ Gesetzliche "
                    "Rente“ unter „Fakten“ ergänzen."
                )
            else:
                nb_na = load_pension_netto_annahmen(nb_person)
                _sel_ids = load_pension_baustein_auswahl(nb_person)
                _other_plans = load_pension_plans(active_only=True)
                _sel_plans = (
                    _other_plans[_other_plans["plan_id"].isin(_sel_ids)]
                    if not _other_plans.empty and _sel_ids else _other_plans.iloc[0:0]
                )

                _rentenbeginn_ts = pd.Timestamp(nb_result["rentenbeginn"])
                if not _sel_plans.empty:
                    _reb = _sel_plans["rentenbeginn"].fillna(pd.Timestamp(date(9999, 12, 31)))
                    _gestartet = _sel_plans[_reb <= _rentenbeginn_ts]
                    _versetzt = _sel_plans[_reb > _rentenbeginn_ts]
                else:
                    _gestartet = _versetzt = _sel_plans

                nb_weitere = float(_gestartet["erwartete_rente"].sum()) * 12 if not _gestartet.empty else 0.0

                if not _gestartet.empty:
                    st.caption(
                        f"→ Einbezogen: {len(_gestartet)} Baustein(e) mit zusammen "
                        f"{nb_weitere:,.0f} €/Jahr ({', '.join(_gestartet['typ'])})."
                    )
                if not _versetzt.empty:
                    st.caption(
                        f"→ Noch **nicht** einbezogen, da Rentenbeginn nach dem der "
                        f"gesetzlichen Rente ({nb_result['rentenbeginn'].strftime('%d.%m.%Y')}) liegt: "
                        + ", ".join(_plan_label(pid) for pid in _versetzt["plan_id"])
                        + "."
                    )

                netto = berechne_netto_rente(
                    nb_result["rente_jaehrlich"], nb_result["rentenbeginn"].year, nb_weitere,
                    float(nb_na.get("grundfreibetrag_entwicklung", 2.0)),
                    float(nb_na.get("sonstige_abzuege", 0.0)),
                    bool(nb_na.get("kvdr_pflichtversichert", True)),
                    float(nb_na.get("kv_zusatzbeitrag", KV_ZUSATZBEITRAG_DURCHSCHNITT)),
                    bool(nb_na.get("pv_kinderlos", False)),
                )

                n1, n2, n3, n4, n5 = st.columns(5)
                with n1:
                    st.metric("Besteuerungsanteil", f"{netto['besteuerungsanteil_pct']:.1f} %", border=True)
                    st.metric("Rentenfreibetrag", f"{netto['rentenfreibetrag_eur']:,.2f} €", border=True)
                with n2:
                    st.metric("Grundfreibetrag (Rentenbeginn)", f"{netto['grundfreibetrag']:,.2f} €",
                               border=True)
                    st.metric("Zu versteuerndes Einkommen", f"{netto['zve']:,.2f} €", border=True)
                with n3:
                    st.metric("Einkommensteuer / Jahr", f"{netto['einkommensteuer_jahr']:,.2f} €",
                               border=True)
                    st.metric("Soli / Jahr", f"{netto['soli_jahr']:,.2f} €", border=True)
                with n4:
                    st.metric("Krankenversicherung / Jahr", f"{netto['kv_beitrag_jahr']:,.2f} €",
                               border=True)
                    st.metric("Pflegeversicherung / Jahr", f"{netto['pv_beitrag_jahr']:,.2f} €", border=True)
                with n5:
                    st.metric("Netto Alterseinkommen gesamt", f"{netto['netto_gesamt_monat']:,.2f} €/Mon.",
                               border=True)
                    st.metric("Alle Abzüge / Jahr", f"{netto['abgaben_jahr']:,.2f} €", border=True)

                st.metric("💶 Netto gesetzl. Rente / Mon.",
                           f"{netto['netto_gesetzliche_rente_monat']:,.2f} €",
                           f"{netto['netto_gesetzliche_rente_jahr']:,.2f} € / Jahr",
                           delta_arrow="off", border=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 2: Gesetzliche Rente
# ═══════════════════════════════════════════════════════════════════════════════
with tab_gesetzlich:
    st.subheader("Gesetzliche Rente")
    st.caption(
        "Ermittelt die gesetzliche Rente aus den Jahres-Entgelten (→ Entgeltpunkte), "
        "einer linearen Fortschreibung bis zum Rentenbeginn sowie Zugangsfaktor, "
        "Rentenartfaktor und Rentenwert – vereinfachtes Modell nach §§ 63 ff. SGB VI. "
        "Rein lokale Berechnung, keine Abfrage bei der Deutschen Rentenversicherung."
    )

    if not all_persons:
        st.info("Noch keine Personen angelegt – zuerst ein Konto im Tab „Administrieren“ hinterlegen.")
    else:
        # ═══════════════════════════════════════════════════════════════════
        # Abschnitt 1: Fakten
        # ═══════════════════════════════════════════════════════════════════
        st.markdown("### 📋 Fakten")
        st.caption(
            "Persönliche Angaben, Entgeltpunkte-Historie sowie aktuelle Angaben zu "
            "Rentenwert und Entgeltpunkten werden gemeinsam je Person gespeichert und "
            "geladen – die Personenauswahl lädt automatisch alle Inhalte dieser Person."
        )

        # ── Sektion 1 + 3: Person, Geburtsdatum, aktuelle Angaben (kompakt) ──
        g_person = st.selectbox("Person", options=all_persons, key="gr_person")
        facts = load_pension_facts(g_person)

        ensure_pension_income_years(g_person, INCOME_START_YEAR, today.year - 1)
        income_df = load_pension_income(g_person)
        _ep_summe_historie = float(income_df.apply(
            lambda r: entgeltpunkte_jahr(r["individuelles_entgelt"], r["durchschnittsentgelt"]), axis=1,
        ).sum()) if not income_df.empty else 0.0

        f1, f2, f3 = st.columns(3)
        with f1:
            g_geburtsdatum = st.date_input(
                "Geburtsdatum",
                value=date.fromisoformat(facts.get("geburtsdatum", "1980-01-01")),
                min_value=date(1940, 1, 1), max_value=today, key=f"gr_geburtsdatum_{g_person}",
            )
        with f2:
            g_rentenwert = st.number_input(
                f"Aktueller Rentenwert (€/EP, Stand {today.year})", min_value=0.0,
                value=float(facts.get("rentenwert", AKTUELLER_RENTENWERT)),
                step=0.01, format="%.2f", key=f"gr_rentenwert_{g_person}",
                help="Jahreszahl im Label ist dynamisch (aktuelles Kalenderjahr) und dient "
                     "nur der Beschriftung. Wird jährlich zum 1. Juli amtlich angepasst – "
                     f"Default orientiert sich am aktuellen Wert ({AKTUELLER_RENTENWERT:.2f} €). "
                     "Annahme zur zukünftigen Entwicklung weiter unten je Szenario.",
            )
        with f3:
            g_ep_override = st.number_input(
                "EP lt. Renteninformation (optional)", min_value=0.0,
                value=float(facts.get("ep_override") or 0.0),
                step=0.1, format="%.3f", key=f"gr_ep_override_{g_person}",
                help=f"Summe der Historie unten: {_ep_summe_historie:.3f} Punkte. Falls hier "
                     "ein Wert > 0 aus der amtlichen Renteninformation eingetragen wird, "
                     "überschreibt dieser die Summe der Historie in der Berechnung.",
            )

        _regel_j, _regel_m = regelaltersgrenze_zusatz(g_geburtsdatum.year)
        st.caption(f"→ Regelaltersgrenze: {_regel_j} Jahre, {_regel_m} Monate")

        # ── Sektion 2: Historie der Entgeltpunkte ───────────────────────────
        st.markdown("##### Historie der Entgeltpunkte")
        st.caption(
            f"Individuelles Entgelt bis zur Beitragsbemessungsgrenze je Jahr. Die Tabelle "
            f"ist ab {INCOME_START_YEAR} durchgängig mit dem statistischen Durchschnittsentgelt "
            "(Anlage 1 SGB VI) vorbelegt – „Ihr Entgelt“ auf 0 € lassen, falls in einem Jahr "
            "kein Verdienst vorlag; beide Spalten sind überschreibbar."
        )

        with st.expander("➕ Weiteres Jahr hinzufügen (z. B. vor {})".format(INCOME_START_YEAR)):
            with st.form("income_new", clear_on_submit=True):
                ic1, ic2, ic3 = st.columns(3)
                with ic1:
                    i_jahr = st.number_input(col_gjah.lab, min_value=1950, max_value=today.year,
                                              value=INCOME_START_YEAR - 1, step=1, key="gi_jahr")
                with ic2:
                    i_entgelt = st.number_input(col_gind.lab, min_value=0.0, value=0.0,
                                                 step=500.0, format="%.2f", key="gi_entgelt")
                with ic3:
                    i_durch = st.number_input(col_gdur.lab, min_value=0.0,
                                               value=durchschnittsentgelt_fuer_jahr(int(INCOME_START_YEAR - 1)),
                                               step=100.0, format="%.2f", key="gi_durch")
                if st.form_submit_button("💾 Speichern", width="stretch"):
                    save_pension_income_year(g_person, int(i_jahr), i_entgelt, i_durch)
                    save_pension_last_context(person=g_person)
                    st.success(f"✅ Jahr {int(i_jahr)} gespeichert.")
                    st.rerun()

        income_display = income_df.copy()
        income_display["entgeltpunkte"] = income_display.apply(
            lambda r: entgeltpunkte_jahr(r["individuelles_entgelt"], r["durchschnittsentgelt"]), axis=1,
        )
        income_display.insert(0, "entfernen", False)

        st.data_editor(
            income_display[["entfernen", "income_id", "jahr", "individuelles_entgelt",
                             "durchschnittsentgelt", "entgeltpunkte"]],
            hide_index=True, num_rows="fixed", disabled=["income_id", "entgeltpunkte"],
            column_config={
                "entfernen":             st.column_config.CheckboxColumn("Entfernen"),
                "income_id":             None,
                "jahr":                  st.column_config.NumberColumn(col_gjah.lab, format="%d"),
                "individuelles_entgelt": st.column_config.NumberColumn(col_gind.lab, format="%.2f €"),
                "durchschnittsentgelt":  st.column_config.NumberColumn(col_gdur.lab, format="%.2f €"),
                "entgeltpunkte":         st.column_config.NumberColumn(col_gep.lab, format="%.3f"),
            },
            key=f"income_editor_{g_person}", width="stretch",
        )

        _g_edits = st.session_state.get(f"income_editor_{g_person}", {}).get("edited_rows", {})
        g_to_delete = [
            int(income_display.iloc[int(pos)]["income_id"])
            for pos, changed in _g_edits.items() if changed.get("entfernen")
        ]
        _g_updates: dict[int, dict] = {}
        for pos, changed in _g_edits.items():
            if changed.get("entfernen"):
                continue
            iid = int(income_display.iloc[int(pos)]["income_id"])
            fields = {c: changed[c] for c in ("jahr", "individuelles_entgelt", "durchschnittsentgelt")
                      if c in changed}
            if fields:
                _g_updates[iid] = fields

        # ── Gemeinsames Speichern: Person, Historie-Änderungen und aktuelle ──
        # Angaben (Rentenwert/EP-Override) werden zusammen in einem Schritt
        # gespeichert; das Laden erfolgt automatisch über die Personenauswahl
        # oben.
        if st.button("💾 Fakten & Historie speichern", key="gr_save_facts", width="stretch"):
            for iid in g_to_delete:
                delete_pension_income(iid)
            for iid, fields in _g_updates.items():
                if iid in g_to_delete:
                    continue
                update_pension_income(iid, fields)
            save_pension_facts(g_person, {
                "geburtsdatum": g_geburtsdatum.isoformat(),
                "rentenwert": g_rentenwert,
                "ep_override": g_ep_override if g_ep_override > 0 else None,
            })
            save_pension_last_context(person=g_person)
            st.success("✅ Fakten und Entgeltpunkte-Historie gespeichert.")
            st.rerun()

        st.divider()

        # ═══════════════════════════════════════════════════════════════════
        # Abschnitt 2: Szenarien zur Rentenentwicklung
        # ═══════════════════════════════════════════════════════════════════
        st.markdown("### 🧭 Szenarien zur Rentenentwicklung")
        st.caption(
            "Szenarien sind **unabhängig von der Person** und können im Tab „📋 Verwalten“ "
            "beliebigen Personen zugeordnet werden – z. B. „Basis“ (Regelaltersrente, "
            "Rentenwert konstant) und „Frühe Rente“ (48 Monate vorgezogen, Rentenwert "
            "+1 %/Jahr). Ausnahme: „Zukünftige Entgeltpunkte pro Jahr“ ist zugleich eine "
            "persönliche Angabe und wird je Person **und** Szenario gespeichert. Annahmen "
            "zur Netto-Berechnung (Steuer, KV/PV) werden getrennt je Person im Tab "
            "„📋 Verwalten“ gepflegt."
        )

        saved_scenarios = list_pension_scenarios()
        sc_options = ["— neu —"] + saved_scenarios
        sc_selected = st.selectbox("Szenario laden", options=sc_options, key="gr_scenario_select")
        sc_params = (load_pension_scenario(sc_selected) or {}) if sc_selected in saved_scenarios else {}

        # Ø Entgeltpunkte der letzten 5 Jahre der oben gewählten Person – Vorschlagswert
        # für „Zukünftige Entgeltpunkte pro Jahr“, falls für diese Person/dieses Szenario
        # noch kein eigener Wert gespeichert ist.
        _ep_default = 0.0
        if not income_df.empty:
            _ep_tmp = income_df.copy()
            _ep_tmp["ep"] = _ep_tmp.apply(
                lambda r: entgeltpunkte_jahr(r["individuelles_entgelt"], r["durchschnittsentgelt"]), axis=1,
            )
            _ep_default = float(_ep_tmp.sort_values("jahr", ascending=False).head(5)["ep"].mean())
        _zukunft_ep_gespeichert = (
            load_pension_zukunft_ep(g_person, sc_selected) if sc_selected in saved_scenarios else None
        )

        a1, a2, a3 = st.columns(3)
        with a1:
            g_monate_abw = st.slider(
                "Renteneintritt ggü. Regelaltersgrenze (Monate)",
                min_value=-48, max_value=36, value=int(sc_params.get("monate_abweichung", 0)),
                step=1, key=f"gr_monate_abw_{sc_selected}",
                help="Negativ = früherer Rentenbeginn (Zugangsfaktor-Abschlag 0,3 %/Monat), "
                     "positiv = späterer Rentenbeginn (Zuschlag 0,5 %/Monat). Wird ignoriert, "
                     "falls rechts ein festes Erwerbsminderungs-Startdatum aktiv ist.",
            )
            _rf_options = list(RENTENFAKTOR_OPTIONS.keys())
            _rf_default = sc_params.get("rentenfaktor_label", _rf_options[0])
            g_rentenfaktor_label = st.selectbox(
                "Rentenfaktor", options=_rf_options,
                index=_rf_options.index(_rf_default) if _rf_default in _rf_options else 0,
                key=f"gr_rentenfaktor_{sc_selected}",
            )
            g_rentenfaktor = RENTENFAKTOR_OPTIONS[g_rentenfaktor_label]
        with a2:
            g_em_aktiv = st.checkbox(
                "Festes Startdatum bei Erwerbsminderung", key=f"gr_em_aktiv_{sc_selected}",
                value=bool(sc_params.get("erwerbsminderung_start")),
                help="Überschreibt den berechneten Rentenbeginn (Regelaltersgrenze ± Monate "
                     "links) direkt mit einem festen Datum. Der Zugangsfaktor wird weiterhin "
                     "aus dem Monatsabstand dieses Datums zur Regelaltersgrenze ermittelt.",
            )
            _em_default = (date.fromisoformat(sc_params["erwerbsminderung_start"])
                           if sc_params.get("erwerbsminderung_start")
                           else date(today.year, today.month, 1))
            g_em_start = st.date_input(
                "Startdatum bei Erwerbsminderung", value=_em_default,
                min_value=RENTENBEGINN_MIN, max_value=RENTENBEGINN_MAX,
                disabled=not g_em_aktiv, key=f"gr_em_start_{sc_selected}",
            )
        with a3:
            g_rentenwert_entw = st.number_input(
                "Zukünftige Entwicklung Rentenwert p.a. (%)",
                value=float(sc_params.get("rentenwert_entwicklung", 0.0)),
                step=0.1, format="%.1f", key=f"gr_rentenwert_entw_{sc_selected}",
                help="Lineare Fortschreibung; 0 % = Rentenwert bleibt konstant.",
            )
            g_zukunft_ep = st.number_input(
                "Zukünftige Entgeltpunkte pro Jahr", min_value=0.0,
                value=(float(_zukunft_ep_gespeichert) if _zukunft_ep_gespeichert is not None
                       else _ep_default),
                step=0.05, format="%.3f", key=f"gr_zukunft_ep_{g_person}_{sc_selected}",
                help=f"Persönliche Annahme von **{g_person}** für dieses Szenario – wird je "
                     f"Person **und** Szenario gespeichert, nicht global. Vorschlag: Ø der "
                     f"letzten 5 Jahre ({_ep_default:.3f}).",
            )

        sn1, sn2 = st.columns([2, 1])
        with sn1:
            sc_name_input = st.text_input(
                "Szenario-Name", value="" if sc_selected == sc_options[0] else sc_selected,
                placeholder="z. B. „Basis“ oder „Frühe Rente“", key=f"gr_scenario_name_{sc_selected}",
            )
        with sn2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("💾 Szenario speichern", key="gr_save_scenario", width="stretch"):
                _sc_name = sc_name_input.strip()
                if not _sc_name:
                    st.error("Bitte einen Namen vergeben.")
                else:
                    save_pension_scenario(_sc_name, {
                        "monate_abweichung": g_monate_abw,
                        "rentenfaktor_label": g_rentenfaktor_label,
                        "rentenwert_entwicklung": g_rentenwert_entw,
                        "erwerbsminderung_start": g_em_start.isoformat() if g_em_aktiv else None,
                    })
                    save_pension_zukunft_ep(g_person, _sc_name, g_zukunft_ep)
                    save_pension_last_context(person=g_person, scenario=_sc_name)
                    st.success(f"✅ Szenario „{_sc_name}“ gespeichert.")
                    st.rerun()

        if saved_scenarios:
            with st.expander("🗑️ Szenario löschen"):
                del_sc = st.selectbox("Szenario", options=[None] + saved_scenarios,
                                       format_func=lambda s: "—" if s is None else s, key="gr_del_sc")
                if st.button("Löschen", disabled=del_sc is None, type="primary", key="gr_del_sc_btn"):
                    delete_pension_scenario(del_sc)
                    st.success(f"✅ Szenario „{del_sc}“ gelöscht.")
                    st.rerun()

        st.divider()

        # ═══════════════════════════════════════════════════════════════════
        # Abschnitt 3: Ergebnisse
        # ═══════════════════════════════════════════════════════════════════
        st.markdown("### 📊 Ergebnisse")

        # ── Sektion 1: Auswahl Person & Szenario ────────────────────────────
        _last_ctx = load_pension_last_context()
        _default_erg_person = (
            _last_ctx.get("person") if _last_ctx.get("person") in all_persons else all_persons[0]
        )
        _default_erg_scenario = (
            _last_ctx.get("scenario") if _last_ctx.get("scenario") in saved_scenarios
            else (saved_scenarios[0] if saved_scenarios else None)
        )

        e1, e2 = st.columns(2)
        with e1:
            erg_person = st.selectbox(
                "Person", options=all_persons,
                index=all_persons.index(_default_erg_person), key="gr_erg_person",
            )
        with e2:
            if not saved_scenarios:
                st.info("Noch keine Szenarien angelegt.")
                erg_scenario = None
            else:
                erg_scenario = st.selectbox(
                    "Szenario", options=saved_scenarios,
                    index=saved_scenarios.index(_default_erg_scenario)
                          if _default_erg_scenario in saved_scenarios else 0,
                    key="gr_erg_scenario",
                )

        if erg_scenario is not None:
            erg_result, erg_params, erg_facts = _lade_gesetzliche_rente_ergebnis(erg_person, erg_scenario)
            if erg_result is None:
                st.warning(f"Für **{erg_person}** fehlt das Geburtsdatum – oben unter „Fakten“ ergänzen.")
            else:
                erg_rentenwert = float(erg_facts.get("rentenwert", AKTUELLER_RENTENWERT))

                # ── Sektion 2: Berechnungsergebnisse als Karten ─────────────
                r1, r2, r3, r4 = st.columns(4)
                with r1:
                    st.metric("Entgeltpunkte bisher", f"{erg_result['summe_ep_bisher']:.3f}", border=True,
                               help="Quelle: Renteninformation" if erg_result["ep_bisher_quelle"] == "renteninformation"
                                    else "Quelle: Summe der Historie")
                with r2:
                    st.metric("Ø Entgeltpunkte (letzte 5 J.)", f"{erg_result['ep_letzte5_avg']:.3f}", border=True)
                with r3:
                    st.metric("Regelaltersgrenze", erg_result["regelaltersgrenze_datum"].strftime("%d.%m.%Y"),
                               border=True)
                with r4:
                    st.metric("Aktueller Rentenwert", f"{erg_rentenwert:.2f} €", border=True)

                r5, r6, r7, r8 = st.columns(4)
                with r5:
                    st.metric("Zukünftige Entgeltpunkte", f"{erg_result['zukuenftige_ep']:.3f}",
                               f"über {erg_result['zukunftsjahre']:.1f} Jahre", delta_arrow="off", border=True)
                with r6:
                    st.metric("Annahme: zukünftige EP/Jahr",
                               f"{float(erg_params.get('zukunft_ep', 0.0)):.3f}", border=True)
                with r7:
                    st.metric("Rentenbeginn", erg_result["rentenbeginn"].strftime("%d.%m.%Y"),
                               f"Zugangsfaktor {erg_result['zugangsfaktor']:.3f}", delta_arrow="off", border=True)
                with r8:
                    st.metric("Rentenwert bei Rentenbeginn", f"{erg_result['rentenwert_bei_beginn']:.2f} €",
                               border=True)

                r9, r10 = st.columns(2)
                with r9:
                    st.metric("Summe Entgeltpunkte", f"{erg_result['gesamt_ep']:.3f}", border=True)
                with r10:
                    st.metric("💶 Bruttorente", f"{erg_result['rente_monatlich']:,.2f} €/Mon.",
                               f"{erg_result['rente_jaehrlich']:,.2f} € / Jahr", delta_arrow="off", border=True)

                # ── Sektion 3: Grafik zu den Annahmen ───────────────────────
                st.markdown("###### Entwicklung der Annahmen bis Rentenbeginn")
                _jahre = list(range(erg_result["letztes_jahr"], erg_result["rentenbeginn"].year + 1))
                if len(_jahre) >= 2:
                    _rate = float(erg_params.get("zukunft_ep", 0.0))
                    _entw = float(erg_params.get("rentenwert_entwicklung", 0.0))
                    _ep_kum = [
                        erg_result["summe_ep_bisher"] + _rate
                        * max(0.0, (date(j, 12, 31) - date(erg_result["letztes_jahr"], 12, 31)).days / 365.25)
                        for j in _jahre
                    ]
                    _rw_verlauf = [
                        erg_rentenwert * (1 + _entw / 100) ** max(0.0, j - today.year)
                        for j in _jahre
                    ]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=_jahre, y=_ep_kum, name="Entgeltpunkte (kumuliert)",
                                              line=dict(color=C["blue"])))
                    fig.add_trace(go.Scatter(x=_jahre, y=_rw_verlauf, name="Rentenwert (€/EP)",
                                              yaxis="y2", line=dict(color=C["green"])))
                    fig.update_layout(
                        **PLOTLY_THEME, height=CHART_HEIGHT,
                        xaxis_title="Jahr",
                        yaxis=dict(title="Entgeltpunkte"),
                        yaxis2=dict(title="Rentenwert (€)", overlaying="y", side="right"),
                        legend=dict(orientation="h", y=1.15),
                    )
                    st.plotly_chart(fig, config=PLOTLY_CONFIG, width="stretch")
                else:
                    st.caption("Zu wenige Jahre für eine Verlaufsgrafik.")


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 3: Kapital-Prognose
# ═══════════════════════════════════════════════════════════════════════════════
with tab_forecast:
    active_plans = load_pension_plans(active_only=True)

    if active_plans.empty:
        st.info("Keine aktiven Vorsorge-Bausteine – im Tab „Verwalten“ anlegen.")
    else:
        c1, c2 = st.columns([1, 3])
        with c1:
            _default_horizon = int(max(
                [_years_to(d) for d in active_plans["rentenbeginn"]] + [10]
            ))
            horizon = st.slider("Prognosehorizont (Jahre)", min_value=1, max_value=50,
                                 value=min(_default_horizon, 50))
            st.caption(
                "Vereinfachtes Modell: Beiträge laufen je Baustein bis zu seinem "
                "individuellen Rentenbeginn, danach wird der Wert zum Zeitpunkt des "
                "Renteneintritts fortgeschrieben."
            )

        # ── KPIs ─────────────────────────────────────────────────────────────
        total_wert  = active_plans["aktueller_wert"].sum()
        total_mon   = active_plans["monatl_beitrag"].sum()
        total_rente = active_plans["erwartete_rente"].sum()
        kapital_bei_renteneintritt = sum(
            project_capital(r["aktueller_wert"], r["monatl_beitrag"],
                             _years_to(r["rentenbeginn"]), r["rendite_pct"])
            for _, r in active_plans.iterrows()
        )

        k1, k2, k3, k4 = st.columns(4)
        with k1:
            st.metric("Kapital heute", f"{total_wert:,.0f} €", border=True)
        with k2:
            st.metric("Ø monatl. Beitrag", f"{total_mon:,.0f} €", border=True)
        with k3:
            st.metric("Kapital bei Rentenbeginn", f"{kapital_bei_renteneintritt:,.0f} €",
                       f"{kapital_bei_renteneintritt - total_wert:+,.0f} €",
                       delta_arrow="off", border=True)
        with k4:
            st.metric("Erw. Rente gesamt/Mon.", f"{total_rente:,.0f} €", border=True)

        # ── Chart: Kapitalverlauf ────────────────────────────────────────────
        years_axis = list(range(0, horizon + 1))
        rows = []
        for y in years_axis:
            for _, r in active_plans.iterrows():
                eff_years = min(float(y), _years_to(r["rentenbeginn"]))
                fv = project_capital(r["aktueller_wert"], r["monatl_beitrag"],
                                      eff_years, r["rendite_pct"])
                rows.append({"jahr": today.year + y, "typ": r["typ"], "wert": fv})
        proj_df = pd.DataFrame(rows)
        proj_total = proj_df.groupby("jahr", as_index=False)["wert"].sum()

        fig = px.area(proj_df, x="jahr", y="wert", color="typ",
                       title="Kapitalverlauf nach Baustein-Typ")
        fig.add_trace(go.Scatter(x=proj_total["jahr"], y=proj_total["wert"],
                                  mode="lines", name="Gesamt",
                                  line=dict(color=C["text"], width=2, dash="dot")))
        fig.update_layout(**PLOTLY_THEME, height=CHART_HEIGHT,
                           yaxis_title="Kapital (€)", xaxis_title=None)
        st.plotly_chart(fig, config=PLOTLY_CONFIG, width="stretch")

        # ── Tabelle je Baustein ──────────────────────────────────────────────
        with st.expander("Details je Baustein"):
            detail = active_plans.copy()
            detail["jahre_bis_renteneintritt"] = detail["rentenbeginn"].apply(_years_to).round(1)
            detail["kapital_bei_renteneintritt"] = detail.apply(
                lambda r: project_capital(r["aktueller_wert"], r["monatl_beitrag"],
                                           r["jahre_bis_renteneintritt"], r["rendite_pct"]),
                axis=1,
            )
            st.dataframe(
                detail[["typ", "anbieter", "aktueller_wert", "monatl_beitrag",
                        "rendite_pct", "jahre_bis_renteneintritt", "kapital_bei_renteneintritt"]]
                .style.format({
                    "aktueller_wert": "{:,.0f} €", "monatl_beitrag": "{:,.0f} €",
                    "rendite_pct": "{:.1f} %", "jahre_bis_renteneintritt": "{:.1f}",
                    "kapital_bei_renteneintritt": "{:,.0f} €",
                }),
                hide_index=True, width="stretch",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 4: Rentenlücke
# ═══════════════════════════════════════════════════════════════════════════════
with tab_gap:
    active_plans = load_pension_plans(active_only=True)
    st.subheader("Rentenlücken-Rechner")
    st.caption(
        "Vergleicht das gewünschte monatliche Alterseinkommen mit der Summe der "
        "erwarteten Renten zzgl. einer angenommenen Kapitalentnahme."
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        wunsch = st.number_input("Gewünschtes Alterseinkommen (€/Mon.)",
                                  min_value=0.0, value=2500.0, step=50.0)
    with c2:
        entnahme_pct = st.number_input("Entnahmerate Kapital p.a. (%)",
                                        min_value=0.0, max_value=20.0, value=4.0, step=0.5,
                                        help="Faustregel „4 %-Regel“: jährlich entnehmbarer "
                                             "Anteil des Kapitals ohne es aufzuzehren.")
    with c3:
        rend_annahme = st.number_input("Angenommene Rendite für Zusatzsparen p.a. (%)",
                                        min_value=0.0, max_value=20.0, value=5.0, step=0.5)

    if active_plans.empty:
        st.info("Keine aktiven Vorsorge-Bausteine – im Tab „Verwalten“ anlegen.")
    else:
        summe_rente = active_plans["erwartete_rente"].sum()
        kapital_bei_renteneintritt = sum(
            project_capital(r["aktueller_wert"], r["monatl_beitrag"],
                             _years_to(r["rentenbeginn"]), r["rendite_pct"])
            for _, r in active_plans.iterrows()
        )
        kapitalentnahme_mon = kapital_bei_renteneintritt * entnahme_pct / 100 / 12
        gesamt_erwartet = summe_rente + kapitalentnahme_mon
        luecke = wunsch - gesamt_erwartet

        k1, k2, k3 = st.columns(3)
        with k1:
            st.metric("Erwartete Rente (Bausteine)", f"{summe_rente:,.0f} €")
        with k2:
            st.metric("+ Kapitalentnahme", f"{kapitalentnahme_mon:,.0f} €")
        with k3:
            color = "red" if luecke > 0 else "green"
            label = "Lücke" if luecke > 0 else "Überschuss"
            st.metric(label, f":{color}[{abs(luecke):,.0f} €]", border=True)

        fig = go.Figure(go.Bar(
            x=["Gewünscht", "Gesetzlich/privat", "Kapitalentnahme"],
            y=[wunsch, summe_rente, kapitalentnahme_mon],
            marker_color=[C["blue"], C["green"], C["amber"]],
        ))
        fig.update_layout(**PLOTLY_THEME, height=320, yaxis_title="€ / Monat")
        st.plotly_chart(fig, config=PLOTLY_CONFIG, width="stretch")

        if luecke > 0:
            _min_years = min(
                [_years_to(r["rentenbeginn"]) for _, r in active_plans.iterrows()
                 if _years_to(r["rentenbeginn"]) > 0] or [0]
            )
            if _min_years > 0:
                zusatzkapital_noetig = luecke * 12 / (entnahme_pct / 100) if entnahme_pct else 0
                zusatz_sparrate = required_monthly_saving(
                    zusatzkapital_noetig, 0.0, _min_years, rend_annahme
                )
                st.warning(
                    f"Um die Lücke von **{luecke:,.0f} €/Mon.** bis zum ersten "
                    f"Rentenbeginn (in {_min_years:.1f} Jahren) über zusätzliches "
                    f"Kapital zu schließen, wären ca. **{zusatz_sparrate:,.0f} €/Mon.** "
                    f"zusätzliche Sparrate bei {rend_annahme:.1f} % Rendite p.a. nötig."
                )
        else:
            st.success("Das erwartete Alterseinkommen deckt den gewünschten Betrag.")
