"""
Contacts module: Streamlit UI for uploading, validating, deduplicating,
displaying, downloading, and managing audience contacts with strict WhatsApp
marketing consent tracking (opt-in, opt-out, and audit trails).
"""

from pathlib import Path
from typing import Optional, List, Dict, Any
import io
import pandas as pd
import streamlit as st

from services.contact_service import (
    process_and_validate_contacts,
    save_validated_contacts,
    get_contacts_as_df,
    count_contacts,
    delete_contact,
    add_or_update_contact,
    record_opt_in,
    record_opt_out,
    record_opt_out_by_phone,
    get_consent_history,
    get_all_contacts,
)
from config.settings import BASE_DIR

SAMPLE_CSV_PATH = BASE_DIR / "data" / "sample_contacts.csv"


# ==============================================================================
# AUDIENCE KPI METRICS
# ==============================================================================

def render_audience_overview_metrics() -> None:
    """Render top summary metric cards for overall audience and marketing consent status."""
    counts = count_contacts()
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(
            label="Total Contacts",
            value=f"{counts['total']:,}",
            help="Total contacts currently stored in the platform database.",
        )
    with col2:
        st.metric(
            label="🟢 Eligible for Campaigns",
            value=f"{counts['eligible']:,}",
            help="Active contacts with verifiable opt-in consent. Only these will receive broadcast campaigns.",
        )
    with col3:
        st.metric(
            label="🔴 Ineligible Contacts",
            value=f"{counts['ineligible']:,}",
            delta=f"{counts['opted_out']} Opted-out, {counts['pending_consent']} Pending",
            delta_color="off",
            help="Contacts without verified consent or who opted out. Automatically excluded from broadcasts.",
        )
    with col4:
        st.metric(
            label="📈 Opt-In Rate",
            value=f"{counts['opt_in_rate']}%",
            help="Percentage of audience eligible for WhatsApp marketing campaigns.",
        )


def render_metric_summary(counts: dict) -> None:
    """Render summary metric cards for CSV validation results."""
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(
            label="Total Rows",
            value=f"{counts.get('total', 0):,}",
            help="Total rows detected in the uploaded CSV file.",
        )
    with col2:
        st.metric(
            label="✅ Valid Format",
            value=f"{counts.get('valid', 0):,}",
            help="E.164 compliant international mobile numbers ready for import.",
        )
    with col3:
        st.metric(
            label="⚠️ Invalid Format",
            value=f"{counts.get('invalid', 0):,}",
            help="Numbers with invalid lengths, letters, or missing country codes.",
        )
    with col4:
        st.metric(
            label="🔁 Duplicates",
            value=f"{counts.get('duplicate_total', 0):,}",
            delta=f"{counts.get('duplicate_db', 0)} in DB, {counts.get('duplicate_file', 0)} in CSV",
            delta_color="off",
            help="Numbers that appear multiple times in the file or already exist in the database.",
        )


# ==============================================================================
# CSV IMPORT & CONSENT VERIFICATION SECTION
# ==============================================================================

