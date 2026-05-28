"""
app_dashboard.py
Analyse-Dashboard: KPIs, Charts, gefilterte Transaktionsübersicht.
Daten werden kontenübergreifend zusammengeführt und interaktiv filterbar dargestellt.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from datetime import date

from app_functions import (
    require_master_password,
    list_saved_users,
    load_categories,
    safe_table_name,
    colour_amount,
    get_config,
    inc_filter,
    exc_filter,
    MONTH_NAMES,
    con,
    col_ctx, col_grp, col_cat, col_rel, col_amt, col_app,
    col_loc, col_dat, col_mon, col_yea,
    col_inf, col_add, col_new, col_rid, col_sld, col_spc, col_note,
)

# ── Sicherheits-Gate ──────────────────────────────────────────────────────────
require_master_password()

st.title("📊 Analysieren")

if "chart_filter" not in st.session_state:
    st.session_state["chart_filter"] = {}


# ── Design-System ─────────────────────────────────────────────────────────────
C = {
    "bg":      "#0D0F14",
    "surface": "#161920",
    "border":  "#252830",
    "text":    "#E8EAF0",
    "muted":   "#6B7280",
    "green":   "#00E5A0",
    "red":     "#FF4D6A",
    "blue":    "#4D9FFF",
    "amber":   "#FFB547",
    "purple":  "#A78BFA",
}

CHART_H = 420

PLOTLY_THEME = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="'Noto Sans', monospace", color=C["text"], size=11),
    margin=dict(l=8, r=8, t=36, b=8),
    colorway=[C["blue"], C["green"], C["amber"], C["purple"], C["red"],
              "#38BDF8", "#F472B6", "#34D399"],
)


# ── Konto-Auswahl ─────────────────────────────────────────────────────────────
# Ebene 1: Schnellauswahl per Button (nur Giro / alle)
# Ebene 2: Checkboxen je Konto – werden durch Ebene 1 vorbelegt,
#          können aber manuell angepasst werden.

accounts  = list_saved_users()
giro_df  = accounts[accounts["Konto"].str.lower() == "giro"]
other_df = accounts[accounts["Konto"].str.lower() != "giro"]
giro_ibans = giro_df["IBAN"].tolist()

with st.expander("🏦 Konten auswählen"):
    col_btns, col_giro, col_other = st.columns([1, 2, 2])

    with col_btns:
        if st.button("Nur Giro", width="stretch"):
            for _, row in accounts.iterrows():
                st.session_state[f"cb_{row['IBAN']}"] = row["IBAN"] in giro_ibans
        if st.button("Alle Konten", width="stretch"):
            for _, row in accounts.iterrows():
                st.session_state[f"cb_{row['IBAN']}"] = True

    selected_ibans: list[str] = []

    with col_giro:
        for _, row in giro_df.iterrows():
            key = f"cb_{row['IBAN']}"
            st.session_state.setdefault(key, True)
            if st.checkbox(f"{row['Person']} · {row['Bank']}", key=key):
                selected_ibans.append(row["IBAN"])

    with col_other:
        for _, row in other_df.iterrows():
            key = f"cb_{row['IBAN']}"
            st.session_state.setdefault(key, False)
            if st.checkbox(f"{row['Person']} · {row['Bank']} · {row['Konto']}", key=key):
                selected_ibans.append(row["IBAN"])

if not selected_ibans:
    st.info("Bitte mindestens ein Konto auswählen.")
    st.stop()

acc_filtered = accounts[accounts["IBAN"].isin(selected_ibans)].reset_index(drop=True)


# ── Daten aus allen gewählten Konten laden ────────────────────────────────────
# Für jede IBAN: neueste Saldo-Zeile pro Datum (verhindert doppelte Einträge nach Re-Imports)

LOAD_COLS = [col_rid, col_yea, col_mon, col_app, col_loc, col_amt,
             col_inf, col_add, col_grp, col_cat, col_ctx, col_rel,
             col_new, col_dat, col_sld, col_spc, col_note]
OTHER_COLS = [c for c in LOAD_COLS if c not in (col_dat, col_sld)]

frames = []
for iban in acc_filtered["IBAN"]:
    try:
        safe_iban = safe_table_name(iban)
    except ValueError:
        st.warning(f"Konto {iban} nicht in Datenbank gefunden – übersprungen.")
        continue
    df_iban = con.execute(f"""
        SELECT
            "{col_dat.col}",
            "{col_sld.col}",
            {", ".join(f'"{c.col}"' for c in OTHER_COLS)},
            '{safe_iban}' AS iban
        FROM "{safe_iban}"
        ORDER BY "{col_dat.col}"
    """).df()
    frames.append(df_iban)

df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

if df.empty:
    st.warning("Keine Transaktionsdaten vorhanden.")
    st.stop()


# ── Kategorien auffüllen und Sortierlisten ────────────────────────────────────

for c in [col_grp, col_cat, col_ctx, col_rel]:
    df[c.col] = df[c.col].fillna("Sonstiges")

# col_new und col_spc: NULL bedeutet "alter Datensatz" → als False behandeln
df[col_new.col] = df[col_new.col].fillna(False)
df[col_spc.col] = df[col_spc.col].fillna(False)

# ── Gruppen/Kategorien aus dedizierter Tabelle ────────────────────────────────
cat_df   = load_categories()
all_grps = sorted(cat_df[col_grp.col].unique().tolist(), key=str.lower)
all_cats = sorted(cat_df[col_cat.col].unique().tolist(), key=str.lower)

ctxs = sorted(df[col_ctx.col].unique(), key=str.lower)
yeas = sorted(df[col_yea.col].unique())
rels = sorted(df[col_rel.col].unique(), key=str.lower)

def_flr_rel = "Familie" if "Familie" in df[col_rel.col].unique() else None
def_exc_ctx = "Arbeit"  if "Arbeit"  in df[col_ctx.col].unique() else None


_today_ym = date.today().strftime("%Y-%m")

# Ungefilterte Rohdaten für den Saldenverlauf sichern.
df_raw = df.copy()
df["year_month"] = df[col_yea.col].astype(str) + "-" + df[col_mon.col].astype(str).str.zfill(2)
_ym_opts = [d.strftime("%Y-%m") for d in pd.date_range(f"{df['year_month'].min()}-01", f"{_today_ym}-01", freq="MS")]


# ── Filter-UI ─────────────────────────────────────────────────────────────────
with st.expander("🔍 Filter"):
    _today_dt = pd.Timestamp(date.today())
    _def_e = min(_today_dt.strftime("%Y-%m"), _ym_opts[-1])
    _def_s = max((_today_dt - pd.DateOffset(months=11)).strftime("%Y-%m"), _ym_opts[0])
    _ym_range = st.select_slider(
        "Zeitraum",
        options=_ym_opts,
        value=(_def_s, _def_e),
        format_func=lambda ym: f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}",
        key="sl_ym",
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _sel_cat = st.session_state.get("flr_cat", [])
        _grp_opts = sorted(
            cat_df[cat_df[col_cat.col].isin(_sel_cat)][col_grp.col].unique().tolist(), key=str.lower
        ) if _sel_cat else all_grps
        flr_grp = st.multiselect(col_grp.lab, _grp_opts, key="flr_grp")
        _sel_grp = st.session_state.get("flr_grp", [])
        _cat_opts = sorted(
            cat_df[cat_df[col_grp.col].isin(_sel_grp)][col_cat.col].unique().tolist(), key=str.lower
        ) if _sel_grp else all_cats
        flr_cat = st.multiselect(col_cat.lab, _cat_opts, key="flr_cat")
        flr_new = st.multiselect(col_new.lab, [True, False], key="flr_new")
    with c2:
        flr_cnt = st.multiselect(col_ctx.lab, ctxs, key="flr_cnt")
        flr_rel = st.multiselect(col_rel.lab, rels, default=def_flr_rel, key="flr_rel")
        flr_spc = st.multiselect(col_spc.lab, [True, False], key="flr_spc")
    with c3:
        src_col1 = st.selectbox("Suchfeld 1", [col_app.col, col_inf.col, col_add.col])
        src_txt1 = st.text_input("Text 1")
    with c4:
        src_col2 = st.selectbox("Suchfeld 2", [col_app.col, col_inf.col, col_add.col])
        src_txt2 = st.text_input("Text 2")

with st.expander("🚫 Ausschließen"):
    c1, c2 = st.columns(2)
    with c1:
        _exc_cat_sel = st.session_state.get("exc_cat", [])
        _exc_grp_opts = sorted(
            cat_df[cat_df[col_cat.col].isin(_exc_cat_sel)][col_grp.col].unique().tolist(), key=str.lower
        ) if _exc_cat_sel else all_grps
        exc_grp = st.multiselect(col_grp.lab, _exc_grp_opts, key="exc_grp")
        _exc_grp_sel = st.session_state.get("exc_grp", [])
        _exc_cat_opts = sorted(
            cat_df[cat_df[col_grp.col].isin(_exc_grp_sel)][col_cat.col].unique().tolist(), key=str.lower
        ) if _exc_grp_sel else all_cats
        exc_cat = st.multiselect(col_cat.lab, _exc_cat_opts, key="exc_cat")
        exc_new = st.multiselect(col_new.lab, [True, False], key="exc_new")
    with c2:
        exc_cnt = st.multiselect(col_ctx.lab, ctxs, default=def_exc_ctx, key="exc_cnt")
        exc_rel = st.multiselect(col_rel.lab, rels, key="exc_rel")
        exc_spc = st.multiselect(col_spc.lab, [True, False], default=True, key="exc_spc")


# ── Filter anwenden ───────────────────────────────────────────────────────────
df = df[
    inc_filter(df[col_grp.col], flr_grp) & exc_filter(df[col_grp.col], exc_grp) &
    inc_filter(df[col_new.col], flr_new) & exc_filter(df[col_new.col], exc_new) &
    inc_filter(df[col_cat.col], flr_cat) & exc_filter(df[col_cat.col], exc_cat) &
    (df["year_month"] >= _ym_range[0]) & (df["year_month"] <= _ym_range[1]) &
    inc_filter(df[col_ctx.col], flr_cnt) & exc_filter(df[col_ctx.col], exc_cnt) &
    inc_filter(df[col_rel.col], flr_rel) & exc_filter(df[col_rel.col], exc_rel) &
    inc_filter(df[col_spc.col], flr_spc) & exc_filter(df[col_spc.col], exc_spc) &
    df[src_col1].fillna("").str.contains(src_txt1, case=False, regex=False) &
    df[src_col2].fillna("").str.contains(src_txt2, case=False, regex=False)
]

# Zeilen ohne Betrag können nicht zugeordnet werden – explizit ausschließen
df = df[df[col_amt.col].notna()]

# Chart-Filter (gesetzt durch Klick auf einen Chart)
_cf = st.session_state["chart_filter"]
if _cf.get("group"):      df = df[df[col_grp.col]    == _cf["group"]]
if _cf.get("category"):   df = df[df[col_cat.col]    == _cf["category"]]
if _cf.get("applicant"):  df = df[df[col_app.col]    == _cf["applicant"]]
if _cf.get("year_month"): df = df[df["year_month"]   == _cf["year_month"]]
if _cf.get("context"):    df = df[df[col_ctx.col]    == _cf["context"]]
if _cf.get("relation"):   df = df[df[col_rel.col]    == _cf["relation"]]

income_df  = df[df[col_amt.col] > 0]
expense_df = df[df[col_amt.col] < 0]


# ── Vollständige Monatsliste aus Filter-Eingaben ─────────────────────────────
_today_ts = pd.Timestamp(date.today()).normalize()
_ms_start = pd.Timestamp(f"{_ym_range[0]}-01")
_ms_end   = min(pd.Timestamp(f"{_ym_range[1]}-01"), _today_ts.replace(day=1))
if _ms_start > _ms_end:
    _ms_end = _ms_start
all_year_months = [d.strftime("%Y-%m") for d in pd.date_range(_ms_start, _ms_end, freq="MS")]


# ── Kontostand am Ende des gewählten Zeitraums ───────────────────────────────
_end_date = min(
    pd.Timestamp(f"{_ym_range[1]}-01") + pd.offsets.MonthEnd(0),
    pd.Timestamp(date.today()).normalize(),
)
_df_raw_ts = pd.to_datetime(df_raw[col_dat.col]).dt.normalize()
end_saldo = 0.0
for _iban in selected_ibans:
    _sub = (
        df_raw[(df_raw["iban"] == _iban) & (_df_raw_ts <= _end_date)]
        [[col_dat.col, col_sld.col]]
        .dropna(subset=[col_sld.col])
    )
    if not _sub.empty:
        end_saldo += float(_sub.sort_values(col_dat.col).iloc[-1][col_sld.col])
_end_label = f"{MONTH_NAMES[int(_ym_range[1][5:])]} {_ym_range[1][:4]}"


# ── Chart-Filter Hilfsfunktion & Anzeige ─────────────────────────────────────

def _pt(ev) -> dict:
    """Ersten selektierten Punkt eines Plotly-Events sicher extrahieren."""
    try:
        pts = ev.selection.points
        return pts[0] if pts else {}
    except (AttributeError, IndexError, KeyError, TypeError):
        return {}

def _set_cf(new_cf: dict) -> None:
    if new_cf != st.session_state["chart_filter"]:
        st.session_state["chart_filter"] = new_cf
        st.rerun()

_cf = st.session_state["chart_filter"]
if any(_cf.values()):
    _parts = []
    if _cf.get("group"):      _parts.append(f"Gruppe: **{_cf['group']}**")
    if _cf.get("category"):   _parts.append(f"Kategorie: **{_cf['category']}**")
    if _cf.get("applicant"):  _parts.append(f"Empfänger: **{_cf['applicant']}**")
    if _cf.get("year_month"): _parts.append(f"Monat: **{_cf['year_month']}**")
    if _cf.get("context"):    _parts.append(f"Kontext: **{_cf['context']}**")
    if _cf.get("relation"):   _parts.append(f"Beziehung: **{_cf['relation']}**")
    _ci, _cb = st.columns([9, 1])
    _ci.info("🎯 Chart-Filter: " + " · ".join(_parts))
    if _cb.button("✕", key="reset_cf", help="Chart-Filter zurücksetzen"):
        st.session_state["chart_filter"] = {}
        st.rerun()

# ── KPI-Karten ────────────────────────────────────────────────────────────────

total_income  = income_df[col_amt.col].sum()
total_expense = expense_df[col_amt.col].sum()
net_balance   = total_income + total_expense
savings_rate  = (net_balance / total_income * 100) if total_income else 0
avg_monthly_expense = total_expense / len(all_year_months) if all_year_months else 0
avg_monthly_income  = total_income  / len(all_year_months) if all_year_months else 0

c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
with c1:
    st.metric("Einnahmen", f":green[{total_income:,.0f} €]",
              f"{len(income_df)} Transaktionen", delta_arrow="off", delta_color="off", border=True)
with c2:
    st.metric("Ausgaben", f":red[{total_expense:,.0f} €]",
              f"{len(expense_df)} Transaktionen", delta_arrow="off", delta_color="off", border=True)
with c3:
    color = "green" if net_balance >= 0 else "red"
    label = "Überschuss" if net_balance >= 0 else "Defizit"
    st.metric("Saldo", f":{color}[{net_balance:,.0f} €]", label,
              delta_arrow="off", delta_color=color, border=True)
with c4:
    color = "green" if savings_rate >= 0 else "red"
    st.metric("Sparrate", f":{color}[{savings_rate:.0f}%]", "der Einnahmen",
              delta_arrow="off", delta_color="off", border=True)
with c5:
    st.metric("Ø Monatl. Einnahmen", f":green[{avg_monthly_income:,.0f} €]",
              "Durchschnitt", delta_arrow="off", delta_color="off", border=True)
with c6:
    st.metric("Ø Monatl. Ausgaben", f":red[{abs(avg_monthly_expense):,.0f} €]",
              "Durchschnitt", delta_arrow="off", delta_color="off", border=True)
with c7:
    color = "green" if end_saldo >= 0 else "red"
    st.metric(f"Kontostand {_end_label}", f":{color}[{end_saldo:,.0f} €]",
              "alle gewählten Konten", delta_arrow="off", delta_color="off", border=True)


# ── Chart 1: Monatlicher Saldo (Bar + Rolling Average)  |  Kontext × Beziehung ─

st.divider()
_col_saldo, _col_matrix = st.columns([3, 2], gap="medium")

with _col_saldo:
    monthly = (
        df.groupby("year_month")[col_amt.col].sum()
        .reindex(all_year_months, fill_value=0)
        .reset_index()
    )
    monthly["color"] = monthly[col_amt.col].apply(lambda x: C["green"] if x >= 0 else C["red"])

    fig_cf = go.Figure()
    fig_cf.add_bar(
        x=monthly["year_month"], y=monthly[col_amt.col],
        marker_color=monthly["color"], name="Saldo",
        hovertemplate="<b>%{x}</b><br>%{y:,.0f} €<extra></extra>",
        text=monthly[col_amt.col], texttemplate="%{text:,.0f} €", textposition="auto",
    )
    fig_cf.add_scatter(
        x=monthly["year_month"],
        y=monthly[col_amt.col].rolling(3, min_periods=1).mean(),
        mode="lines", line=dict(color=C["blue"], width=2, dash="dot"), name="Ø 3M",
    )
    fig_cf.add_hline(y=0, line_color=C["border"], line_width=1)
    fig_cf.update_layout(**PLOTLY_THEME, height=CHART_H, title="Monatlicher Saldo",
                         legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1))
    fig_cf.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
    fig_cf.update_yaxes(showgrid=True, gridcolor=C["border"], zeroline=False,
                        ticksuffix=" €", tickformat=",.0f")
    ev_cf = st.plotly_chart(fig_cf, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key="chart_cf")
    pt = _pt(ev_cf)
    if pt:
        ym = pt.get("x", "")[:7]  # normalisiert "2025-07-01" → "2025-07"
        if ym:
            _set_cf({**st.session_state["chart_filter"], "year_month": ym})

with _col_matrix:
    _cr = (
        df.groupby([col_ctx.col, col_rel.col])[col_amt.col]
        .sum().reset_index()
    )
    if _cr.empty:
        st.info("Keine Daten für die Kontext × Beziehung-Matrix.")
    else:
        _rel_vals = sorted(_cr[col_rel.col].unique(), key=str.lower)
        _bar_colors = [C["blue"], C["green"], C["amber"], C["purple"], C["red"],
                       "#38BDF8", "#F472B6", "#34D399"]

        fig_cr = go.Figure()
        for i, _rel in enumerate(_rel_vals):
            _sub = _cr[_cr[col_rel.col] == _rel]
            fig_cr.add_bar(
                x=_sub[col_ctx.col],
                y=_sub[col_amt.col],
                name=_rel,
                marker_color=_bar_colors[i % len(_bar_colors)],
                customdata=[[_rel]] * len(_sub),
                hovertemplate=f"<b>%{{x}} · {_rel}</b><br>%{{y:,.0f}} €<extra></extra>",
            )

        fig_cr.update_layout(
            **PLOTLY_THEME, height=CHART_H,
            title="Saldo: Kontext × Beziehung",
            barmode="group",
            legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1),
        )
        fig_cr.update_xaxes(showgrid=False, tickangle=25)
        fig_cr.update_yaxes(showgrid=True, gridcolor=C["border"],
                            ticksuffix=" €", tickformat=",.0f")
        fig_cr.add_hline(y=0, line_color=C["border"], line_width=1)
        ev_cr = st.plotly_chart(fig_cr, width='stretch', config={"displayModeBar": False},
                                on_select="rerun", key="chart_cr")
        pt = _pt(ev_cr)
        if pt:
            ctx_val = pt.get("x", "")
            rel_val = pt.get("customdata", [None])[0]
            if ctx_val or rel_val:
                _set_cf({**st.session_state["chart_filter"],
                         "context": ctx_val or None,
                         "relation": rel_val or None})


# ── Chart 2: Einnahmen vs. Ausgaben  |  Sunburst Ausgaben ────────────────────

st.divider()
c1, c2 = st.columns([3, 2], gap="medium")

with c1:
    inc_m  = income_df.groupby("year_month")[col_amt.col].sum().reset_index(name="income")
    exp_m  = expense_df.groupby("year_month")[col_amt.col].sum().apply(abs).reset_index(name="expense")
    merged = (
        pd.DataFrame({"year_month": all_year_months})
        .merge(inc_m, on="year_month", how="left")
        .merge(exp_m, on="year_month", how="left")
        .fillna(0)
    )

    fig_ie = go.Figure()
    for series, color, label in [
        ("income",  C["green"], "Einnahmen"),
        ("expense", C["red"],   "Ausgaben"),
    ]:
        fig_ie.add_scatter(
            x=merged["year_month"], y=merged[series],
            mode="lines+markers", name=label,
            line=dict(color=color, width=2), marker=dict(size=4),
            fill="tozeroy",
            fillcolor=f"rgba({','.join(str(int(color.lstrip('#')[i:i+2], 16)) for i in (0,2,4))},0.07)",
            hovertemplate=f"<b>{label}<br>%{{x}}</b><br>%{{y:,.0f}} €<extra></extra>",
        )
    fig_ie.update_layout(**PLOTLY_THEME, height=CHART_H, title="Monatliche Einnahmen vs. Ausgaben",
                         legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1))
    fig_ie.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
    fig_ie.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
    ev_ie = st.plotly_chart(fig_ie, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key="chart_ie")
    pt = _pt(ev_ie)
    if pt:
        ym = pt.get("x", "")[:7]  # normalisiert "2025-07-01" → "2025-07"
        if ym:
            _set_cf({**st.session_state["chart_filter"], "year_month": ym})

with c2:
    grp_cat_exp = (
        expense_df.groupby([col_grp.col, col_cat.col])[col_amt.col]
        .sum().apply(abs).reset_index()
    )
    fig_pie = px.sunburst(
        grp_cat_exp, path=[col_grp.col, col_cat.col], values=col_amt.col, color=col_grp.col,
        color_discrete_sequence=[C["red"], C["amber"], C["blue"], C["green"], C["purple"],
                                  "#38BDF8", "#F472B6", "#34D399"],
    )
    fig_pie.update_traces(
        textinfo="label+percent parent",
        hovertemplate="<b>%{label}</b><br>%{value:,.0f} €<br>%{percentParent:.1%} der Gruppe<br>%{percentRoot:.1%} gesamt<extra></extra>",
    )
    fig_pie.update_layout(**PLOTLY_THEME, height=CHART_H, title="Ausgaben nach Gruppen und Kategorien")
    ev_pie = st.plotly_chart(fig_pie, width='stretch', on_select="rerun", key="chart_pie")
    pt = _pt(ev_pie)
    if pt:
        label, parent = pt.get("label", ""), pt.get("parent", "")
        if parent:   # Kategorie-Ebene: parent = Gruppe
            _set_cf({"group": parent, "category": label, "applicant": None})
        elif label:  # Gruppen-Ebene
            _set_cf({"group": label, "category": None, "applicant": None})


# ── Chart 3: Top-10-Kategorien  |  Heatmap Ausgaben nach Gruppe ──────────────

st.divider()
c3, c4 = st.columns([3, 2], gap="medium")

with c3:
    cat_exp = (
        expense_df.groupby(col_cat.col)[col_amt.col]
        .sum().apply(abs).reset_index()
        .sort_values(col_amt.col, ascending=True).tail(10)
    )
    fig_bar = go.Figure(go.Bar(
        x=cat_exp[col_amt.col], y=cat_exp[col_cat.col],
        orientation="h",
        marker=dict(color=cat_exp[col_amt.col],
                    colorscale=[[0, C["border"]], [1, C["blue"]]], showscale=False),
        hovertemplate="<b>%{y}</b><br>%{x:,.0f} €<extra></extra>",
    ))
    fig_bar.update_layout(**PLOTLY_THEME, height=CHART_H, title="Top 10 Ausgaben-Kategorien")
    fig_bar.update_xaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
    fig_bar.update_yaxes(showgrid=False)
    ev_bar = st.plotly_chart(fig_bar, width='stretch', config={"displayModeBar": False},
                             on_select="rerun", key="chart_bar")
    pt = _pt(ev_bar)
    if pt:
        cat = pt.get("y", "")
        if cat:
            _set_cf({**st.session_state["chart_filter"], "category": cat, "applicant": None})

with c4:
    heat = (
        expense_df.groupby([col_yea.col, col_mon.col, col_grp.col])[col_amt.col]
        .apply(abs).reset_index()
    )
    heat["ym"] = heat[col_yea.col].astype(str) + "-" + heat[col_mon.col].astype(str).str.zfill(2)
    pivot = heat.pivot_table(index=col_grp.col, columns="ym", values=col_amt.col, aggfunc="sum").fillna(0)
    pivot_pct = pivot.div(pivot.sum(axis=1), axis=0) * 100

    fig_hm = go.Figure(go.Heatmap(
        z=pivot_pct.values, x=pivot_pct.columns.tolist(), y=pivot.index.tolist(),
        colorscale=[[0, C["surface"]], [0.05, "#2A35B1"], [.1, "#2AB13C"], [.5, "#B1A12A"], [1, "#B12A2A"]],
        hovertemplate="<b>%{y}</b><br>%{x}<br>%{z:,.1f}%<extra></extra>",
        showscale=True, colorbar=dict(thickness=10),
    ))
    fig_hm.update_layout(**PLOTLY_THEME, height=CHART_H, title="Heatmap: Ausgaben-Anteil nach Gruppe",
                         dragmode="select")
    fig_hm.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
    fig_hm.update_yaxes(showgrid=False)
    ev_hm = st.plotly_chart(fig_hm, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key="chart_hm")
    pt = _pt(ev_hm)
    if pt:
        grp = pt.get("y", "")
        if grp:
            _set_cf({**st.session_state["chart_filter"], "group": grp, "category": None, "applicant": None})


# ── Chart 4: Kumulativer Saldo  |  Transaktionsvolumen-Verteilung ─────────────

st.divider()
c5, c6 = st.columns([3, 2], gap="medium")

def _hex_to_rgba(hex_color: str, alpha: float = 0.6) -> str:
    """Konvertiert einen Hex-Farbstring in rgba-Notation mit Transparenz."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


