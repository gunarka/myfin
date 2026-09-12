"""
db_schema.py
Zentrale Schema-Definitionen für MyFin. Keine Streamlit-Abhängigkeit.

Normalisierung (Gruppen/Kategorien):
  "groups"     – group_id PK, name UNIQUE
  "categories" – category_id PK, group_id, name, UNIQUE(group_id, name)
Transaktions-/recurring-/oneoff-Tabellen speichern nur noch "category_id"
(FK, kein Textfeld mehr). "inflation" speichert "group_id" statt Textname.

Bewusst OHNE FOREIGN-KEY-Constraints: DuckDB erlaubt zwar FKs, aber sie
erschweren ALTER TABLE/DROP COLUMN-Migrationen unnötig. Referenzintegrität
wird ausschließlich über get_or_create_category_id()/get_or_create_group_id()
sichergestellt (Single-User-Betrieb, kein gleichzeitiger Schreibzugriff).

Lesezugriff auf Gruppe/Kategorie im Klartext erfolgt über Views
("{iban}_v", "recurring_v", "oneoff_v", "inflation_v"), die category_id/
group_id gegen die normalisierten Tabellen auflösen. Schreibzugriffe
(INSERT/UPDATE) erfolgen weiterhin auf die Basistabellen mit der ID.
"""

# ── DDL-Bausteine ──────────────────────────────────────────────────────────

ACCOUNTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "Accounts" (
    "Person" TEXT NOT NULL,
    "Bank"   TEXT NOT NULL,
    "Konto"  TEXT NOT NULL,
    "IBAN"   TEXT NOT NULL UNIQUE,
    "Abruf"  TEXT NOT NULL DEFAULT 'FinTS'
)
"""

GROUPS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "groups" (
    "group_id" INTEGER PRIMARY KEY DEFAULT nextval('group_id_seq'),
    "name"     TEXT NOT NULL UNIQUE
)
"""

CATEGORIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "categories" (
    "category_id" INTEGER PRIMARY KEY DEFAULT nextval('category_id_seq'),
    "group_id"    INTEGER NOT NULL,
    "name"        TEXT NOT NULL,
    UNIQUE ("group_id", "name")
)
"""

RECURRING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "recurring" (
    "forecast_id"   INTEGER PRIMARY KEY DEFAULT nextval('forecast_id_seq'),
    "applicant"     TEXT,
    "amount"        DOUBLE NOT NULL,
    "category_id"   INTEGER,
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
"""

ONEOFF_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "oneoff" (
    "oneoff_id"   INTEGER PRIMARY KEY DEFAULT nextval('oneoff_id_seq'),
    "applicant"   TEXT,
    "amount"      DOUBLE NOT NULL,
    "category_id" INTEGER,
    "relation"    TEXT,
    "context"     TEXT,
    "iban"        TEXT,
    "event_date"  DATE NOT NULL,
    "note"        TEXT
)
"""

SCENARIOS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "scenarios" (
    "name"        TEXT PRIMARY KEY,
    "params_json" TEXT NOT NULL,
    "created_at"  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

INFLATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "inflation" (
    "group_id"   INTEGER PRIMARY KEY,
    "annual_pct" DOUBLE NOT NULL DEFAULT 0.0
)
"""

PENSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "pension_plans" (
    "plan_id"         INTEGER PRIMARY KEY DEFAULT nextval('plan_id_seq'),
    "person"          TEXT,
    "name"            TEXT,
    "typ"             TEXT NOT NULL,
    "anbieter"        TEXT,
    "monatl_beitrag"  DOUBLE NOT NULL DEFAULT 0.0,
    "aktueller_wert"  DOUBLE NOT NULL DEFAULT 0.0,
    "erwartete_rente" DOUBLE NOT NULL DEFAULT 0.0,
    "rendite_pct"     DOUBLE NOT NULL DEFAULT 0.0,
    "rentenbeginn"    DATE,
    "status"          TEXT NOT NULL DEFAULT 'aktiv',
    "note"            TEXT
)
"""

PENSION_INCOME_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "pension_income" (
    "income_id"             INTEGER PRIMARY KEY DEFAULT nextval('income_id_seq'),
    "person"                TEXT NOT NULL,
    "jahr"                  INTEGER NOT NULL,
    "individuelles_entgelt" DOUBLE NOT NULL DEFAULT 0.0,
    "durchschnittsentgelt"  DOUBLE NOT NULL DEFAULT 0.0
)
"""

