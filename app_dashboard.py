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
    render_category_filters,
    con,
    col_ctx, col_grp, col_cat, col_rel, col_amt, col_app,
    col_loc, col_dat, col_mon, col_yea,
    col_inf, col_add, col_new, col_rid, col_sld, col_spc, col_note,
    DASHBOARD_COLORS, make_plotly_theme,
)

# ── Sicherheits-Gate ──────────────────────────────────────────────────────────
require_master_password()

st.title("📊 Analysieren")

if "chart_filter" not in st.session_state:
    st.session_state["chart_filter"] = {}


# ── Design-System (zentral in app_functions.py, hier nur referenziert) ───────
C = DASHBOARD_COLORS

CHART_H = 420

PLOTLY_THEME = make_plotly_theme()


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
        FROM "{safe_iban}_v"
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


# ── Filter-UI (gemeinsamer Helper mit app_assign.py) ─────────────────────────
def _render_ym_slider():
    _today_dt = pd.Timestamp(date.today())
    _def_e = min(_today_dt.strftime("%Y-%m"), _ym_opts[-1])
    _def_s = max((_today_dt - pd.DateOffset(months=11)).strftime("%Y-%m"), _ym_opts[0])
    return st.select_slider(
        "Zeitraum",
        options=_ym_opts,
        value=(_def_s, _def_e),
        format_func=lambda ym: f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}",
        key="sl_ym",
    )

_f = render_category_filters(
    cat_df, all_grps, all_cats, ctxs, rels,
    search_cols=[col_app.col, col_inf.col, col_add.col],
    def_flr_rel=def_flr_rel, def_exc_ctx=def_exc_ctx, def_exc_spc=True,
    render_extra=_render_ym_slider,
)
_ym_range           = _f["extra"]
flr_grp, exc_grp    = _f["flr_grp"], _f["exc_grp"]
flr_cat, exc_cat    = _f["flr_cat"], _f["exc_cat"]
flr_new, exc_new    = _f["flr_new"], _f["exc_new"]
flr_cnt, exc_cnt    = _f["flr_cnt"], _f["exc_cnt"]
flr_rel, exc_rel    = _f["flr_rel"], _f["exc_rel"]
flr_spc, exc_spc    = _f["flr_spc"], _f["exc_spc"]
src_col1, src_txt1  = _f["src_col1"], _f["src_txt1"]
src_col2, src_txt2  = _f["src_col2"], _f["src_txt2"]


