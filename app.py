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
# AUTHENTICATION
# ─────────────────────────────────────────────
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        st.title("📊 Marketing Data Manager")
        st.divider()
        pw = st.text_input("Enter password to continue", type="password")
        if pw:
            valid_passwords = st.secrets.get("passwords", {})
            if pw in valid_passwords.values():
                st.session_state.authenticated = True
                # Store role based on which password was used
                for role, role_pw in valid_passwords.items():
                    if pw == role_pw:
                        st.session_state.role = role
                        break
                st.rerun()
            else:
                st.error("Incorrect password")
        st.stop()

check_password()


# ─────────────────────────────────────────────
# DATABASE CONNECTION
# ─────────────────────────────────────────────
@st.cache_resource
def get_engine():
    creds = st.secrets["database"]
    url = (
        f"postgresql://{creds['user']}:{quote_plus(creds['password'])}"
        f"@{creds['host']}:{creds['port']}/{creds['dbname']}"
    )
    return create_engine(url)


def test_connection(engine):
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        return str(e)


# ─────────────────────────────────────────────
# COLUMN MAPPING CONFIGS
# ─────────────────────────────────────────────
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

REQUIRED_COLUMNS = {
    "marketing_mastersheet": ["Semester ID", "Program ID", "Platform"],
    "leads_mastersheet":     ["Semester", "Date"],
    "enroll_mastersheet":    ["Semester", "Final Decision"],
}

# Keys used to detect duplicates
DEDUP_KEYS = {
    "marketing_mastersheet": ["semester_id", "program_id", "platform"],
    "leads_mastersheet":     ["email"],
    "enroll_mastersheet":    ["email_address"],
}

# Available strategies per table
STRATEGIES = {
    "marketing_mastersheet": [
        "Smart Append (detect & skip duplicates)",
        "Replace (truncate & reload)",
        "Upsert (update existing, insert new)",
    ],
    "leads_mastersheet": [
        "Smart Append (detect & skip duplicates)",
        "Replace (truncate & reload)",
    ],
    "enroll_mastersheet": [
        "Smart Append (detect & skip duplicates)",
        "Replace (truncate & reload)",
    ],
}


# ─────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────
def get_table_stats(engine):
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
    errors = []
    required = REQUIRED_COLUMNS.get(table_name, [])
    expected = list(COLUMN_MAP[table_name].keys())

    for col in required:
        if col not in df.columns:
            errors.append(f"Missing required column: **{col}**")

    matching = [c for c in df.columns if c in expected]
    match_pct = len(matching) / len(expected) * 100 if expected else 0
    if match_pct < 50:
        errors.append(
            f"Only {match_pct:.0f}% of expected columns found. "
            f"Are you sure this is the correct file for **{table_name}**?"
        )

    return errors, matching, match_pct


def clean_and_map(df, table_name):
    col_map = COLUMN_MAP[table_name]
    cols_to_keep = [c for c in df.columns if c in col_map]
    df = df[cols_to_keep].copy()
    df = df.rename(columns=col_map)

    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip()
        df[col] = df[col].replace("nan", None)
        df[col] = df[col].replace("None", None)
        df[col] = df[col].replace("", None)

    return df


# ─────────────────────────────────────────────
# DUPLICATE DETECTION
# ─────────────────────────────────────────────
def detect_duplicates(df_new, table_name, engine):
    keys = DEDUP_KEYS.get(table_name, [])
    if not keys:
        return df_new, pd.DataFrame(), 0

    available_keys = [k for k in keys if k in df_new.columns]
    if not available_keys:
        return df_new, pd.DataFrame(), 0

    key_cols_sql = ", ".join(available_keys)
    try:
        with engine.connect() as conn:
            df_existing = pd.read_sql(
                text(f"SELECT {key_cols_sql} FROM {table_name}"),
                conn,
            )
            existing_count = len(df_existing)
    except Exception:
        return df_new, pd.DataFrame(), 0

    if df_existing.empty:
        return df_new, pd.DataFrame(), 0

    def normalize(df, cols):
        df = df.copy()
        for col in cols:
            if col in df.columns and df[col].dtype == "object":
                df[col] = df[col].astype(str).str.strip().str.lower()
            if col in df.columns and pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].astype(str)
        return df

    df_new_norm = normalize(df_new, available_keys)
    df_existing_norm = normalize(df_existing, available_keys)

    merged = df_new_norm.merge(
        df_existing_norm.drop_duplicates(),
        on=available_keys,
        how="left",
        indicator=True,
    )

    is_new = merged["_merge"] == "left_only"
    df_new_only = df_new[is_new.values].reset_index(drop=True)
    df_duplicates = df_new[~is_new.values].reset_index(drop=True)

    return df_new_only, df_duplicates, existing_count


