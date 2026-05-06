import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
from datetime import datetime

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Marketing Data Manager",
    page_icon="📊",
    layout="wide",
)

# ─────────────────────────────────────────────
# DATABASE CONNECTION
# ─────────────────────────────────────────────
@st.cache_resource
def get_engine():
    """Create a cached database engine from Streamlit secrets."""
    creds = st.secrets["database"]
    url = (
        f"postgresql://{creds['user']}:{quote_plus(creds['password'])}"
        f"@{creds['host']}:{creds['port']}/{creds['dbname']}"
    )
    return create_engine(url)


def test_connection(engine):
    """Test if the database connection is alive."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        return str(e)


# ─────────────────────────────────────────────
# COLUMN MAPPING CONFIGS
# ─────────────────────────────────────────────
# Maps Excel column names → PostgreSQL column names for each table

COLUMN_MAP = {
    "marketing_mastersheet": {
        "Semester ID":          "semester_id",
        "Program":              "program",
        "Program ID":           "program_id",
        "Platform":             "platform",
        "Budget Spent":         "budget_spent",
        "Leads":                "leads",
        "Enrollments":          "enrollments",
        "Campaign Start Date":  "campaign_start_date",
        "Campaign End Date":    "campaign_end_date",
    },
    "leads_mastersheet": {
        "Program":              "program",
        "ProgramID":            "program_id",
        "First Name":           "first_name",
        "Last Name":            "last_name",
        "Country":              "country",
        "Phone":                "phone",
        "Email":                "email",
        "Lead Source":           "lead_source",
        "Lead Source Details":   "lead_source_details",
        "Lead Status":          "lead_status",
        "Unqualified Reason":   "unqualified_reason",
        "Day":                  "day",
        "Month":                "month",
        "Year":                 "year",
        "Date":                 "date",
        "Full Name":            "full_name",
        "Semester":             "semester",
    },
    "enroll_mastersheet": {
        "Full Name":                        "full_name",
        "Email Address":                    "email_address",
        "Phone Number":                     "phone_number",
        "Country of Residence":             "country_of_residence",
        "Gender":                           "gender",
        "Date of Birth":                    "date_of_birth",
        "Nationality":                      "nationality",
        "Employment Status":                "employment_status",
        "Created Date":                     "created_date",
        "Lead Source (Salesforce)":          "lead_source_salesforce",
        "Lead Source details (Salesforce)":  "lead_source_details_sf",
        "Uni Category":                     "uni_category",
        "Final Decision":                   "final_decision",
        "Semester":                         "semester",
        "Lead Source Grouping":             "lead_source_grouping",
        "Lead (Paid/Organic)":              "lead_paid_organic",
        "Program":                          "program",
        "Program ID":                       "program_id",
    },
}

# Required columns for validation (subset that must not be missing)
REQUIRED_COLUMNS = {
    "marketing_mastersheet": ["Semester ID", "Program ID", "Platform"],
    "leads_mastersheet":     ["Semester", "Date"],
    "enroll_mastersheet":    ["Semester", "Final Decision"],
}

# Dedup keys for each table (used for the REPLACE strategy)
DEDUP_KEYS = {
    "marketing_mastersheet": ["semester_id", "program_id", "platform"],
    "leads_mastersheet":     ["full_name", "email", "date", "semester", "program_id"],
    "enroll_mastersheet":    ["full_name", "email_address", "created_date", "semester", "program_id"],
}


# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────
def get_table_stats(engine):
    """Fetch row counts and last updated for all 3 tables."""
    stats = {}
    for table in COLUMN_MAP.keys():
        try:
            with engine.connect() as conn:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                last_updated = conn.execute(
                    text(f"SELECT MAX(updated_at) FROM {table}")
                ).scalar()
                stats[table] = {"rows": count, "last_updated": last_updated}
        except Exception:
            stats[table] = {"rows": "Error", "last_updated": None}
    return stats


def validate_file(df, table_name):
    """Validate that uploaded file has the required columns."""
    errors = []
    required = REQUIRED_COLUMNS.get(table_name, [])
    expected = list(COLUMN_MAP[table_name].keys())

    # Check required columns exist
    for col in required:
        if col not in df.columns:
            errors.append(f"Missing required column: **{col}**")

    # Check for completely unexpected files
    matching = [c for c in df.columns if c in expected]
    match_pct = len(matching) / len(expected) * 100 if expected else 0
    if match_pct < 50:
        errors.append(
            f"Only {match_pct:.0f}% of expected columns found. "
            f"Are you sure this is the correct file for **{table_name}**?"
        )

    return errors, matching, match_pct


def clean_and_map(df, table_name):
    """Rename columns, clean whitespace, and prepare for DB insert."""
    col_map = COLUMN_MAP[table_name]

    # Only keep columns that exist in the mapping
    cols_to_keep = [c for c in df.columns if c in col_map]
    df = df[cols_to_keep].copy()

    # Rename to DB column names
    df = df.rename(columns=col_map)

    # Strip whitespace from string columns
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip()
        df[col] = df[col].replace("nan", None)
        df[col] = df[col].replace("None", None)
        df[col] = df[col].replace("", None)

    return df


def upload_replace(df, table_name, engine):
    """
    REPLACE strategy: truncate and reload the full table.
    Safest approach for weekly full-file uploads.
    """
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_name} RESTART IDENTITY"))
        df.to_sql(table_name, conn, if_exists="append", index=False)
    return len(df)


def upload_append_dedup(df, table_name, engine):
    """
    APPEND strategy: insert new rows, then remove duplicates.
    Better for incremental daily additions.
    """
    with engine.begin() as conn:
        df.to_sql(table_name, conn, if_exists="append", index=False)

        # Remove duplicates based on dedup keys
        keys = DEDUP_KEYS.get(table_name, [])
        if keys:
            key_conditions = " AND ".join([f"a.{k} = b.{k}" for k in keys])
            # Keep the row with the lowest id (earliest insert)
            dedup_sql = f"""
                DELETE FROM {table_name} a
                USING {table_name} b
                WHERE a.id > b.id AND {key_conditions}
            """
            result = conn.execute(text(dedup_sql))
            return len(df), result.rowcount  # rows inserted, dupes removed
    return len(df), 0


def upload_upsert_marketing(df, engine):
    """
    UPSERT strategy for marketing_mastersheet only.
    Uses ON CONFLICT with the UNIQUE constraint.
    """
    inserted = 0
    with engine.begin() as conn:
        for _, row in df.iterrows():
            row_dict = {k: (None if pd.isna(v) else v) for k, v in row.items()}
            cols = ", ".join(row_dict.keys())
            placeholders = ", ".join([f":{k}" for k in row_dict.keys()])
            update_cols = ", ".join([
                f"{k} = EXCLUDED.{k}" for k in row_dict.keys()
                if k not in ("semester_id", "program_id", "platform")
            ])
            sql = f"""
                INSERT INTO marketing_mastersheet ({cols})
                VALUES ({placeholders})
                ON CONFLICT (semester_id, program_id, platform)
                DO UPDATE SET {update_cols}, updated_at = NOW()
            """
            conn.execute(text(sql), row_dict)
            inserted += 1
    return inserted


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.title("📊 Marketing Data Manager")
    st.caption("Weekly data upload portal")
    st.divider()

    # Connection status
    engine = get_engine()
    conn_status = test_connection(engine)
    if conn_status is True:
        st.success("🟢 Database connected")
    else:
        st.error(f"🔴 Connection failed: {conn_status}")
        st.stop()

    st.divider()
    page = st.radio("Navigate", [
        "📈 Dashboard",
        "📤 Upload Data",
        "🔍 Browse Tables",
    ])


# ─────────────────────────────────────────────
# PAGE: DASHBOARD
# ─────────────────────────────────────────────
if page == "📈 Dashboard":
    st.header("Database Overview")

    stats = get_table_stats(engine)

    col1, col2, col3 = st.columns(3)

    with col1:
        s = stats["marketing_mastersheet"]
        st.metric("Marketing Mastersheet", f"{s['rows']:,} rows")
        if s["last_updated"]:
            st.caption(f"Last updated: {s['last_updated']:%Y-%m-%d %H:%M}")

    with col2:
        s = stats["leads_mastersheet"]
        st.metric("Leads Mastersheet", f"{s['rows']:,} rows")
        if s["last_updated"]:
            st.caption(f"Last updated: {s['last_updated']:%Y-%m-%d %H:%M}")

    with col3:
        s = stats["enroll_mastersheet"]
        st.metric("Enroll Mastersheet", f"{s['rows']:,} rows")
        if s["last_updated"]:
            st.caption(f"Last updated: {s['last_updated']:%Y-%m-%d %H:%M}")

    st.divider()

    # Quick KPI summary
    st.subheader("Quick KPIs")
    try:
        with engine.connect() as conn:
            total_leads = conn.execute(text("SELECT COUNT(*) FROM leads_mastersheet")).scalar()
            total_enrolled = conn.execute(
                text("SELECT COUNT(*) FROM enroll_mastersheet WHERE final_decision = 'Enrolled'")
            ).scalar()
            total_submitted = conn.execute(
                text("SELECT COUNT(*) FROM enroll_mastersheet WHERE final_decision IN ('Submitted', 'Enrolled')")
            ).scalar()
            total_spend = conn.execute(
                text("SELECT COALESCE(SUM(budget_spent), 0) FROM marketing_mastersheet")
            ).scalar()

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Leads", f"{total_leads:,}")
        k2.metric("Submitted Apps", f"{total_submitted:,}")
        k3.metric("Enrollments", f"{total_enrolled:,}")
        k4.metric("Total Budget", f"${total_spend:,.2f}")

    except Exception as e:
        st.warning(f"Could not fetch KPIs: {e}")


# ─────────────────────────────────────────────
# PAGE: UPLOAD DATA
# ─────────────────────────────────────────────
elif page == "📤 Upload Data":
    st.header("Upload Weekly Data")

    # Table selection
    table_name = st.selectbox("Select target table", [
        "marketing_mastersheet",
        "leads_mastersheet",
        "enroll_mastersheet",
    ])

    # Upload strategy
    strategy = st.radio(
        "Upload strategy",
        ["Replace (truncate & reload)", "Append (add new, remove dupes)", "Upsert (marketing only)"],
        help=(
            "**Replace**: Deletes all existing rows and loads the full file. Safest for weekly full exports.\n\n"
            "**Append**: Adds new rows, then removes exact duplicates. Good for incremental additions.\n\n"
            "**Upsert**: Updates existing rows by key, inserts new ones. Only works for marketing_mastersheet."
        ),
    )

    if strategy == "Upsert (marketing only)" and table_name != "marketing_mastersheet":
        st.warning("Upsert is only available for `marketing_mastersheet` (it has a unique constraint). Choose a different strategy.")
        st.stop()

    st.divider()

    # File upload
    uploaded_file = st.file_uploader(
        f"Upload file for **{table_name}**",
        type=["xlsx", "csv"],
        help="Excel (.xlsx) or CSV (.csv) with the same column format as the original mastersheet.",
    )

    if uploaded_file:
        # Read file
        try:
            if uploaded_file.name.endswith(".csv"):
                df_raw = pd.read_csv(uploaded_file)
            else:
                df_raw = pd.read_excel(uploaded_file)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            st.stop()

        # Show raw preview
        st.subheader("File Preview (raw)")
        st.dataframe(df_raw.head(10), use_container_width=True)
        st.caption(f"{len(df_raw):,} rows × {len(df_raw.columns)} columns")

        # Validate
        errors, matching, match_pct = validate_file(df_raw, table_name)

        if errors:
            st.error("Validation errors found:")
            for err in errors:
                st.write(f"- {err}")
            st.stop()
        else:
            st.success(f"Validation passed — {match_pct:.0f}% columns matched ({len(matching)}/{len(COLUMN_MAP[table_name])})")

        # Clean and map
        df_clean = clean_and_map(df_raw, table_name)

        st.subheader("Cleaned Preview (DB-ready)")
        st.dataframe(df_clean.head(10), use_container_width=True)

        # Column comparison
        with st.expander("Column mapping details"):
            mapping_data = []
            for excel_col, db_col in COLUMN_MAP[table_name].items():
                found = "✅" if excel_col in df_raw.columns else "❌"
                mapping_data.append({
                    "Excel Column": excel_col,
                    "DB Column": db_col,
                    "Found": found,
                })
            st.dataframe(pd.DataFrame(mapping_data), use_container_width=True, hide_index=True)

        st.divider()

        # Confirm upload
        st.warning(
            f"You are about to **{strategy.split('(')[0].strip().lower()}** "
            f"**{len(df_clean):,}** rows into `{table_name}`."
        )

        col_confirm, col_cancel = st.columns([1, 3])
        with col_confirm:
            confirm = st.button("✅ Confirm Upload", type="primary")

        if confirm:
            with st.spinner("Uploading to database..."):
                try:
                    if strategy.startswith("Replace"):
                        count = upload_replace(df_clean, table_name, engine)
                        st.success(f"Replaced table with **{count:,}** rows.")

                    elif strategy.startswith("Append"):
                        count, dupes = upload_append_dedup(df_clean, table_name, engine)
                        st.success(f"Appended **{count:,}** rows, removed **{dupes:,}** duplicates.")

                    elif strategy.startswith("Upsert"):
                        count = upload_upsert_marketing(df_clean, engine)
                        st.success(f"Upserted **{count:,}** rows.")

                    st.balloons()

                except Exception as e:
                    st.error(f"Upload failed: {e}")


# ─────────────────────────────────────────────
# PAGE: BROWSE TABLES
# ─────────────────────────────────────────────
elif page == "🔍 Browse Tables":
    st.header("Browse Database Tables")

    table_name = st.selectbox("Select table", [
        "marketing_mastersheet",
        "leads_mastersheet",
        "enroll_mastersheet",
    ], key="browse_table")

    # Filters
    with st.expander("Filters", expanded=True):
        col_a, col_b = st.columns(2)

        with col_a:
            # Semester filter
            try:
                sem_col = "semester_id" if table_name == "marketing_mastersheet" else "semester"
                with engine.connect() as conn:
                    semesters = conn.execute(
                        text(f"SELECT DISTINCT {sem_col} FROM {table_name} ORDER BY {sem_col}")
                    ).scalars().all()
                selected_sem = st.multiselect("Semester", semesters)
            except Exception:
                selected_sem = []

        with col_b:
            # Program filter
            try:
                with engine.connect() as conn:
                    programs = conn.execute(
                        text(f"SELECT DISTINCT program_id FROM {table_name} ORDER BY program_id")
                    ).scalars().all()
                selected_prog = st.multiselect("Program ID", programs)
            except Exception:
                selected_prog = []

    # Build query
    query = f"SELECT * FROM {table_name} WHERE 1=1"
    params = {}

    if selected_sem:
        sem_col = "semester_id" if table_name == "marketing_mastersheet" else "semester"
        query += f" AND {sem_col} = ANY(:semesters)"
        params["semesters"] = selected_sem

    if selected_prog:
        query += " AND program_id = ANY(:programs)"
        params["programs"] = selected_prog

    query += " ORDER BY id DESC LIMIT 500"

    # Fetch and display
    try:
        with engine.connect() as conn:
            df_browse = pd.read_sql(text(query), conn, params=params)

        st.dataframe(df_browse, use_container_width=True, hide_index=True)
        st.caption(f"Showing {len(df_browse)} rows (max 500)")

        # Download button
        csv = df_browse.to_csv(index=False)
        st.download_button(
            "📥 Download as CSV",
            csv,
            f"{table_name}_{datetime.now():%Y%m%d}.csv",
            "text/csv",
        )

    except Exception as e:
        st.error(f"Query failed: {e}")