with c5:
    # ── Gestapelter Saldenverlauf ─────────────────────────────────────────────
    # col_sld = absoluter Kontostand je Konto; für einen Gesamtüberblick
    # werden die Salden zeitlich ausgerichtet und gestapelt (stackgroup).
    # Fehlende Daten zwischen Buchungen werden per ffill interpoliert.
    line_colors = [C["green"], C["blue"], C["amber"], C["purple"], C["red"]]

    # ── Zeitfenster bestimmen ────────────────────────────────────────────────
    # Fenster basiert auf den Filter-Eingaben (Jahr/Monat), nicht auf den
    # tatsächlichen Buchungstagen – damit Konten mit wenigen oder keinen
    # Buchungen im Fenster trotzdem ihren letzten Saldo fortgeschrieben sehen.
    # Datumsspalte einmalig konsistent als normalisierte Timestamps konvertieren.
    # DuckDB liefert je nach Schema date- oder datetime-Werte; Vergleiche und
    # reindex erfordern identische Typen.
    df_raw_norm = df_raw.copy()
    df_raw_norm[col_dat.col] = pd.to_datetime(df_raw_norm[col_dat.col]).dt.normalize()

    # Zeitfenster aus dem FILTER ableiten (nicht aus den Buchungstagen).
    # So bleibt die x-Achse konsistent, auch wenn ein Konto im Fenster nur
    # wenige Buchungen hat oder gar keine. Begrenzung am oberen Rand: heute.
    today = pd.Timestamp(date.today()).normalize()
    win_start = pd.Timestamp(f"{_ym_range[0]}-01")
    win_end   = pd.Timestamp(f"{_ym_range[1]}-01") + pd.offsets.MonthEnd(0)
    win_end   = min(win_end, today)
    if win_start > win_end:
        win_end = win_start

    # Tagesgenauer gemeinsamer Datumsindex über das gesamte Fenster
    all_dates = pd.date_range(start=win_start, end=win_end, freq="D")
    stacked   = pd.DataFrame(index=pd.Index(all_dates, name="date"))

    ibans_in_df = sorted(df_raw_norm["iban"].unique()) if "iban" in df_raw_norm.columns else [None]

    for iban in ibans_in_df:
        # Alle Salden des Kontos vom frühesten bekannten Datum bis Fensterende.
        # Saldo VOR dem Fenster wird so als Startwert berücksichtigt und
        # über das Fenster fortgeschrieben, wenn keine Buchung erfolgt.
        sub_all = (
            df_raw_norm[(df_raw_norm["iban"] == iban) &
                        (df_raw_norm[col_dat.col] <= win_end)]
            [[col_dat.col, col_sld.col]]
            .dropna(subset=[col_sld.col])
            .sort_values(col_dat.col)
            .drop_duplicates(col_dat.col, keep="last")
            .set_index(col_dat.col)
        )
        row = acc_filtered[acc_filtered["IBAN"] == iban]
        label = f"{row.iloc[0]['Person']} · {row.iloc[0]['Bank']} · {row.iloc[0]['Konto']}" if not row.empty else iban

        if sub_all.empty:
            # Konto hat überhaupt keinen Saldo – mit 0 belegen
            stacked[label] = 0.0
            continue

        # Auf täglichen Index reindizieren und ffill über alle Tage
        full_index = pd.date_range(
            start=min(sub_all.index.min(), win_start),
            end=win_end, freq="D",
        )
        series = sub_all[col_sld.col].reindex(full_index).ffill()
        # Auf das Anzeige-Fenster zuschneiden
        stacked[label] = series.reindex(all_dates).values

    # Konten die vor dem Fenster noch keinen Saldo hatten: mit 0 belegen
    stacked = stacked.fillna(0).reset_index().rename(columns={"index": "date"})

    # ── Chart rendern ────────────────────────────────────────────────────
    fig_cum = go.Figure()
    trace_labels = [c for c in stacked.columns if c != "date"]
    multi_acc = len(trace_labels) > 1
    for i, label in enumerate(trace_labels):
        color = line_colors[i % len(line_colors)]
        scatter_kwargs = dict(
            x=stacked["date"], y=stacked[label],
            mode="lines", name=label,
            line=dict(color=color, width=1.5),
            hovertemplate=f"<b>{label}<br>%{{x}}</b><br>%{{y:,.0f}} €<extra></extra>",
        )
        if multi_acc:
            # Mehrere Konten: Bereiche stapeln
            scatter_kwargs["stackgroup"] = "saldo"
            scatter_kwargs["fillcolor"] = _hex_to_rgba(color, 0.6)
        else:
            # Einzelnes Konto: Linie mit Flächenfüllung bis zur 0-Achse
            scatter_kwargs["fill"] = "tozeroy"
        fig_cum.add_scatter(**scatter_kwargs)

    fig_cum.update_layout(
        **PLOTLY_THEME, height=CHART_H,
        title="Saldenverlauf" + (" (gestapelt)" if multi_acc else ""),
        showlegend=multi_acc,
        legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1),
    )
    fig_cum.update_xaxes(showgrid=False, tickangle=45)
    fig_cum.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
    st.plotly_chart(fig_cum, width='stretch', config={"displayModeBar": False})