# ─────────────────────────────────────────────
# UPLOAD STRATEGIES
# ─────────────────────────────────────────────
def upload_replace(df, table_name, engine):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_name} RESTART IDENTITY"))
        df.to_sql(table_name, conn, if_exists="append", index=False)
    return len(df)


def upload_new_only(df_new_only, table_name, engine):
    if df_new_only.empty:
        return 0
    with engine.begin() as conn:
        df_new_only.to_sql(table_name, conn, if_exists="append", index=False)
    return len(df_new_only)


def upload_upsert_marketing(df, engine):
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
    st.caption(f"Logged in as: **{st.session_state.get('role', 'unknown')}**")
    if st.button("🚪 Logout"):
        st.session_state.authenticated = False
        st.session_state.role = None
        st.rerun()
    st.divider()

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

    st.subheader("Quick KPIs")
    try:
        with engine.connect() as conn:
            total_leads = conn.execute(
                text("SELECT COUNT(*) FROM leads_mastersheet")
            ).scalar()
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

    # Upload strategy — dynamic per table
    available_strategies = STRATEGIES[table_name]
    strategy = st.radio(
        "Upload strategy",
        available_strategies,
        help=(
            "**Smart Append**: Scans for duplicates BEFORE uploading. "
            "Shows you exactly which rows are new vs already in the database. "
            "Only inserts new rows.\n\n"
            "**Replace**: Deletes ALL existing rows and loads the full file. "
            "Use for weekly full exports.\n\n"
            "**Upsert** *(marketing only)*: Updates existing rows matched by "
            "semester + program + platform. Inserts new ones."
        ),
    )

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

        # File preview
        st.subheader("📄 File Preview")
        st.dataframe(df_raw.head(10), use_container_width=True)
        st.caption(f"{len(df_raw):,} rows × {len(df_raw.columns)} columns")

        # Validate columns
        errors, matching, match_pct = validate_file(df_raw, table_name)

        if errors:
            st.error("Validation errors found:")
            for err in errors:
                st.write(f"- {err}")
            st.stop()
        else:
            st.success(
                f"Validation passed — {match_pct:.0f}% columns matched "
                f"({len(matching)}/{len(COLUMN_MAP[table_name])})"
            )

        # Clean and map columns
        df_clean = clean_and_map(df_raw, table_name)

        # Column mapping details
        with st.expander("Column mapping details"):
            mapping_data = []
            for excel_col, db_col in COLUMN_MAP[table_name].items():
                found = "✅" if excel_col in df_raw.columns else "❌"
                mapping_data.append({
                    "Excel Column": excel_col,
                    "DB Column": db_col,
                    "Found": found,
                })
            st.dataframe(
                pd.DataFrame(mapping_data),
                use_container_width=True,
                hide_index=True,
            )

        st.divider()

        # ───────────────────────────────────
        # DUPLICATE ANALYSIS
        # ───────────────────────────────────
        st.subheader("🔍 Duplicate Analysis")

        with st.spinner("Scanning database for duplicates..."):
            df_new_only, df_duplicates, existing_count = detect_duplicates(
                df_clean, table_name, engine
            )

        # Show which key is used
        keys = DEDUP_KEYS.get(table_name, [])
        available_keys = [k for k in keys if k in df_clean.columns]
        st.caption(f"Matching on: `{'` + `'.join(available_keys)}`")

        # Summary metrics
        col_d1, col_d2, col_d3, col_d4 = st.columns(4)
        col_d1.metric("In Database", f"{existing_count:,}")
        col_d2.metric("In Upload", f"{len(df_clean):,}")
        col_d3.metric("🆕 New Rows", f"{len(df_new_only):,}")
        col_d4.metric("🔁 Duplicates", f"{len(df_duplicates):,}")

        # Visual bar
        if len(df_clean) > 0:
            new_pct = len(df_new_only) / len(df_clean) * 100
            dup_pct = len(df_duplicates) / len(df_clean) * 100
            st.progress(new_pct / 100)
            st.caption(f"**{new_pct:.1f}%** new rows  ·  **{dup_pct:.1f}%** duplicates")

        # Expandable duplicate details
        if not df_duplicates.empty:
            with st.expander(
                f"👀 View {len(df_duplicates):,} duplicate rows (already in database)",
                expanded=False,
            ):
                st.dataframe(df_duplicates, use_container_width=True, hide_index=True)

        if not df_new_only.empty:
            with st.expander(
                f"🆕 View {len(df_new_only):,} new rows (will be inserted)",
                expanded=False,
            ):
                st.dataframe(
                    df_new_only.head(200),
                    use_container_width=True,
                    hide_index=True,
                )
                if len(df_new_only) > 200:
                    st.caption(f"Showing first 200 of {len(df_new_only):,} new rows")

        st.divider()

        # ───────────────────────────────────
        # UPLOAD CONFIRMATION
        # ───────────────────────────────────
        if strategy.startswith("Smart Append"):
            if df_new_only.empty:
                st.info(
                    "🟡 No new rows to upload — all rows in this file "
                    "already exist in the database."
                )
            else:
                st.warning(
                    f"Ready to insert **{len(df_new_only):,}** new rows into "
                    f"`{table_name}`. **{len(df_duplicates):,}** duplicates "
                    f"will be skipped."
                )
                if st.button("✅ Upload New Rows Only", type="primary"):
                    with st.spinner("Uploading new rows..."):
                        try:
                            count = upload_new_only(df_new_only, table_name, engine)
                            st.success(
                                f"Inserted **{count:,}** new rows. "
                                f"Skipped **{len(df_duplicates):,}** duplicates."
                            )
                            st.balloons()
                        except Exception as e:
                            st.error(f"Upload failed: {e}")

        elif strategy.startswith("Replace"):
            st.warning(
                f"⚠️ This will **DELETE all {existing_count:,} existing rows** "
                f"and replace with **{len(df_clean):,}** rows from the file."
            )
            confirm_text = st.text_input(
                f"Type **{table_name}** to confirm replacement:",
                placeholder=table_name,
            )
            if st.button("🔄 Replace All Data", type="primary"):
                if confirm_text == table_name:
                    with st.spinner("Replacing table data..."):
                        try:
                            count = upload_replace(df_clean, table_name, engine)
                            st.success(f"Replaced table with **{count:,}** rows.")
                            st.balloons()
                        except Exception as e:
                            st.error(f"Upload failed: {e}")
                else:
                    st.error(f"Please type `{table_name}` exactly to confirm.")

        elif strategy.startswith("Upsert"):
            st.warning(
                f"Will upsert **{len(df_clean):,}** rows into `{table_name}`. "
                f"Existing rows (matched by semester + program + platform) "
                f"will be updated. New rows will be inserted."
            )
            if st.button("✅ Upsert Data", type="primary"):
                with st.spinner("Upserting rows..."):
                    try:
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
            try:
                sem_col = (
                    "semester_id"
                    if table_name == "marketing_mastersheet"
                    else "semester"
                )
                with engine.connect() as conn:
                    semesters = conn.execute(
                        text(
                            f"SELECT DISTINCT {sem_col} FROM {table_name} "
                            f"ORDER BY {sem_col}"
                        )
                    ).scalars().all()
                selected_sem = st.multiselect("Semester", semesters)
            except Exception:
                selected_sem = []

        with col_b:
            try:
                with engine.connect() as conn:
                    programs = conn.execute(
                        text(
                            f"SELECT DISTINCT program_id FROM {table_name} "
                            f"ORDER BY program_id"
                        )
                    ).scalars().all()
                selected_prog = st.multiselect("Program ID", programs)
            except Exception:
                selected_prog = []

    # Build query
    query = f"SELECT * FROM {table_name} WHERE 1=1"
    params = {}

    if selected_sem:
        sem_col = (
            "semester_id"
            if table_name == "marketing_mastersheet"
            else "semester"
        )
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

        csv = df_browse.to_csv(index=False)
        st.download_button(
            "📥 Download as CSV",
            csv,
            f"{table_name}_{datetime.now():%Y%m%d}.csv",
            "text/csv",
        )

    except Exception as e:
        st.error(f"Query failed: {e}")