INFLATION_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "inflation_history" (
    "category_code" TEXT NOT NULL,
    "category"      TEXT NOT NULL,
    "group_code"    TEXT,
    "group_label"   TEXT,
    "level"         INTEGER,
    "date"          DATE NOT NULL,
    "index_value"   DOUBLE,
    "yoy_pct"       DOUBLE,
    PRIMARY KEY ("category_code", "date")
)
"""

TRANSACTION_COLUMNS: dict[str, str] = {
    "row_id":               "INTEGER",
    "date":                 "DATE",
    "date_year":            "INTEGER",
    "date_month":           "INTEGER",
    "amount":               "DOUBLE",
    "saldo":                "DOUBLE",
    "applicant":            "TEXT",
    "applicant_name":       "TEXT",
    "applicant_iban":       "TEXT",
    "location":             "TEXT",
    "purpose":              "TEXT",
    "posting_text":         "TEXT",
    "entry_date":           "DATE",
    "guessed_entry_date":   "DATE",
    "category_id":          "INTEGER",
    "context":              "TEXT",
    "relation":             "TEXT",
    "note":                 "TEXT",
    "new_entry":            "BOOLEAN",
    "special":              "BOOLEAN",
    "bank_reference":       "TEXT",
    "end_to_end_reference": "TEXT",
}


# ── Introspektions-Helfer ─────────────────────────────────────────────────

def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0] > 0


def _table_columns(con, name: str) -> set[str]:
    return {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [name]
        ).fetchall()
    }


# ── Normalisierte Gruppen/Kategorien: Anlage + Lookup ─────────────────────

def get_or_create_group_id(con, name: str) -> int:
    name = name.strip()
    row = con.execute('SELECT "group_id" FROM "groups" WHERE "name" = ?', [name]).fetchone()
    if row:
        return row[0]
    return con.execute(
        'INSERT INTO "groups" ("name") VALUES (?) RETURNING "group_id"', [name]
    ).fetchone()[0]


def get_or_create_category_id(con, group_name: str | None, category_name: str | None) -> int | None:
    """
    Löst eine Gruppe/Kategorie-Textkombination in ihre category_id auf,
    legt Gruppe und/oder Kategorie bei Bedarf neu an. Gibt None zurück,
    wenn Gruppe oder Kategorie leer/None sind (= keine Zuordnung).
    """
    if not group_name or not category_name:
        return None
    group_name = group_name.strip()
    category_name = category_name.strip()
    if not group_name or not category_name:
        return None
    gid = get_or_create_group_id(con, group_name)
    row = con.execute(
        'SELECT "category_id" FROM "categories" WHERE "group_id" = ? AND "name" = ?',
        [gid, category_name],
    ).fetchone()
    if row:
        return row[0]
    return con.execute(
        'INSERT INTO "categories" ("group_id", "name") VALUES (?, ?) RETURNING "category_id"',
        [gid, category_name],
    ).fetchone()[0]


def _ensure_categories_tables(con) -> None:
    """
    Legt 'groups' und 'categories' normalisiert an. Erkennt eine ALTE,
    kombinierte categories-Tabelle (Spalten "group"/"category" als TEXT,
    aus einer früheren MyFin-Version) und migriert deren Inhalt verlustfrei
    in die normalisierten Tabellen, bevor die neue Struktur angelegt wird.
    Idempotent.
    """
    con.execute("CREATE SEQUENCE IF NOT EXISTS group_id_seq START 1")
    con.execute("CREATE SEQUENCE IF NOT EXISTS category_id_seq START 1")

    legacy_rows = None
    if _table_exists(con, "categories"):
        _cols = _table_columns(con, "categories")
        if "category_id" not in _cols and "group" in _cols and "category" in _cols:
            legacy_rows = con.execute('SELECT "group", "category" FROM "categories"').fetchall()
            con.execute('ALTER TABLE "categories" RENAME TO "categories_legacy_migration"')

    con.execute(GROUPS_TABLE_SQL)
    con.execute(CATEGORIES_TABLE_SQL)

    if legacy_rows:
        for grp, cat in legacy_rows:
            if grp and cat:
                get_or_create_category_id(con, grp, cat)
        con.execute('DROP TABLE "categories_legacy_migration"')


def _migrate_named_table_categories(con, table: str) -> None:
    """
    Migriert group/category TEXT-Spalten einer Tabelle (Transaktionstabelle,
    'recurring' oder 'oneoff') verlustfrei in category_id, falls die Tabelle
    noch im alten Format vorliegt. Stellt außerdem sicher, dass die Spalte
    category_id existiert. Idempotent.
    """
    cols = _table_columns(con, table)
    if "category_id" not in cols:
        con.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "category_id" INTEGER')
        cols.add("category_id")

    if "group" in cols and "category" in cols:
        combos = con.execute(
            f'SELECT DISTINCT "group", "category" FROM "{table}" '
            f'WHERE "group" IS NOT NULL AND "category" IS NOT NULL'
        ).fetchall()
        for grp, cat in combos:
            cid = get_or_create_category_id(con, grp, cat)
            con.execute(
                f'UPDATE "{table}" SET "category_id" = ? '
                f'WHERE "group" = ? AND "category" = ? AND "category_id" IS NULL',
                [cid, grp, cat],
            )
        con.execute(f'ALTER TABLE "{table}" DROP COLUMN "group"')
        con.execute(f'ALTER TABLE "{table}" DROP COLUMN "category"')


def _migrate_inflation_table(con) -> None:
    """Migriert 'inflation' von group TEXT PRIMARY KEY zu group_id INTEGER PRIMARY KEY."""
    if not _table_exists(con, "inflation"):
        return
    cols = _table_columns(con, "inflation")
    if "group_id" in cols or "group" not in cols:
        return  # bereits migriert bzw. unerwarteter Zustand
    rows = con.execute('SELECT "group", "annual_pct" FROM "inflation"').fetchall()
    con.execute('ALTER TABLE "inflation" RENAME TO "inflation_legacy_migration"')
    con.execute(INFLATION_TABLE_SQL)
    for grp, pct in rows:
        if grp:
            gid = get_or_create_group_id(con, grp)
            con.execute(
                'INSERT OR REPLACE INTO "inflation" ("group_id", "annual_pct") VALUES (?, ?)',
                [gid, pct],
            )
    con.execute('DROP TABLE "inflation_legacy_migration"')


def _ensure_forecast_category_views(con) -> None:
    """Views recurring_v/oneoff_v/inflation_v mit aufgelöstem Gruppe/Kategorie-Text."""
    con.execute('''
        CREATE OR REPLACE VIEW "recurring_v" AS
        SELECT r.* EXCLUDE ("category_id"),
               c."name" AS "category",
               g."name" AS "group"
        FROM "recurring" r
        LEFT JOIN "categories" c ON r."category_id" = c."category_id"
        LEFT JOIN "groups" g ON c."group_id" = g."group_id"
    ''')
    con.execute('''
        CREATE OR REPLACE VIEW "oneoff_v" AS
        SELECT o.* EXCLUDE ("category_id"),
               c."name" AS "category",
               g."name" AS "group"
        FROM "oneoff" o
        LEFT JOIN "categories" c ON o."category_id" = c."category_id"
        LEFT JOIN "groups" g ON c."group_id" = g."group_id"
    ''')
    con.execute('''
        CREATE OR REPLACE VIEW "inflation_v" AS
        SELECT g."name" AS "group", i."annual_pct" AS "annual_pct"
        FROM "inflation" i
        JOIN "groups" g ON i."group_id" = g."group_id"
    ''')


def ensure_transaction_view(con, iban: str) -> None:
    """Erstellt/aktualisiert die Lese-View '{iban}_v' mit aufgelöster group/category."""
    con.execute(f'''
        CREATE OR REPLACE VIEW "{iban}_v" AS
        SELECT t.* EXCLUDE ("category_id"),
               c."name" AS "category",
               g."name" AS "group"
        FROM "{iban}" t
        LEFT JOIN "categories" c ON t."category_id" = c."category_id"
        LEFT JOIN "groups" g ON c."group_id" = g."group_id"
    ''')


def _migrate_inflation_history_table(con) -> None:
    """
    Migriert 'inflation_history' auf das erweiterte Schema mit category_code
    (stabiler Schlüssel statt Label) und Gruppierungs-Spalten (group_code/
    group_label/level) für COICOP-Hierarchien. Bestehende Zeilen werden mit
    category_code = category übernommen (Gruppierung bleibt dabei leer,
    lässt sich durch Re-Import mit dem neuen Destatis-Parser nachtragen).
    Idempotent.
    """
    if not _table_exists(con, "inflation_history"):
        return
    cols = _table_columns(con, "inflation_history")
    if "category_code" in cols:
        return
    rows = con.execute(
        'SELECT "category", "date", "index_value", "yoy_pct" FROM "inflation_history"'
    ).fetchall()
    con.execute('ALTER TABLE "inflation_history" RENAME TO "inflation_history_legacy_migration"')
    con.execute(INFLATION_HISTORY_TABLE_SQL)
    for cat, dt, idx, yoy in rows:
        con.execute(
            'INSERT OR REPLACE INTO "inflation_history" '
            '("category_code", "category", "date", "index_value", "yoy_pct") '
            'VALUES (?, ?, ?, ?, ?)',
            [cat, cat, dt, idx, yoy],
        )
    con.execute('DROP TABLE "inflation_history_legacy_migration"')


def _migrate_pension_plans_table(con) -> None:
    """Ergänzt die Spalte "name" (Bausteinname) in bereits bestehenden
    pension_plans-Tabellen älterer Versionen. Idempotent."""
    if not _table_exists(con, "pension_plans"):
        return
    con.execute('ALTER TABLE "pension_plans" ADD COLUMN IF NOT EXISTS "name" TEXT')


# ── Öffentliche Bootstrap-Funktionen ───────────────────────────────────────

def ensure_core_tables(con) -> None:
    """
    Legt alle Kern-Tabellen normalisiert an (inkl. groups/categories) und
    migriert automatisch aus einem evtl. vorhandenen alten Schema
    (kombinierte categories-Tabelle, group-TEXT in recurring/oneoff/inflation).
    Idempotent, verändert keine fachlichen Werte.
    """
    con.execute(ACCOUNTS_TABLE_SQL)
    _ensure_categories_tables(con)

    con.execute("CREATE SEQUENCE IF NOT EXISTS forecast_id_seq START 1")
    con.execute(RECURRING_TABLE_SQL)
    con.execute("CREATE SEQUENCE IF NOT EXISTS oneoff_id_seq START 1")
    con.execute(ONEOFF_TABLE_SQL)
    con.execute(SCENARIOS_TABLE_SQL)
    con.execute(INFLATION_TABLE_SQL)

    con.execute("CREATE SEQUENCE IF NOT EXISTS plan_id_seq START 1")
    con.execute(PENSION_TABLE_SQL)
    _migrate_pension_plans_table(con)
    con.execute("CREATE SEQUENCE IF NOT EXISTS income_id_seq START 1")
    con.execute(PENSION_INCOME_TABLE_SQL)
    con.execute(INFLATION_HISTORY_TABLE_SQL)
    _migrate_inflation_history_table(con)

    _migrate_named_table_categories(con, "recurring")
    _migrate_named_table_categories(con, "oneoff")
    _migrate_inflation_table(con)
    _ensure_forecast_category_views(con)


def create_transaction_table(con, iban: str) -> None:
    """
    Legt eine neue Transaktionstabelle für `iban` an – IMMER mit dem
    vollständigen, normalisierten Schema (category_id statt group/category-
    Text) – sowie die zugehörige Lese-View "{iban}_v".
    `iban` muss bereits validiert sein (siehe safe_table_name / _is_valid_iban).
    """
    cols_sql = ", ".join(f'"{c}" {t}' for c, t in TRANSACTION_COLUMNS.items())
    con.execute(f'CREATE TABLE IF NOT EXISTS "{iban}" ({cols_sql})')
    ensure_transaction_view(con, iban)


def migrate_transaction_tables(con) -> None:
    """
    Gleicht alle bestehenden Transaktionstabellen gegen TRANSACTION_COLUMNS ab,
    migriert group/category-Text (falls noch vorhanden) verlustfrei in
    category_id und stellt die zugehörige "{iban}_v"-View her.
    Rein additiv/migrierend – bestehende fachliche Werte werden nicht
    verändert, nur ihre Speicherform (Text -> ID) für group/category.
    """
    ibans = [r[0] for r in con.execute('SELECT "IBAN" FROM "Accounts"').fetchall()]
    for iban in ibans:
        _migrate_named_table_categories(con, iban)
        for col_name, col_type in TRANSACTION_COLUMNS.items():
            con.execute(f'ALTER TABLE "{iban}" ADD COLUMN IF NOT EXISTS "{col_name}" {col_type}')
        ensure_transaction_view(con, iban)