with c6:
    bins = np.array([0, 10, 50, 100, 500, 1000, 5000, 10000, 50000])
    fig_hist = go.Figure()
    for data, color, name in [
        (income_df[col_amt.col],       C["green"], "Einnahmen"),
        (expense_df[col_amt.col].abs(), C["red"],  "Ausgaben"),
    ]:
        counts, edges = np.histogram(data.dropna(), bins=bins)
        fig_hist.add_bar(
            x=edges[:-1], y=counts, width=np.diff(edges), offset=0,
            name=name, marker_color=color, opacity=0.7,
            hovertemplate=f"<b>{name}<br>%{{x:,.0f}} – %{{customdata:,.0f}} €</b><br>%{{y}} Transaktionen<extra></extra>",
            customdata=edges[1:],
        )
    fig_hist.update_layout(**PLOTLY_THEME, height=CHART_H, barmode="overlay",
                           title="Verteilung der Transaktionsvolumen",
                           legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1))
    fig_hist.update_xaxes(showgrid=False, tickprefix="€ ", type="log")
    fig_hist.update_yaxes(showgrid=True, gridcolor=C["border"], type="log")
    st.plotly_chart(fig_hist, width='stretch', config={"displayModeBar": False})


# ── Chart 5: Jährlicher Vergleich  |  Top Empfänger ──────────────────────────