def render_csv_uploader_section() -> None:
    """Render the CSV upload, parsing, validation, and storage interface."""
    st.subheader("📤 Import & Validate Contact List")
    st.markdown(
        "Upload a `.csv` contact list. Mobile numbers are normalized to **E.164** and screened for duplicates. "
        "In accordance with Meta policies, marketing consent is never assumed and must be explicitly established."
    )

    # Compliance warning
    st.warning(
        "⚠️ **WhatsApp Marketing Compliance**: Never assume that uploaded phone numbers have WhatsApp marketing consent. "
        "Meta strictly requires demonstrable opt-in before sending broadcast campaigns. Contacts imported without explicit "
        "consent are recorded with **No Consent / Ineligible** status and excluded from broadcasts."
    )

    col_upload, col_sample = st.columns([3, 1], gap="medium")

    with col_upload:
        uploaded_file = st.file_uploader(
            "Choose a CSV file",
            type=["csv"],
            help="Upload a CSV with columns for Name and Mobile Number (and optional Consent).",
        )

    with col_sample:
        st.write("")
        st.write("")
        load_sample = st.button("🧪 Load Sample CSV", help="Quickly test with sample contacts from data/sample_contacts.csv")

    df_to_process: Optional[pd.DataFrame] = None
    source_name = "Uploaded File"

    if uploaded_file is not None:
        try:
            df_to_process = pd.read_csv(uploaded_file)
            source_name = uploaded_file.name
        except Exception as e:
            st.error(f"Error reading CSV file: {e}")
    elif load_sample and SAMPLE_CSV_PATH.exists():
        df_to_process = pd.read_csv(SAMPLE_CSV_PATH)
        source_name = "sample_contacts.csv"
        st.session_state["contacts_sample_loaded"] = True

    if "contacts_sample_loaded" in st.session_state and st.session_state["contacts_sample_loaded"] and df_to_process is None:
        if SAMPLE_CSV_PATH.exists():
            df_to_process = pd.read_csv(SAMPLE_CSV_PATH)
            source_name = "sample_contacts.csv"

    if df_to_process is not None:
        st.write("---")

        # Column configuration and country code options
        with st.expander("⚙️ Import Configuration & Column Mapping", expanded=False):
            col_map1, col_map2, col_opt = st.columns(3)
            cols = list(df_to_process.columns)

            with col_map1:
                name_col = st.selectbox(
                    "Name Column",
                    options=cols,
                    index=0,
                    help="Column containing the contact's name.",
                )
            with col_map2:
                default_phone_idx = 1 if len(cols) > 1 else 0
                for idx, c in enumerate(cols):
                    if any(term in c.lower() for term in ["phone", "mobile", "cell", "contact"]):
                        default_phone_idx = idx
                        break
                phone_col = st.selectbox(
                    "Mobile / Phone Column",
                    options=cols,
                    index=default_phone_idx,
                    help="Column containing mobile numbers.",
                )
            with col_opt:
                default_cc = st.text_input(
                    "Default Country Code (optional)",
                    value="",
                    placeholder="e.g. 1, 91, 44",
                    help="Used if numbers in CSV lack a leading + and are 10 digits.",
                )

        # Marketing consent collection settings
        st.markdown("#### 🛡️ Marketing Consent Declaration")
        has_explicit_optin = st.checkbox(
            "I certify that these contacts have granted explicit, verifiable opt-in consent for WhatsApp marketing messages.",
            value=False,  # STRICT DEFAULT: False
            help="Check only if you have collected documented consent (e.g. checkbox on checkout or website sign-up).",
        )

        consent_source_str = "csv_import"
        consent_proof_str = None

        if has_explicit_optin:
            col_c1, col_c2 = st.columns(2)
            with col_c1:
                consent_source_str = st.selectbox(
                    "Consent Collection Source",
                    options=[
                        "Website Opt-in Form",
                        "Checkout Agreement",
                        "In-Store QR Code Scan",
                        "Event / Webinar Registration",
                        "SMS Opt-in Keyword",
                        "Paper Form / Offline Agreement",
                    ],
                    index=0,
                )
            with col_c2:
                consent_proof_str = st.text_input(
                    "Proof / Audit Reference (optional)",
                    placeholder="e.g., Web Form Submission #9421, Terms agreed at checkout",
                    help="Documented proof supporting compliance in case of user dispute or Meta inquiry.",
                )
        else:
            st.info(
                "ℹ️ Contacts will be imported with **⏳ No Consent** status and will be "
                "**excluded from marketing campaigns** until explicit opt-in is recorded."
            )

        # Process and validate contacts
        validated_df, counts = process_and_validate_contacts(
            df_to_process,
            name_col=name_col,
            phone_col=phone_col,
            default_country_code=default_cc if default_cc else None,
            default_opt_in=has_explicit_optin,
            check_db=True,
        )

        # Show KPI Summary
        render_metric_summary(counts)

        st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

        # Filter preview display
        filter_status = st.radio(
            "Filter Preview:",
            options=[
                "All Records",
                "Eligible for Campaigns (Opted In)",
                "Ineligible (No Consent)",
                "Invalid Format",
                "Duplicates Only",
            ],
            horizontal=True,
        )

        filtered_preview = validated_df.copy()
        if filter_status == "Eligible for Campaigns (Opted In)":
            filtered_preview = filtered_preview[
                (filtered_preview["Validation Status"] == "VALID") & (filtered_preview["Consent Status"] == "OPT_IN")
            ]
        elif filter_status == "Ineligible (No Consent)":
            filtered_preview = filtered_preview[
                (filtered_preview["Validation Status"] == "VALID") & (filtered_preview["Consent Status"] != "OPT_IN")
            ]
        elif filter_status == "Invalid Format":
            filtered_preview = filtered_preview[filtered_preview["Validation Status"] == "INVALID_FORMAT"]
        elif filter_status == "Duplicates Only":
            filtered_preview = filtered_preview[
                filtered_preview["Validation Status"].isin(["DUPLICATE_IN_FILE", "ALREADY_IN_DATABASE"])
            ]

        # Display Contacts in styled table
        st.dataframe(
            filtered_preview,
            column_config={
                "Name": st.column_config.TextColumn("Contact Name", width="medium"),
                "Original Number": st.column_config.TextColumn("Raw Input", width="small"),
                "Normalized Number": st.column_config.TextColumn("E.164 Mobile", width="small"),
                "Consent Status": st.column_config.TextColumn("Consent", width="small"),
                "Campaign Eligibility": st.column_config.TextColumn("Eligibility", width="small"),
                "Validation Status": st.column_config.TextColumn("Status", width="small"),
                "Validation Reason": st.column_config.TextColumn("Reason / Details", width="medium"),
            },
            hide_index=True,
            use_container_width=True,
        )

        # Download & Action Row
        col_dl, col_save, col_clear = st.columns([1.5, 1.5, 1], gap="medium")

        valid_df_download = validated_df[validated_df["Validation Status"] == "VALID"]
        csv_buffer = io.StringIO()
        valid_df_download.to_csv(csv_buffer, index=False)
        csv_bytes = csv_buffer.getvalue().encode("utf-8")

        with col_dl:
            st.download_button(
                label="📥 Download Validated Contacts (CSV)",
                data=csv_bytes,
                file_name="validated_contacts_e164.csv",
                mime="text/csv",
                disabled=valid_df_download.empty,
                help="Export cleaned contacts with E.164 formatted phone numbers and consent states.",
                use_container_width=True,
            )

        with col_save:
            save_clicked = st.button(
                f"💾 Save {counts['valid']} Valid Contacts to Database",
                type="primary",
                disabled=(counts["valid"] == 0),
                help="Insert verified valid contacts into SQLite database and record consent records.",
                use_container_width=True,
            )

        with col_clear:
            if st.button("🔄 Reset Import", use_container_width=True):
                st.session_state.pop("contacts_sample_loaded", None)
                st.rerun()

        if save_clicked:
            with st.spinner("Saving contacts to SQLite database..."):
                res = save_validated_contacts(
                    validated_df,
                    default_opt_in=has_explicit_optin,
                    consent_source=consent_source_str,
                    consent_proof=consent_proof_str,
                )

                if res["saved"] > 0:
                    st.success(
                        f"🎉 Successfully saved **{res['saved']}** valid contacts to the database! "
                        f"({res['skipped']} duplicates skipped)."
                    )
                    st.session_state.pop("contacts_sample_loaded", None)
                    st.rerun()
                elif res["skipped"] > 0 and res["saved"] == 0:
                    st.warning(f"All {res['skipped']} valid numbers are already present in the database.")
                else:
                    st.error(f"Encountered {res['errors']} error(s) while saving contacts.")


