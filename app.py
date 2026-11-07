"""
WhatsApp Campaign Management Platform
Main Streamlit application entry point.
"""

import streamlit as st
from database.connection import init_db, check_db_connection
from modules.dashboard import render_dashboard
from modules.contacts import render_contacts
from services.whatsapp_api import whatsapp_client, WhatsAppAPIError

# Auto-initialize database schema on application startup
try:
    init_db()
except Exception as e:
    st.error(f"Failed to initialize database: {e}")

# Configure page settings
st.set_page_config(
    page_title="WhatsApp Campaign Platform",
    page_icon="📱",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for polished SaaS dashboard typography, spacing, and styling
st.markdown(
    """
    <style>
        /* Base typography & clean layout adjustments */
        .main .block-container {
            padding-top: 1.8rem;
            padding-bottom: 2.5rem;
            max-width: 1280px;
        }

        /* Metric cards custom styling */
        [data-testid="stMetricValue"] {
            font-size: 1.65rem !important;
            font-weight: 700 !important;
            color: #0F172A;
        }
        [data-testid="stMetricLabel"] {
            font-size: 0.85rem !important;
            font-weight: 600 !important;
            color: #64748B !important;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        /* Sidebar custom branding */
        [data-testid="stSidebar"] {
            background-color: #F8FAFC;
            border-right: 1px solid #E2E8F0;
        }
        .sidebar-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 0 16px 0;
            border-bottom: 1px solid #E2E8F0;
            margin-bottom: 16px;
        }
        .sidebar-brand h2 {
            font-size: 1.15rem;
            font-weight: 700;
            color: #0F172A;
            margin: 0;
        }
        .badge-pill {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 9999px;
            font-size: 0.72rem;
            font-weight: 600;
            background-color: #E0E7FF;
            color: #4338CA;
            margin-top: 4px;
        }
        .status-badge {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 10px 12px;
            background-color: #F1F5F9;
            border-radius: 8px;
            font-size: 0.78rem;
            color: #475569;
            margin-top: 24px;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: #10B981;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def render_placeholder_page(page_name: str, icon: str, description: str) -> None:
    """Render a clean placeholder UI for upcoming modules."""
    st.title(f"{icon} {page_name}")
    st.caption("Module status: Ready for implementation in subsequent phases.")
    st.write("---")

    with st.container(border=True):
        st.subheader("Component Overview")
        st.info(description)
        st.markdown(
            """
            > **Notice**: Database connectivity and WhatsApp Cloud API integrations will be attached in the next development phase.
            """
        )


def render_settings_page() -> None:
    """Render platform settings with live database status and diagnostics."""
    st.title("⚙️ Platform Settings")
    st.caption("Manage database connection, WhatsApp Cloud API keys, and environment configuration.")
    st.write("---")

    with st.container(border=True):
        st.subheader("🗄️ Database Integration Status")
        health = check_db_connection()
        if health.get("connected"):
            st.success(f"Connected to **{health.get('dialect', '').upper()}** database successfully.")
        else:
            st.error(f"Database connection error: {health.get('error', 'Unknown')}")

        col1, col2 = st.columns(2)
        with col1:
            st.write(f"**Dialect:** `{health.get('dialect', 'N/A')}`")
            st.write(f"**Tables Registered:** `{health.get('table_count', 0)}`")
        with col2:
            st.write(f"**Connection URL:** `{health.get('database_url', 'N/A')}`")
            st.write(f"**Engine Status:** `{health.get('status', 'N/A')}`")

        st.markdown("**Registered Schema Tables:**")
        tables = health.get("tables", [])
        if tables:
            st.write(", ".join([f"`{t}`" for t in sorted(tables)]))

    with st.container(border=True):
        st.subheader("📱 WhatsApp Cloud API Integration")
        creds = whatsapp_client.get_masked_credentials()
        
        mode_color = "#3B82F6" if creds["mode"] == "test" else "#10B981"
        st.markdown(
            f"""
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
                <span style="background-color: {mode_color}; color: white; padding: 3px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase;">
                    {creds['mode']} mode (default)
                </span>
                <span style="color: #64748B; font-size: 0.85rem;">• Meta Graph API {creds['api_version']}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col_w1, col_w2 = st.columns(2)
        with col_w1:
            st.write(f"**Phone Number ID:** `{creds['phone_number_id']}`")
            st.write(f"**Business Account (WABA):** `{creds['waba_id']}`")
        with col_w2:
            st.write(f"**Access Token:** `{creds['api_token']}` *(Never exposed in UI/logs)*")
            st.write(f"**API Endpoint:** `{creds['endpoint']}`")

        # Health Check
        health = whatsapp_client.check_health()
        if health.get("connected"):
            st.success(f"API Status: **Connected** ({health.get('mode')})")
        else:
            st.warning(f"API Status: **{health.get('status')}** ({health.get('error', 'Unreachable')})")

        st.markdown("---")
        st.markdown("#### 🧪 Test Dispatch to Authorized Recipient")
        st.caption("Send an approved WhatsApp template (e.g. Meta standard `hello_world`) to a single verified recipient.")

        col_t1, col_t2 = st.columns([1.5, 1])
        with col_t1:
            test_phone = st.text_input("Recipient Mobile Number", placeholder="+14155552671 or +919876543210")
        with col_t2:
            test_tpl = st.text_input("Approved Template Name", value="hello_world", help="Meta pre-approved template name")

        send_test_btn = st.button("🚀 Send Test Message", type="primary", use_container_width=False)
        if send_test_btn:
            if not test_phone:
                st.error("Please enter a valid mobile number.")
            else:
                try:
                    with st.spinner("Dispatching via WhatsApp Cloud API..."):
                        dispatch_res = whatsapp_client.send_template_message(
                            to_phone=test_phone,
                            template_name=test_tpl,
                            enforce_consent=False,  # Permitted for developer test numbers in test mode
                        )
                        st.success(f"Message dispatched successfully! WAMID: `{dispatch_res['wamid']}`")
                        st.json(dispatch_res)
                except WhatsAppAPIError as err:
                    st.error(f"API Error [{err.__class__.__name__}]: {err}")
                except Exception as ex:
                    st.error(f"Failed to dispatch: {ex}")

    with st.container(border=True):
        st.subheader("Component Overview")
        st.info("Manage WhatsApp Cloud API keys, Webhook verification tokens, database connections, and platform preferences.")
        st.markdown(
            """
            > **Notice**: Database connectivity (SQLite / SQLAlchemy) is active with automatic table creation, unique constraints, and persistent storage.
            """
        )


def main() -> None:
    # --------------------------------------------------------------------------
    # SIDEBAR NAVIGATION
    # --------------------------------------------------------------------------
    with st.sidebar:
        st.markdown(
            """
            <div class="sidebar-brand">
                <span style="font-size: 1.8rem;">📱</span>
                <div>
                    <h2>CampaignHub</h2>
                    <span class="badge-pill">WhatsApp Platform</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        nav_options = [
            "Dashboard",
            "Contacts",
            "Campaigns",
            "Schedule Campaign",
            "Analytics",
            "Settings",
        ]

        icons = {
            "Dashboard": "📊",
            "Contacts": "👥",
            "Campaigns": "📢",
            "Schedule Campaign": "⏰",
            "Analytics": "📈",
            "Settings": "⚙️",
        }

        selected_page = st.radio(
            "Main Menu",
            options=nav_options,
            format_func=lambda opt: f"{icons.get(opt, '')}  {opt}",
            index=0,
            label_visibility="collapsed",
        )

        db_health = check_db_connection()
        if db_health.get("connected"):
            dialect_name = db_health.get("dialect", "sqlite").upper()
            tbl_count = db_health.get("table_count", 0)
            st.markdown(
                f"""
                <div class="status-badge">
                    <span class="status-dot" style="background-color: #10B981;"></span>
                    <span>DB Active &bull; {dialect_name} ({tbl_count} tables)</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                """
                <div class="status-badge">
                    <span class="status-dot" style="background-color: #EF4444;"></span>
                    <span>DB Disconnected &bull; Check Logs</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # --------------------------------------------------------------------------
    # PAGE ROUTING
    # --------------------------------------------------------------------------
    if selected_page == "Dashboard":
        render_dashboard()
    elif selected_page == "Contacts":
        render_contacts()
    elif selected_page == "Campaigns":
        render_placeholder_page(
            "Campaign Management",
            "📢",
            "Draft WhatsApp templates, preview messages with dynamic tags, and launch instant broadcast campaigns.",
        )
    elif selected_page == "Schedule Campaign":
        render_placeholder_page(
            "Schedule Campaign",
            "⏰",
            "Schedule broadcast campaigns for future dates and recurring dispatches using APScheduler.",
        )
    elif selected_page == "Analytics":
        render_placeholder_page(
            "Detailed Analytics",
            "📈",
            "In-depth breakdown of link tracking, read receipts, delivery timelines, and recipient engagement.",
        )
    elif selected_page == "Settings":
        render_settings_page()


if __name__ == "__main__":
    main()