st.divider()
c7, c8 = st.columns([3, 2], gap="medium")

with c7:
    yearly_grp = (
        expense_df.groupby([col_yea.col, col_grp.col])[col_amt.col]
        .sum().apply(abs).reset_index()
    )
    fig_yg = px.bar(
        yearly_grp, x=col_grp.col, y=col_amt.col,
        color=yearly_grp[col_yea.col].astype(str), barmode="group",
        color_discrete_sequence=[C["blue"], C["green"], C["amber"], C["purple"]],
        labels={col_grp.col: "Gruppe", col_amt.col: "€", col_yea.col: "Jahr"},
    )
    fig_yg.update_layout(**PLOTLY_THEME, height=CHART_H, title="Jährlicher Vergleich nach Gruppe",
                         yaxis_title=None, legend_title_text="",
                         legend=dict(orientation="v", yanchor="top", xanchor="left", y=1, x=0))
    fig_yg.update_traces(hovertemplate="<b>%{x}</b><br>%{y:,.0f} €<extra></extra>")
    fig_yg.update_xaxes(showgrid=False, tickangle=25)
    fig_yg.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
    ev_yg = st.plotly_chart(fig_yg, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key="chart_yg")
    pt = _pt(ev_yg)
    if pt:
        grp = pt.get("x", "")
        if grp:
            _set_cf({**st.session_state["chart_filter"], "group": grp, "category": None, "applicant": None})