# ==============================================================================
# DATABASE DIRECTORY & CONSENT MANAGEMENT SECTION
# ==============================================================================

def render_database_directory_section() -> None:
    """Render the SQLite contact directory table with search, consent filtering, and opt-out controls."""
    st.subheader("📚 Saved Contacts Directory")
    st.caption("View and manage audience contacts and WhatsApp marketing consent status.")

    col_search, col_filter = st.columns([2, 1])
    with col_search:
        search_term = st.text_input(
            "Search contacts",
            placeholder="🔍 Search by name, phone (+1...), email, or tags...",
            label_visibility="collapsed",
        )
    with col_filter:
        consent_filter = st.selectbox(
            "Filter Consent Status",
            options=[
                "All",
                "Eligible (Opted In)",
                "Ineligible (No Consent / Opted Out)",
                "Explicitly Opted Out",
                "Pending Consent",
            ],
            index=0,
            label_visibility="collapsed",
        )

    contacts_df = get_contacts_as_df(
        search_term=search_term if search_term else None,
        consent_filter=consent_filter if consent_filter != "All" else None,
    )

    if contacts_df.empty:
        st.info("No contacts matching criteria found in database.")
    else:
        st.dataframe(
            contacts_df,
            column_config={
                "ID": st.column_config.NumberColumn("ID", width="small"),
                "Name": st.column_config.TextColumn("Name", width="medium"),
                "Mobile Number": st.column_config.TextColumn("Mobile (E.164)", width="medium"),
                "Email": st.column_config.TextColumn("Email", width="medium"),
                "Marketing Consent": st.column_config.TextColumn("Consent", width="small"),
                "Eligible for Campaigns": st.column_config.TextColumn("Campaign Eligible", width="small"),
                "Consent Source": st.column_config.TextColumn("Consent Source", width="medium"),
                "Consent Date": st.column_config.TextColumn("Consent Date", width="medium"),
                "Status": st.column_config.TextColumn("Status", width="small"),
                "Tags": st.column_config.TextColumn("Tags", width="small"),
            },
            hide_index=True,
            use_container_width=True,
        )

        # Export DB contacts button
        csv_buffer = io.StringIO()
        contacts_df.to_csv(csv_buffer, index=False)
        st.download_button(
            label="📥 Export Filtered Contacts (CSV)",
            data=csv_buffer.getvalue().encode("utf-8"),
            file_name="contacts_export.csv",
            mime="text/csv",
            help="Download the filtered contacts list from SQLite.",
        )

    st.write("---")

    # --------------------------------------------------------------------------
    # INTERACTIVE CONSENT MANAGEMENT: RECORD OPT-OUT OR OPT-IN
    # --------------------------------------------------------------------------
    col_optout, col_history = st.columns(2, gap="large")

    all_contacts = get_all_contacts(limit=500)
    contact_options = {f"{c.name or 'Unknown'} ({c.phone_number})": c.id for c in all_contacts}

    with col_optout:
        with st.container(border=True):
            st.markdown("#### 🛡️ Record Consent Change / Opt-Out")
            st.caption("Log unsubscribe requests (e.g. STOP) or record verified opt-in consent.")

            if not contact_options:
                st.info("Add contacts to manage consent.")
            else:
                selected_contact_label = st.selectbox(
                    "Select Contact",
                    options=list(contact_options.keys()),
                    key="consent_mgr_contact",
                )
                selected_contact_id = contact_options[selected_contact_label]

                action_type = st.radio(
                    "Consent Action:",
                    options=["⛔ Record Opt-Out Request (STOP / Unsubscribe)", "✅ Record Explicit Opt-In (Grant Consent)"],
                    key="consent_mgr_action",
                )

                consent_src = st.selectbox(
                    "Channel / Source",
                    options=[
                        "WhatsApp 'STOP' Keyword",
                        "Customer Support Request",
                        "Unsubscribe Link Click",
                        "Website Form Opt-In",
                        "Manual Administrator Update",
                    ],
                    key="consent_mgr_src",
                )

                consent_notes = st.text_input(
                    "Reason / Audit Notes",
                    placeholder="e.g. Recipient replied STOP to broadcast, Requested removal",
                    key="consent_mgr_notes",
                )

                if st.button("Apply Consent Change", type="primary", use_container_width=True):
                    if "Opt-Out" in action_type:
                        record_opt_out(
                            contact_id=selected_contact_id,
                            source=consent_src,
                            proof="Opt-out logged in platform",
                            notes=consent_notes,
                        )
                        st.success(f"Successfully recorded Opt-Out for {selected_contact_label}. Excluded from campaigns.")
                    else:
                        record_opt_in(
                            contact_id=selected_contact_id,
                            source=consent_src,
                            proof="Opt-in verified",
                            notes=consent_notes,
                        )
                        st.success(f"Successfully recorded Opt-In for {selected_contact_label}. Now eligible for campaigns.")
                    st.rerun()

    # --------------------------------------------------------------------------
    # CONSENT HISTORY AUDIT TRAIL
    # --------------------------------------------------------------------------
    with col_history:
        with st.container(border=True):
            st.markdown("#### 📜 Consent Audit Trail")
            st.caption("Chronological verification history for compliance audits.")

            if not contact_options:
                st.info("No contacts available.")
            else:
                history_contact_label = st.selectbox(
                    "Inspect Contact History",
                    options=list(contact_options.keys()),
                    key="history_contact_select",
                )
                history_contact_id = contact_options[history_contact_label]

                history = get_consent_history(history_contact_id)
                if not history:
                    st.info("No consent events recorded for this contact yet.")
                else:
                    history_df = pd.DataFrame(history)
                    st.dataframe(
                        history_df,
                        column_config={
                            "timestamp": st.column_config.TextColumn("Date & Time", width="medium"),
                            "status": st.column_config.TextColumn("Status", width="small"),
                            "source": st.column_config.TextColumn("Source", width="medium"),
                            "proof": st.column_config.TextColumn("Proof Reference", width="medium"),
                            "notes": st.column_config.TextColumn("Notes", width="medium"),
                        },
                        hide_index=True,
                        use_container_width=True,
                    )


# ==============================================================================
# MAIN PAGE ENTRY POINT
# ==============================================================================

def render_contacts() -> None:
    """Main rendering entry point for the Contacts Management page."""
    st.title("👥 Contact & Consent Management")
    st.markdown("Manage WhatsApp audience lists, opt-in consent compliance, and unsubscribe requests.")
    st.write("---")

    # High-level audience overview (Eligible vs Ineligible counts)
    render_audience_overview_metrics()

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    tab_directory, tab_import = st.tabs(["🗃️ Contact Directory & Consent", "📥 Import & Validate CSV"])

    with tab_directory:
        render_database_directory_section()

    with tab_import:
        render_csv_uploader_section()