# ── Basis-Filter anwenden (Formular-Filter aus dem Filter-Expander) ──────────
df_filtered = df[
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
df_filtered = df_filtered[df_filtered[col_amt.col].notna()]


# ── Cross-Filter-Helfer (Klick auf Chart-Elemente) ───────────────────────────
# Jedes Chart "besitzt" die Filter-Schlüssel, die es selbst per Klick setzt
# (CHART_OWN_KEYS). Beim Aufbau der EIGENEN Datenbasis eines Charts werden
# nur die Filter der JEWEILS ANDEREN Charts angewendet (cf_frame) – die
# eigene Auswahl wird stattdessen per Opacity hervorgehoben (dim_traces),
# damit z.B. weiterhin alle Monate/Gruppen sichtbar bleiben. KPIs und die
# Transaktions-Tabelle nutzen dagegen immer den vollständigen Filter (df).
#
# Ein erneuter Klick auf ein bereits selektiertes Element hebt nur diesen
# einen Filter wieder auf (_toggle_cf). Der Reset-Button im Hinweisbanner
# hebt dagegen alle Chart-Filter gleichzeitig auf.

CHART_OWN_KEYS: dict[str, tuple[str, ...]] = {
    "cf":  ("year_month",),
    "cr":  ("context", "relation"),
    "ie":  ("year_month",),
    "pie": ("group", "category"),
    "bar": ("category",),
    "hm":  ("group",),
    "yg":  ("group",),
    "pay": ("applicant",),
    "mc":  ("year_month",),
}

_CF_COLS = {
    "group":     col_grp.col,
    "category":  col_cat.col,
    "applicant": col_app.col,
    "context":   col_ctx.col,
    "relation":  col_rel.col,
}


def apply_cf(d: pd.DataFrame, cf: dict, exclude: tuple = ()) -> pd.DataFrame:
    """Wendet die Chart-Filter (außer den in `exclude` genannten Schlüsseln) an."""
    mask = pd.Series(True, index=d.index)
    for key, colname in _CF_COLS.items():
        if cf.get(key) and key not in exclude:
            mask &= d[colname] == cf[key]
    if cf.get("year_month") and "year_month" not in exclude:
        mask &= d["year_month"] == cf["year_month"]
    return d[mask]


def cf_frame(chart_id: str) -> pd.DataFrame:
    """Datenbasis für ein Chart: alle Filter außer den eigenen Klick-Schlüsseln.
    Sonderfall Sunburst ("pie"): Gruppe/Kategorie werden nur dann von der
    eigenen Filterung ausgenommen, wenn die aktuelle Auswahl vom Sunburst
    selbst stammt (siehe _gc()). Wurde sie stattdessen über "bar" (Kategorie)
    oder "hm"/"yg" (Gruppe) gesetzt, gilt sie als Cross-Filter eines anderen
    Charts und wird ganz normal angewendet."""
    exclude = CHART_OWN_KEYS[chart_id]
    if chart_id == "pie" and cf.get("_gc_owner", "pie") != "pie":
        exclude = ()
    return apply_cf(df_filtered, cf, exclude=exclude)


def _toggle_cf(cf: dict, **kv) -> dict:
    """Setzt die übergebenen Schlüssel; ist die exakt gleiche Auswahl bereits
    aktiv, werden nur diese Schlüssel wieder aufgehoben (Toggle)."""
    new_cf = dict(cf)
    if all(new_cf.get(k) == v for k, v in kv.items()):
        for k in kv:
            new_cf[k] = None
    else:
        new_cf.update(kv)
    return new_cf


def _gc(new_cf: dict, chart_id: str) -> dict:
    """Markiert, welches Chart die aktuelle Gruppen-/Kategorie-Auswahl gesetzt
    hat (_gc_owner). Der Sunburst ("pie") teilt sich diese beiden Filter-
    Schlüssel mit "bar" (Kategorie) und "hm"/"yg" (Gruppe) – siehe cf_frame():
    Nur wenn die Auswahl vom Sunburst SELBST stammt, wird sie von dessen
    eigener Datenbasis ausgeschlossen (Zoom statt Filter, mit Prozentangaben
    relativ zum Gesamtbudget). Stammt sie von einem ANDEREN Chart, filtert
    sie den Sunburst wie jeden anderen Chart-Filter auch – genauso, wie es
    bereits für die Transaktions-Tabelle gilt. Ohne Gruppe/Kategorie in der
    Auswahl wird die Markierung entfernt, damit sie nicht fälschlich als
    aktiver Filter im Info-Banner erscheint."""
    new_cf["_gc_owner"] = chart_id if (new_cf.get("group") or new_cf.get("category")) else None
    return new_cf


def _click_changed(chart_id: str, pt: dict, *sig_fields: str) -> bool:
    """Liefert True bei einer ECHTEN Änderung der Klick-Selektion gegenüber
    dem letzten Aufruf – sowohl bei einer neuen Auswahl als auch bei einem
    NATIVEN DESELECT: Plotly meldet einen erneuten Klick auf ein bereits
    selektiertes Element als LEERE Selektion (nicht als erneuter Klick mit
    demselben Wert). Eine leere Selektion DIREKT NACH einem durch
    _bump_chart() ausgelösten Neu-Mount wird dagegen ignoriert, da sie nur
    der Startzustand der frischen, noch nie angeklickten Widget-Instanz ist
    und kein tatsächliches Deselect durch den Nutzer."""
    state_key = f"_last_click_{chart_id}"
    nonce_key = f"_last_click_nonce_{chart_id}"
    cur_nonce = st.session_state.get("_chart_nonce", {}).get(chart_id, 0)
    prev_nonce = st.session_state.get(nonce_key)
    sig = tuple(pt.get(f) for f in sig_fields) if pt else None

    if sig is None and prev_nonce != cur_nonce:
        st.session_state[nonce_key] = cur_nonce
        st.session_state[state_key] = None
        return False

    if st.session_state.get(state_key) == sig:
        return False
    st.session_state[state_key] = sig
    st.session_state[nonce_key] = cur_nonce
    return True


def _chart_key(chart_id: str) -> str:
    """Widget-Key inkl. Nonce für ein Chart. Streamlit speichert die zuletzt
    angeklickte Selektion pro Widget-Key dauerhaft – ein zweiter Klick auf
    exakt dasselbe Element würde sonst als 'keine Wertänderung' gar keinen
    neuen Durchlauf auslösen und ließe sich damit nie zurücksetzen. Der Nonce
    erzwingt nach jeder verarbeiteten Aktion (_bump_chart) einen frischen
    Neu-Mount der Komponente ohne Rest-Selektion."""
    nonce = st.session_state.setdefault("_chart_nonce", {}).get(chart_id, 0)
    return f"chart_{chart_id}_{nonce}"


def _bump_chart(chart_id: str) -> None:
    nonces = st.session_state.setdefault("_chart_nonce", {})
    nonces[chart_id] = nonces.get(chart_id, 0) + 1


def dim_traces(fig: go.Figure, axis: str, matches) -> None:
    """Hebt passende Punkte über ein EIGENES marker.opacity-Array hervor.
    WICHTIG: Plotlys eingebautes 'selectedpoints' darf hierfür NICHT genutzt
    werden – Streamlit meldet über on_select JEDEN aktuell in der Grafik als
    'selektiert' markierten Punkt zurück, UNABHÄNGIG davon, ob das von einem
    echten Nutzer-Klick stammt oder von uns selbst per selectedpoints gesetzt
    wurde. Das führt dazu, dass die eigene Klick-Erkennung sich selbst
    bestätigt und ein echtes Deselect (erneuter Klick) nicht mehr erkennt.
    Plotlys native Selektions-Abdunklung wird daher zusätzlich explizit
    neutralisiert (unselected.marker.opacity=1), damit sie sich nicht mit
    dem eigenen Array überlagert (führte zuvor zu kumulativ steigender
    Transparenz bei wiederholten Klicks)."""
    for trace in fig.data:
        if getattr(trace, "hoverinfo", None) == "skip":
            # Bewusst von Klick-/Hover-Interaktion ausgeschlossene Traces
            # (z.B. die Ø-3M-Linie) unangetastet lassen: unselected/marker
            # -Eigenschaften auf einer nicht-interaktiven Trace zu setzen
            # kann Plotlys Klick-Auflösung für die GESAMTE Grafik stören.
            continue
        vals = getattr(trace, axis, None)
        if vals is None:
            continue
        trace.update(
            marker_opacity=[1.0 if matches(trace, v) else 0.25 for v in vals],
            unselected=dict(marker=dict(opacity=1)),
        )


# ── Chart-Filter (Klicks) anwenden ────────────────────────────────────────────
# df: vollständig gefiltert (alle Chart-Klicks kombiniert) – Basis für KPIs
# und die Transaktions-Tabelle. Die einzelnen Charts bauen ihre eigene
# Datenbasis über cf_frame() (siehe oben), um den eigenen Klick-Filter NICHT
# auf sich selbst anzuwenden (Hervorhebung statt Ausblenden).
cf = st.session_state["chart_filter"]
df = apply_cf(df_filtered, cf)

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

if any(cf.values()):
    _parts = []
    if cf.get("group"):      _parts.append(f"Gruppe: **{cf['group']}**")
    if cf.get("category"):   _parts.append(f"Kategorie: **{cf['category']}**")
    if cf.get("applicant"):  _parts.append(f"Empfänger: **{cf['applicant']}**")
    if cf.get("year_month"): _parts.append(f"Monat: **{cf['year_month']}**")
    if cf.get("context"):    _parts.append(f"Kontext: **{cf['context']}**")
    if cf.get("relation"):   _parts.append(f"Beziehung: **{cf['relation']}**")
    _ci, _cb = st.columns([9, 1])
    _ci.info("🎯 Chart-Filter: " + " · ".join(_parts))
    if _cb.button("✕", key="reset_cf", help="Alle Chart-Filter zurücksetzen"):
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
    d = cf_frame("cf")
    monthly = (
        d.groupby("year_month")[col_amt.col].sum()
        .reindex(all_year_months, fill_value=0)
        .reset_index()
    )
    monthly["color"] = monthly[col_amt.col].apply(lambda x: C["green"] if x >= 0 else C["red"])
    monthly["idx"] = range(len(monthly))
    monthly["label"] = monthly["year_month"].apply(lambda ym: f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}")

    # Bewusst minimal gehalten (nur die Balken-Trace, keine Ø-3M-Linie, keine
    # Text-Labels, keine Shapes): jede Zusatz-Trace/-Shape auf diesem Chart
    # hat dazu geführt, dass die Klick-Erkennung nach dem ersten Klick
    # dauerhaft ausfiel. Dieser schlanke Aufbau entspricht exakt dem Muster
    # der zuverlässig funktionierenden Charts (z.B. "Top 12 Empfänger").
    fig_cf = go.Figure()
    fig_cf.add_bar(
        x=monthly["idx"], y=monthly[col_amt.col],
        marker_color=monthly["color"], name="Saldo",
        customdata=monthly["year_month"],
        hovertemplate="<b>%{customdata}</b><br>%{y:,.0f} €<extra></extra>",
    )
    fig_cf.add_hline(y=0, line_color=C["border"], line_width=1)
    fig_cf.update_layout(**PLOTLY_THEME, height=CHART_H, title="Monatlicher Saldo", showlegend=False)
    fig_cf.update_xaxes(showgrid=False, tickangle=45,
                        tickmode="array", tickvals=monthly["idx"], ticktext=monthly["label"])
    fig_cf.update_yaxes(showgrid=True, gridcolor=C["border"], zeroline=False,
                        ticksuffix=" €", tickformat=",.0f")
    if cf.get("year_month"):
        dim_traces(fig_cf, "customdata", lambda tr, v: v == cf["year_month"])
    ev_cf = st.plotly_chart(fig_cf, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key=_chart_key("cf"))
    pt = _pt(ev_cf)
    if _click_changed("cf", pt, "customdata"):
        _bump_chart("cf")
        ym = pt.get("customdata", "") if pt else ""
        if ym:
            _set_cf(_toggle_cf(cf, year_month=ym))
        else:
            _set_cf({**cf, "year_month": None})

with _col_matrix:
    d = cf_frame("cr")
    _cr = (
        d.groupby([col_ctx.col, col_rel.col])[col_amt.col]
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
        if cf.get("context") or cf.get("relation"):
            dim_traces(fig_cr, "x", lambda tr, v: v == cf.get("context") and tr.name == cf.get("relation"))
        ev_cr = st.plotly_chart(fig_cr, width='stretch', config={"displayModeBar": False},
                                on_select="rerun", key=_chart_key("cr"))
        pt = _pt(ev_cr)
        if _click_changed("cr", pt, "x", "customdata"):
            _bump_chart("cr")
            ctx_val = pt.get("x", "") if pt else ""
            rel_val = pt.get("customdata", [None])[0] if pt else None
            if ctx_val and rel_val:
                _set_cf(_toggle_cf(cf, context=ctx_val, relation=rel_val))
            else:
                _set_cf({**cf, "context": None, "relation": None})


# ── Chart 2: Einnahmen vs. Ausgaben  |  Sunburst Ausgaben ────────────────────

st.divider()
c1, c2 = st.columns([3, 2], gap="medium")

with c1:
    d = cf_frame("ie")
    inc_m  = d[d[col_amt.col] > 0].groupby("year_month")[col_amt.col].sum().reset_index(name="income")
    exp_m  = d[d[col_amt.col] < 0].groupby("year_month")[col_amt.col].sum().apply(abs).reset_index(name="expense")
    merged = (
        pd.DataFrame({"year_month": all_year_months})
        .merge(inc_m, on="year_month", how="left")
        .merge(exp_m, on="year_month", how="left")
        .fillna(0)
    )
    merged["idx"] = range(len(merged))
    merged["label"] = merged["year_month"].apply(lambda ym: f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}")

    fig_ie = go.Figure()
    for series, color, label in [
        ("income",  C["green"], "Einnahmen"),
        ("expense", C["red"],   "Ausgaben"),
    ]:
        fig_ie.add_scatter(
            x=merged["idx"], y=merged[series],
            mode="lines+markers", name=label,
            line=dict(color=color, width=2), marker=dict(size=4),
            fill="tozeroy",
            fillcolor=f"rgba({','.join(str(int(color.lstrip('#')[i:i+2], 16)) for i in (0,2,4))},0.07)",
            customdata=merged["year_month"],
            hovertemplate=f"<b>{label}<br>%{{customdata}}</b><br>%{{y:,.0f}} €<extra></extra>",
        )
    fig_ie.update_layout(**PLOTLY_THEME, height=CHART_H, title="Monatliche Einnahmen vs. Ausgaben",
                         legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1))
    # Numerische x-Positionen statt datumsähnlicher Strings – siehe Kommentar
    # bei "Monatlicher Saldo" weiter oben (type="category" störte dort das
    # native Klick-Toggle-Verhalten von Plotly).
    fig_ie.update_xaxes(showgrid=False, tickangle=45,
                        tickmode="array", tickvals=merged["idx"], ticktext=merged["label"])
    fig_ie.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
    if cf.get("year_month"):
        dim_traces(fig_ie, "customdata", lambda tr, v: v == cf["year_month"])
    ev_ie = st.plotly_chart(fig_ie, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key=_chart_key("ie"))
    pt = _pt(ev_ie)
    if _click_changed("ie", pt, "customdata"):
        _bump_chart("ie")
        ym = pt.get("customdata", "") if pt else ""
        if ym:
            _set_cf(_toggle_cf(cf, year_month=ym))
        else:
            _set_cf({**cf, "year_month": None})

with c2:
    d = cf_frame("pie")
    grp_cat_exp = (
        d[d[col_amt.col] < 0].groupby([col_grp.col, col_cat.col])[col_amt.col]
        .sum().apply(abs).reset_index()
    )
    if grp_cat_exp.empty:
        st.info("Keine Ausgaben für die Sunburst-Darstellung im gewählten Zeitraum.")
    else:
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

        # Das Sunburst-Chart setzt sein EIGENES, eingebautes Klick-Zoom-
        # Verhalten direkt als Dashboard-Filter um (statt wie die anderen
        # Charts nur hervorzuheben): der aktuell fokussierte Knoten wird
        # über das "level"-Attribut aus cf abgeleitet – rein daten-getrieben,
        # ohne auf clientseitig erhaltenen Zoom-Zustand angewiesen zu sein.
        # Ein Klick auf den bereits fokussierten Knoten (= Zentrum) hebt
        # genau diese Ebene wieder auf.
        _ids_present = set(grp_cat_exp[col_grp.col]) | {
            f"{g}/{c}" for g, c in zip(grp_cat_exp[col_grp.col], grp_cat_exp[col_cat.col])
        }
        _level = ""
        if cf.get("group") and cf.get("category") and f"{cf['group']}/{cf['category']}" in _ids_present:
            _level = f"{cf['group']}/{cf['category']}"
        elif cf.get("group") and cf["group"] in _ids_present:
            _level = cf["group"]
        fig_pie.update_traces(level=_level)

        ev_pie = st.plotly_chart(fig_pie, width='stretch', config={"displayModeBar": False},
                                  on_select="rerun", key="chart_pie")
        pt = _pt(ev_pie)
        if _click_changed("pie", pt, "label", "parent"):
            label, parent = pt.get("label", ""), pt.get("parent", "")
            cur_grp, cur_cat = cf.get("group"), cf.get("category")
            if label == cur_grp and parent == "" and not cur_cat:
                # Klick auf die bereits fokussierte Gruppe (Zentrum) → zurücksetzen
                _set_cf(_gc({**cf, "group": None, "category": None}, "pie"))
            elif label == cur_cat and parent == cur_grp:
                # Klick auf die bereits fokussierte Kategorie (Zentrum) → eine Ebene zurück
                _set_cf(_gc({**cf, "category": None}, "pie"))
            elif parent:
                # Kategorie-Ebene angeklickt (ggf. inkl. Gruppenwechsel)
                _set_cf(_gc({**cf, "group": parent, "category": label}, "pie"))
            elif label:
                # Gruppen-Ebene angeklickt
                _set_cf(_gc({**cf, "group": label, "category": None}, "pie"))



# ── Chart 3: Top-10-Kategorien  |  Heatmap Ausgaben nach Gruppe ──────────────

st.divider()
c3, c4 = st.columns([3, 2], gap="medium")

with c3:
    d = cf_frame("bar")
    cat_exp = (
        d[d[col_amt.col] < 0].groupby(col_cat.col)[col_amt.col]
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
    if cf.get("category"):
        dim_traces(fig_bar, "y", lambda tr, v: v == cf["category"])
    ev_bar = st.plotly_chart(fig_bar, width='stretch', config={"displayModeBar": False},
                             on_select="rerun", key=_chart_key("bar"))
    pt = _pt(ev_bar)
    if _click_changed("bar", pt, "y"):
        _bump_chart("bar")
        cat = pt.get("y", "") if pt else ""
        if cat:
            _set_cf(_gc(_toggle_cf(cf, category=cat), "bar"))
        else:
            _set_cf(_gc({**cf, "category": None}, "bar"))

with c4:
    d = cf_frame("hm")
    heat = (
        d[d[col_amt.col] < 0].groupby([col_yea.col, col_mon.col, col_grp.col])[col_amt.col]
        .sum().apply(abs).reset_index()
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
    fig_hm.update_layout(**PLOTLY_THEME, height=CHART_H, title="Heatmap: Ausgaben-Anteil nach Gruppe")
    fig_hm.update_xaxes(showgrid=False, tickangle=45, dtick="M1")
    fig_hm.update_yaxes(showgrid=False)
    if cf.get("group") in pivot.index.tolist():
        _y_idx = pivot.index.tolist().index(cf["group"])
        fig_hm.add_shape(type="rect", xref="paper", x0=0, x1=1,
                         y0=_y_idx - 0.5, y1=_y_idx + 0.5,
                         line=dict(color=C["amber"], width=2))
    ev_hm = st.plotly_chart(fig_hm, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key=_chart_key("hm"))
    pt = _pt(ev_hm)
    if _click_changed("hm", pt, "y"):
        _bump_chart("hm")
        grp = pt.get("y", "") if pt else ""
        if grp:
            _set_cf(_gc(_toggle_cf(cf, group=grp), "hm"))
        else:
            _set_cf(_gc({**cf, "group": None}, "hm"))


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
    # Bewusst OHNE Chart-Filter: der Saldenverlauf zeigt den tatsächlichen
    # Kontostand und wird nur durch Konto-Auswahl und Zeitraum bestimmt.
    line_colors = [C["green"], C["blue"], C["amber"], C["purple"], C["red"]]

    df_raw_norm = df_raw.copy()
    df_raw_norm[col_dat.col] = pd.to_datetime(df_raw_norm[col_dat.col]).dt.normalize()

    today = pd.Timestamp(date.today()).normalize()
    win_start = pd.Timestamp(f"{_ym_range[0]}-01")
    win_end   = pd.Timestamp(f"{_ym_range[1]}-01") + pd.offsets.MonthEnd(0)
    win_end   = min(win_end, today)
    if win_start > win_end:
        win_end = win_start

    all_dates = pd.date_range(start=win_start, end=win_end, freq="D")
    stacked   = pd.DataFrame(index=pd.Index(all_dates, name="date"))

    ibans_in_df = sorted(df_raw_norm["iban"].unique()) if "iban" in df_raw_norm.columns else [None]

    for iban in ibans_in_df:
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
            stacked[label] = 0.0
            continue

        full_index = pd.date_range(
            start=min(sub_all.index.min(), win_start),
            end=win_end, freq="D",
        )
        series = sub_all[col_sld.col].reindex(full_index).ffill()
        stacked[label] = series.reindex(all_dates).values

    stacked = stacked.fillna(0).reset_index().rename(columns={"index": "date"})

    fig_cum = go.Figure()
    trace_labels = [c for c in stacked.columns if c != "date"]
    multi_acc = len(trace_labels) > 1
    stacked_total = stacked[trace_labels].sum(axis=1)

    for i, label in enumerate(trace_labels):
        color = line_colors[i % len(line_colors)]
        scatter_kwargs = dict(
            x=stacked["date"], y=stacked[label],
            mode="lines", name=label,
            line=dict(color=color, width=1.5),
            customdata=stacked_total,
            hovertemplate=(
                f"<b>%{{x}}<br>{label}</b><br>Konto: %{{y:,.0f}} €"
                + ("<br>Gesamt: %{customdata:,.0f} €" if multi_acc else "")
                + "<extra></extra>"
            ),
        )
        if multi_acc:
            scatter_kwargs["stackgroup"] = "saldo"
            scatter_kwargs["fillcolor"] = _hex_to_rgba(color, 0.6)
        else:
            scatter_kwargs["fill"] = "tozeroy"
        fig_cum.add_scatter(**scatter_kwargs)

    fig_cum.update_layout(
        **PLOTLY_THEME, height=CHART_H,
        title="Saldenverlauf" + (" (gestapelt)" if multi_acc else ""),
        showlegend=multi_acc,
        legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1),
    )
    # Monatsanfänge als Tick-Positionen mit "MMM YYYY"-Beschriftung (deutsche
    # Kürzel aus MONTH_NAMES, zur Vereinheitlichung mit den anderen Charts).
    _tick_dates_cum = pd.date_range(win_start, win_end, freq="MS")
    fig_cum.update_xaxes(
        showgrid=False, tickangle=45,
        tickmode="array", tickvals=_tick_dates_cum,
        ticktext=[f"{MONTH_NAMES[d.month]} {d.year}" for d in _tick_dates_cum],
    )
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
    d = cf_frame("yg")
    yearly_grp = (
        d[d[col_amt.col] < 0].groupby([col_yea.col, col_grp.col])[col_amt.col]
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
    if cf.get("group"):
        dim_traces(fig_yg, "x", lambda tr, v: v == cf["group"])
    ev_yg = st.plotly_chart(fig_yg, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key=_chart_key("yg"))
    pt = _pt(ev_yg)
    if _click_changed("yg", pt, "x"):
        _bump_chart("yg")
        grp = pt.get("x", "") if pt else ""
        if grp:
            _set_cf(_gc(_toggle_cf(cf, group=grp), "yg"))
        else:
            _set_cf(_gc({**cf, "group": None}, "yg"))

with c8:
    d = cf_frame("pay")
    payees = (
        d[d[col_amt.col] < 0].groupby(col_app.col)[col_amt.col]
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
    if cf.get("applicant"):
        dim_traces(fig_pay, "y", lambda tr, v: v == cf["applicant"])
    ev_pay = st.plotly_chart(fig_pay, width='stretch', config={"displayModeBar": False},
                             on_select="rerun", key=_chart_key("pay"))
    pt = _pt(ev_pay)
    if _click_changed("pay", pt, "y"):
        _bump_chart("pay")
        app = pt.get("y", "") if pt else ""
        if app:
            _set_cf(_toggle_cf(cf, applicant=app))
        else:
            _set_cf({**cf, "applicant": None})


# ── Chart: Monatsvergleich (frei wählbare Monate) ────────────────────────────
# Nutzt cf_frame("mc") (= gefilterte Daten ohne den eigenen year_month-Filter),
# damit ein Klick auf einen Monats-Balken sich nicht selbst auf nur noch
# diesen Monat filtert – alle gewählten Monate bleiben sichtbar.

st.divider()
_prev_ym = (pd.Timestamp(f"{_today_ym}-01") - pd.DateOffset(months=1)).strftime("%Y-%m")
_mc_defaults = sorted({m for m in (
    all_year_months[0]  if all_year_months else None,   # erster Monat im Filterzeitraum
    _prev_ym,                                            # Vormonat
    all_year_months[-1] if all_year_months else None,   # aktueller Monat (Ende Filterzeitraum)
) if m and m in all_year_months})

_mc_label = lambda ym: f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}"

mc_months = st.multiselect(
    "Monate zum Vergleich auswählen",
    options=all_year_months,
    default=_mc_defaults,
    format_func=_mc_label,
    key="mc_months",
)

if mc_months:
    mc_sel = sorted(mc_months)
    d = cf_frame("mc")
    mc_exp = (
        d[(d[col_amt.col] < 0) & (d["year_month"].isin(mc_sel))]
        .groupby([col_grp.col, "year_month"])[col_amt.col]
        .sum().apply(abs).reset_index()
    )
    _mc_colors = [C["blue"], C["green"], C["amber"], C["purple"], C["red"],
                  "#38BDF8", "#F472B6", "#34D399"]

    fig_mc = go.Figure()
    for i, ym in enumerate(mc_sel):
        _sub = mc_exp[mc_exp["year_month"] == ym]
        fig_mc.add_bar(
            x=_sub[col_grp.col], y=_sub[col_amt.col],
            name=_mc_label(ym),
            marker_color=_mc_colors[i % len(_mc_colors)],
            customdata=[[ym]] * len(_sub),
            hovertemplate=f"<b>%{{x}} · {_mc_label(ym)}</b><br>%{{y:,.0f}} €<extra></extra>",
        )
    fig_mc.update_layout(**PLOTLY_THEME, height=CHART_H, title="Monatsvergleich nach Gruppe",
                         barmode="group",
                         legend=dict(orientation="h", yanchor="top", xanchor="right", y=1, x=1))
    fig_mc.update_xaxes(showgrid=False, tickangle=25)
    fig_mc.update_yaxes(showgrid=True, gridcolor=C["border"], ticksuffix=" €", tickformat=",.0f")
    # Plotlys eingebaute Punkt-Abdunklung bei aktiver Klick-Selektion neutralisieren:
    # Die Hervorhebung erfolgt hier bewusst über die (davon unabhängige) Trace-weite
    # Opacity je Monat, nicht über einzelne Punkte.
    fig_mc.update_traces(unselected=dict(marker=dict(opacity=1)))
    if cf.get("year_month"):
        for tr in fig_mc.data:
            _sel = bool(tr.customdata) and tr.customdata[0][0] == cf["year_month"]
            tr.opacity = 1.0 if _sel else 0.25
    ev_mc = st.plotly_chart(fig_mc, width='stretch', config={"displayModeBar": False},
                            on_select="rerun", key=_chart_key("mc"))
    pt = _pt(ev_mc)
    if _click_changed("mc", pt, "customdata"):
        _bump_chart("mc")
        ym_val = pt.get("customdata", [None])[0] if pt else None
        if ym_val:
            _set_cf(_toggle_cf(cf, year_month=ym_val))
        else:
            _set_cf({**cf, "year_month": None})
else:
    st.info("Bitte mindestens einen Monat auswählen.")


# ── Rohdaten-Tabelle ──────────────────────────────────────────────────────────
# Zeigt die vollständig gefilterte Datenbasis (Formular- + alle Chart-Filter).

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