with c8:
    payees = (
        expense_df.groupby(col_app.col)[col_amt.col]
        .sum().apply(abs).reset_index()
        .sort_values(col_amt.col, ascending=False).head(12)
    )
    fig_pay = go.Figure(go.Bar(
        x=payees[col_amt.col], y=payees[col_app.col],
        orientation="h", marker_color=C["amber"],
        hovertemplate="<b>%{y}</b><br>%{x:,.0f} €<extra></extra>",
    ))
    fig_pay.update_layout(**PLOTLY_THEME, height=CHART_H, title="Top 12 Empfänger", showlegend=False)
    fig_pay.update_xaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
    fig_pay.update_yaxes(showgrid=False, categoryorder="total ascending")
    ev_pay = st.plotly_chart(fig_pay, width='stretch', config={"displayModeBar": False},
                             on_select="rerun", key="chart_pay")
    pt = _pt(ev_pay)
    if pt:
        app = pt.get("y", "")
        if app:
            _set_cf({**st.session_state["chart_filter"], "applicant": app})


# ── Rohdaten-Tabelle ──────────────────────────────────────────────────────────

st.divider()
with st.expander("📋 Alle Transaktionen", expanded=False):
    show_cols = [col_rid, col_yea, col_mon, col_app, col_loc, col_amt,
                 col_inf, col_add, col_grp, col_cat, col_ctx, col_rel,
                 col_new, col_spc, col_dat, col_note]
    disp = df[[c.col for c in show_cols]].sort_values(col_dat.col, ascending=False).copy()

    st.dataframe(
        disp.style
            .map(colour_amount, subset=[col_amt.col])
            .format({col_amt.col: "{:,.2f} €"}),
        width='stretch',
        height=380,
        hide_index=True,
        column_config=get_config(show_cols) | {
            col_rid.col: None, col_yea.col: None, col_mon.col: None,
        },
    )
