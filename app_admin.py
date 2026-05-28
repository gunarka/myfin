"""
app_admin.py
Verwaltung von Bankkonten und FinTS-Zugangsdaten.
Tabs: Hinzufügen · Laden · Entfernen
SICHERHEIT: Tabellennamen (IBANs) werden vor SQL-Verwendung bereinigt.
            PINs werden nie angezeigt.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import json
import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st

from app_functions import (
    require_master_password,
    load_fints_credentials,
    save_fints_credentials,
    delete_fints_credentials,
    FintsCredentials,
    FIELD_LABELS,
    list_saved_users,
    DATA_DIR as _DATA_DIR,
    con,
)
_PKG_FILE  = _DATA_DIR / "installed_packages.txt"
_LOG_FILE  = _DATA_DIR / "software_log.txt"


def _run_pip(*args: str) -> tuple[int, str, str]:
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True, text=True
    )
    return result.returncode, result.stdout, result.stderr


def _write_installed_packages() -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _, out, _ = _run_pip("list", "--format=freeze")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _PKG_FILE.write_text(f"# Pakete – Stand {ts}\n{out}", encoding="utf-8")


def _log_sw(action: str, detail: str) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {action}: {detail}\n")


def _get_outdated() -> list[dict]:
    _, out, _ = _run_pip("list", "--outdated", "--format=json")
    try:
        return json.loads(out)
    except Exception:
        return []


def _get_installed() -> list[dict]:
    _, out, _ = _run_pip("list", "--format=json")
    try:
        return json.loads(out)
    except Exception:
        return []


def _valid_pkg_spec(spec: str) -> bool:
    return bool(re.match(r'^[A-Za-z0-9_\-\.\[\],>=<!~\s]+$', spec))


def _collect_project_imports() -> set[str]:
    """Parse all project .py files (excluding .venv) and return top-level import names."""
    import ast
    imports: set[str] = set()
    for f in Path(__file__).parent.rglob("*.py"):
        if ".venv" in f.parts or "__pycache__" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split(".")[0])
    return imports


def _analyze_packages() -> tuple[list[dict], list[dict], list[dict]]:
    """
    Analyse installed packages against project imports.

    Returns three lists of dicts:
      directly_used   – package provides a module imported in the project
      dependencies    – not imported directly, but required by another package
      possibly_unused – not imported, and no other package depends on it
    """
    import importlib.metadata as m
    def _norm(name: str) -> str:
        return name.lower().replace("_", "-")

    project_imports = _collect_project_imports()

    # module_name -> [dist_name, ...]
    pkg_dist = m.packages_distributions()

    # dist_name -> set of top-level module names
    dist_modules: dict[str, set[str]] = {}
    for mod, dists in pkg_dist.items():
        for d in dists:
            dist_modules.setdefault(d, set()).add(mod)

    # dist_name_normalized -> [names of packages that require it]
    reverse_deps: dict[str, list[str]] = {}
    all_dists = list(m.distributions())
    for d in all_dists:
        name = d.metadata["Name"]
        for req in (d.metadata.get_all("Requires-Dist") or []):
            dep_name = re.split(r"[\s>=<!~;\[\(]", req.strip())[0]
            reverse_deps.setdefault(_norm(dep_name), []).append(name)

    # Always-keep packages that are expected to have no reverse-deps
    _SYSTEM_PKGS = {"pip", "setuptools", "wheel", "pkg-resources", "distribute"}

    directly_used: list[dict] = []
    dependencies: list[dict] = []
    possibly_unused: list[dict] = []

    for d in all_dists:
        name    = d.metadata["Name"]
        version = d.metadata["Version"]
        mods    = dist_modules.get(name, set())
        req_by  = sorted(set(reverse_deps.get(_norm(name), [])))
        used_mods = sorted(mods & project_imports)

        if used_mods:
            directly_used.append({"Paket": name, "Version": version, "Import(s)": ", ".join(used_mods)})
        elif req_by:
            dependencies.append({"Paket": name, "Version": version, "Benötigt von": ", ".join(req_by)})
        elif _norm(name) not in _SYSTEM_PKGS:
            possibly_unused.append({"Paket": name, "Version": version, "Module": ", ".join(sorted(mods)[:6])})

    key = lambda x: x["Paket"].lower()
    return sorted(directly_used, key=key), sorted(dependencies, key=key), sorted(possibly_unused, key=key)


# ── Sicherheits-Gate ──────────────────────────────────────────────────────────
require_master_password()

st.header("⚙️ Administrieren")


def _is_valid_iban(iban: str) -> bool:
    """Einfache Formatprüfung: 15–34 alphanumerische Zeichen."""
    return bool(re.fullmatch(r"[A-Z]{2}[0-9A-Z]{13,32}", iban.strip()))


# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_accounts, tab_software = st.tabs(["🏦 Bankkonten", "🔧 Software"])


# ── Tab: Bankkonten ───────────────────────────────────────────────────────────
with tab_accounts:
    # Einmalig laden – wird von allen drei Expanders gemeinsam genutzt
    saved_df = list_saved_users()
    saved    = saved_df["KEY"].to_list()

    # ── Hinzufügen ────────────────────────────────────────────────────────────
    with st.expander("➕ Konto hinzufügen", expanded=False):
        with st.form("save_form"):
            name            = st.text_input(FIELD_LABELS["name"])
            bank            = st.text_input(FIELD_LABELS["bank"])
            typ             = st.text_input(FIELD_LABELS["typ"])
            bank_account    = st.text_input(FIELD_LABELS["bank_account"])
            abruf           = st.selectbox("Abruf via", ["FinTS", "CSV"], index=0)
            st.caption("Nur für FinTS erforderlich:")
            bank_identifier = st.text_input(FIELD_LABELS["bank_identifier"])
            user_id         = st.text_input(FIELD_LABELS["user_id"])
            pin             = st.text_input(FIELD_LABELS["pin"], type="password")
            server          = st.text_input(FIELD_LABELS["server"])
            pid             = st.text_input(FIELD_LABELS["pid"])
            submitted = st.form_submit_button("💾 Speichern", width='stretch')

        if submitted:
            if not all([name, bank, typ, bank_account]):
                st.error("Bitte Name, Bank, Kontotyp und IBAN ausfüllen.")
            elif not _is_valid_iban(bank_account):
                st.error("Ungültiges IBAN-Format.")
            elif abruf == "FinTS" and not all([bank_identifier, user_id, pin, server, pid]):
                st.error("Bitte alle FinTS-Felder ausfüllen.")
            elif abruf == "FinTS" and not server.strip().lower().startswith("https://"):
                st.error("FinTS Server-URL muss mit https:// beginnen (TLS erforderlich).")
            else:
                account_key = f"{name}_{bank}_{typ}"
                try:
                    if abruf == "FinTS":
                        save_fints_credentials(FintsCredentials(
                            account=account_key,
                            name=name, bank=bank, typ=typ,
                            bank_account=bank_account.strip(),
                            bank_identifier=bank_identifier,
                            user_id=user_id, pin=pin,
                            server=server, pid=pid,
                        ))
                    con.execute(
                        "INSERT INTO Accounts (Person, Bank, Konto, IBAN, Abruf) VALUES (?, ?, ?, ?, ?)",
                        [name, bank, typ, bank_account.strip(), abruf],
                    )
                    # safe_table_name() kann hier nicht genutzt werden, da die IBAN noch nicht
                    # in der Accounts-Whitelist steht. Die Regex-Prüfung via _is_valid_iban()
                    # erlaubt nur [A-Z]{2}[0-9A-Z]{13,32} – kein SQL-Sonderzeichen möglich.
                    validated_iban = bank_account.strip()
                    template_row = con.execute(
                        'SELECT IBAN FROM Accounts WHERE IBAN != ? LIMIT 1',
                        [validated_iban]
                    ).fetchone()
                    if template_row:
                        con.execute(
                            f'CREATE TABLE IF NOT EXISTS "{validated_iban}" '
                            f'AS SELECT * FROM "{template_row[0]}" WHERE 1=0'
                        )
                    else:
                        con.execute(f'CREATE TABLE IF NOT EXISTS "{validated_iban}" (row_id VARCHAR)')
                    st.session_state.pop("known_ibans_cache", None)
                    st.success(f"✅ Konto **{account_key}** ({abruf}) gespeichert.")
                except Exception as e:
                    st.error(f"Fehler beim Speichern: {e}")

    # ── Laden ─────────────────────────────────────────────────────────────────
    with st.expander("🔍 Zugangsdaten anzeigen", expanded=False):
        if saved:
            account_load = st.selectbox("Konto auswählen", saved, key="load_select",
                                        format_func=lambda k: saved_df.set_index("KEY").loc[k, "LABEL"])
        else:
            account_load = st.text_input("Nutzername", placeholder="z. B. Alice_DKB_Giro", key="load_input")

        if st.button("🔍 Laden", width='stretch'):
            if not account_load:
                st.warning("Bitte einen Nutzernamen eingeben.")
            else:
                creds = load_fints_credentials(account_load)
                if creds is None:
                    st.error(f"Keine Zugangsdaten für **{account_load}** gefunden.")
                else:
                    st.success(f"✅ Zugangsdaten für **{account_load}** geladen.")
                    st.table({
                        "Feld": list(FIELD_LABELS.values()),
                        "Wert": [
                            creds.account, creds.name, creds.bank, creds.typ,
                            creds.bank_account, creds.bank_identifier,
                            creds.user_id,
                            "••••••••",
                            creds.server, creds.pid,
                        ],
                    })

    # ── Entfernen ─────────────────────────────────────────────────────────────
    with st.expander("🗑️ Konto entfernen", expanded=False):
        if not saved:
            st.info("Noch keine Zugangsdaten gespeichert.")
        else:
            account_del = st.selectbox("Konto auswählen", saved, key="del_select",
                                       format_func=lambda k: saved_df.set_index("KEY").loc[k, "LABEL"])
            st.warning(
                f"⚠️ Löscht Keyring-Eintrag für **{account_del}**. "
                "Die Transaktionsdaten in der Datenbank bleiben erhalten."
            )
            if st.button("🗑️ Löschen", type="primary", width='stretch'):
                try:
                    delete_fints_credentials(account_del)
                    st.session_state.pop("known_ibans_cache", None)
                    st.success(f"✅ Zugangsdaten für **{account_del}** gelöscht.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fehler beim Löschen: {e}")


# ── Tab: Software ─────────────────────────────────────────────────────────────
with tab_software:
    st.subheader("Paketverwaltung")

    # ── Umgebungsinfo ──────────────────────────────────────────────────────────
    with st.expander("ℹ️ Umgebung"):
        st.code(
            f"Python:  {sys.version.split()[0]}\n"
            f"Pfad:    {sys.executable}\n"
            f"pip:     {_run_pip('--version')[1].strip()}"
        )

    st.divider()

    # ── Updates prüfen ─────────────────────────────────────────────────────────
    st.markdown("#### 🔄 Updates")
    col_check, _ = st.columns([1, 1])

    with col_check:
        if st.button("🔍 Updates prüfen", width="stretch"):
            with st.spinner("Prüfe auf Updates…"):
                st.session_state["sw_outdated"] = _get_outdated()
                st.session_state["sw_checked"]  = True

    if st.session_state.get("sw_checked"):
        outdated = st.session_state.get("sw_outdated", [])
        if not outdated:
            st.success("✅ Alle Pakete sind aktuell.")
        else:
            st.info(f"**{len(outdated)}** Paket(e) können aktualisiert werden:")
            df_out = pd.DataFrame(outdated)[["name", "version", "latest_version"]]
            df_out.columns = ["Paket", "Installiert", "Verfügbar"]
            st.dataframe(df_out, width="stretch", hide_index=True)

            pkg_names = [p["name"] for p in outdated]
            selected  = st.multiselect(
                "Pakete auswählen", pkg_names, default=pkg_names, key="sw_selected"
            )

            col_sel, col_alle = st.columns(2)
            with col_sel:
                upd_sel = st.button(
                    "⬆️ Ausgewählte aktualisieren",
                    disabled=not selected,
                    width="stretch",
                    key="sw_upd_sel",
                )
            with col_alle:
                upd_all = st.button("⬆️ Alle aktualisieren", width="stretch", key="sw_upd_all")

            to_update = selected if upd_sel else (pkg_names if upd_all else [])
            if to_update:
                with st.status(f"Aktualisiere {len(to_update)} Paket(e)…", expanded=True):
                    errors = []
                    for pkg in to_update:
                        st.write(f"⏳ {pkg}…")
                        rc, _, stderr = _run_pip("install", "--upgrade", pkg)
                        if rc == 0:
                            st.write(f"✅ {pkg}")
                            _log_sw("UPDATE", pkg)
                        else:
                            st.write(f"❌ {pkg}: {stderr[:150]}")
                            errors.append(pkg)
                    _write_installed_packages()
                    st.session_state["sw_checked"] = False
                if errors:
                    st.error(f"Fehler bei: {', '.join(errors)}")
                else:
                    st.success("Alle Pakete aktualisiert.")
                st.info("Bitte App neu laden damit geänderte Pakete wirksam werden.")

    st.divider()

    # ── Neues Paket installieren ───────────────────────────────────────────────
    with st.expander("📦 Paket installieren"):
        new_pkg = st.text_input(
            "Paketname (z. B. `requests` oder `requests==2.32.0`)",
            key="sw_new_pkg",
        )
        if st.button("📥 Installieren", width="stretch", key="sw_install_btn"):
            spec = new_pkg.strip()
            if not spec:
                st.warning("Bitte einen Paketnamen eingeben.")
            elif not _valid_pkg_spec(spec):
                st.error("Ungültiger Paketname.")
            else:
                with st.spinner(f"Installiere {spec}…"):
                    rc, _, stderr = _run_pip("install", spec)
                if rc == 0:
                    _log_sw("INSTALL", spec)
                    _write_installed_packages()
                    st.success(f"✅ {spec} installiert.")
                    st.info("Bitte App neu laden damit das neue Paket verfügbar ist.")
                else:
                    st.error(f"Fehler: {stderr[:300]}")

    # ── Paket deinstallieren ───────────────────────────────────────────────────
    with st.expander("🗑️ Paket deinstallieren"):
        if st.button("🔄 Paketliste laden", key="sw_load_installed"):
            st.session_state["sw_installed"] = _get_installed()

        installed_pkgs = st.session_state.get("sw_installed")
        if installed_pkgs is None:
            st.info("Klicke auf ‚Paketliste laden' um alle installierten Pakete anzuzeigen.")
        else:
            pkg_names_all = sorted(p["name"] for p in installed_pkgs)
            pkg_to_remove = st.selectbox("Paket auswählen", pkg_names_all, key="sw_remove_sel")
            st.warning(f"⚠️ **{pkg_to_remove}** wird dauerhaft deinstalliert.")
            confirmed = st.checkbox("Ja, ich möchte dieses Paket entfernen.", key="sw_confirm_rm")
            if st.button("🗑️ Entfernen", key="sw_remove_btn", disabled=not confirmed):
                with st.spinner(f"Entferne {pkg_to_remove}…"):
                    rc, _, stderr = _run_pip("uninstall", "-y", pkg_to_remove)
                if rc == 0:
                    _log_sw("UNINSTALL", pkg_to_remove)
                    _write_installed_packages()
                    st.session_state.pop("sw_installed", None)
                    st.success(f"✅ {pkg_to_remove} entfernt.")
                else:
                    st.error(f"Fehler: {stderr[:300]}")

    st.divider()

    # ── Paket-Analyse ──────────────────────────────────────────────────────────
    st.markdown("#### 🔎 Abhängigkeitsanalyse")
    st.caption(
        "Prüft welche installierten Pakete direkt im Projekt genutzt werden, "
        "welche transitive Abhängigkeiten sind und welche möglicherweise ungenutzt sind."
    )

    if st.button("🔎 Analyse starten", key="sw_analyze_btn"):
        with st.spinner("Scanne Projektdateien und Paketmetadaten…"):
            result = _analyze_packages()
            st.session_state["sw_analysis"] = result

    analysis = st.session_state.get("sw_analysis")
    if analysis is not None:
        direct, deps, unused = analysis

        with st.expander(f"✅ Direkt genutzt ({len(direct)})", expanded=False):
            st.dataframe(pd.DataFrame(direct), width="stretch", hide_index=True)

        with st.expander(f"🔗 Transitive Abhängigkeiten ({len(deps)})", expanded=False):
            st.caption("Diese Pakete werden nicht direkt importiert, sind aber Abhängigkeiten anderer Pakete.")
            st.dataframe(pd.DataFrame(deps), width="stretch", hide_index=True)

        with st.expander(f"⚠️ Möglicherweise ungenutzt ({len(unused)})", expanded=True):
            if not unused:
                st.success("Keine ungenutzten Pakete gefunden.")
            else:
                st.warning(
                    "Diese Pakete werden weder direkt importiert noch als Abhängigkeit eines "
                    "anderen Pakets gelistet. Vor dem Deinstallieren bitte manuell prüfen."
                )
                hdr = st.columns([3, 2, 5, 2])
                for label, col in zip(["**Paket**", "**Version**", "**Module**", ""], hdr):
                    col.markdown(label)
                st.divider()
                for row in unused:
                    c1, c2, c3, c4 = st.columns([3, 2, 5, 2])
                    c1.write(row["Paket"])
                    c2.write(row["Version"])
                    c3.write(row["Module"] or "—")
                    if c4.button("🗑️ Entfernen", key=f"sw_rm_{row['Paket']}", type="primary"):
                        with st.spinner(f"Entferne {row['Paket']}…"):
                            rc, _, stderr = _run_pip("uninstall", "-y", row["Paket"])
                        if rc == 0:
                            _log_sw("UNINSTALL", row["Paket"])
                            _write_installed_packages()
                            st.session_state.pop("sw_analysis", None)
                            st.rerun()
                        else:
                            st.error(f"Fehler: {stderr[:200]}")

    st.divider()

    # ── Log & Pakete-Datei ─────────────────────────────────────────────────────
    st.markdown("#### 📋 Protokoll & Dateien")

    with st.expander("📄 Aktivitäts-Log"):
        if _LOG_FILE.exists():
            log_text = _LOG_FILE.read_text(encoding="utf-8")
            # Letzte 100 Zeilen anzeigen
            lines = log_text.splitlines()
            st.code("\n".join(lines[-100:]), language=None)
            st.download_button(
                "⬇️ Log herunterladen",
                data=log_text,
                file_name="software_log.txt",
                mime="text/plain",
            )
        else:
            st.info("Noch keine Aktivitäten protokolliert.")

    with st.expander("📦 Installierte Pakete (Snapshot)"):
        col_ref, col_dl = st.columns(2)
        with col_ref:
            if st.button("🔄 Snapshot aktualisieren", width="stretch", key="sw_snap_btn"):
                _write_installed_packages()
                _log_sw("SNAPSHOT", str(_PKG_FILE))
                st.success(f"Gespeichert: {_PKG_FILE}")
        if _PKG_FILE.exists():
            snap_text = _PKG_FILE.read_text(encoding="utf-8")
            with col_dl:
                st.download_button(
                    "⬇️ Herunterladen",
                    data=snap_text,
                    file_name="installed_packages.txt",
                    mime="text/plain",
                    width="stretch",
                )
            st.code(snap_text, language=None)
        else:
            st.info("Noch kein Snapshot vorhanden. Klicke auf ‚Snapshot aktualisieren'.")

    st.divider()

    # ── App-Reload ─────────────────────────────────────────────────────────────
    st.markdown("#### 🔄 App-Neustart")
    st.caption(
        "Ersetzt den laufenden Streamlit-Prozess durch eine neue Instanz (os.execv). "
        "Die Verbindung trennt kurz, der Browser verbindet sich neu und zeigt den Login."
    )
    if st.button("🔄 App neu starten", key="sw_reload_btn"):
        app_py = str((Path(__file__).parent / "app.py").resolve())
        os.execv(sys.executable, [sys.executable, "-m", "streamlit", "run", app_py])
