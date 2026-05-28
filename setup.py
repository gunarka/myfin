#!/usr/bin/env python3
"""
setup.py
Ersteinrichtung nach dem Klonen: Legt .data/-Verzeichnis und leere
Datenbanktabellen an. Der Keyring wird beim ersten App-Start erzeugt,
sobald ein Master-Passwort eingegeben wird.

Ausführen:  python setup.py
"""
from pathlib import Path
import sys

DATA_DIR = Path(__file__).parent / ".data"
DB_PATH  = DATA_DIR / "bookings.duckdb"


def _create_tables(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS Accounts (
            "Person" TEXT NOT NULL,
            "Bank"   TEXT NOT NULL,
            "Konto"  TEXT NOT NULL,
            "IBAN"   TEXT NOT NULL UNIQUE,
            "Abruf"  TEXT NOT NULL DEFAULT 'FinTS'
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            "group"    TEXT NOT NULL,
            "category" TEXT NOT NULL,
            UNIQUE ("group", "category")
        )
    """)
    con.execute("CREATE SEQUENCE IF NOT EXISTS forecast_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS recurring (
            "forecast_id"   INTEGER PRIMARY KEY DEFAULT nextval('forecast_id_seq'),
            "applicant"     TEXT,
            "amount"        DOUBLE NOT NULL,
            "group"         TEXT,
            "category"      TEXT,
            "relation"      TEXT,
            "context"       TEXT,
            "iban"          TEXT,
            "interval_type" TEXT NOT NULL,
            "interval_num"  INTEGER NOT NULL DEFAULT 1,
            "start_date"    DATE NOT NULL,
            "end_date"      DATE,
            "status"        TEXT NOT NULL DEFAULT 'aktiv',
            "variability"   DOUBLE NOT NULL DEFAULT 10.0,
            "note"          TEXT
        )
    """)
    con.execute("CREATE SEQUENCE IF NOT EXISTS oneoff_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS oneoff (
            "oneoff_id"  INTEGER PRIMARY KEY DEFAULT nextval('oneoff_id_seq'),
            "applicant"  TEXT,
            "amount"     DOUBLE NOT NULL,
            "group"      TEXT,
            "category"   TEXT,
            "relation"   TEXT,
            "context"    TEXT,
            "iban"       TEXT,
            "event_date" DATE NOT NULL,
            "note"       TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scenarios (
            "name"        TEXT PRIMARY KEY,
            "params_json" TEXT NOT NULL,
            "created_at"  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS inflation (
            "group"      TEXT PRIMARY KEY,
            "annual_pct" DOUBLE NOT NULL DEFAULT 0.0
        )
    """)


def main() -> int:
    try:
        import duckdb
    except ImportError:
        print("FEHLER: duckdb nicht gefunden.")
        print("Bitte zuerst installieren:  pip install -r requirements.txt")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Verzeichnis  {DATA_DIR}")

    con = duckdb.connect(str(DB_PATH))
    _create_tables(con)
    con.close()
    print(f"[OK] Datenbank    {DB_PATH}")

    print("""
Nächste Schritte:
  1. Virtuelle Umgebung erstellen:
       python -m venv .venv
  2. Pakete installieren:
       .venv/bin/pip install -r requirements.txt
  3. App starten:
       streamlit run app.py
  4. Beim ersten Start wird ein Master-Passwort abgefragt –
     dieses Passwort verschlüsselt den Keyring (.data/keyring.cfg).
     Bitte sicher aufbewahren; ohne es sind die Zugangsdaten nicht
     wiederherstellbar.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
