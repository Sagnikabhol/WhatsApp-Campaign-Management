"""
Dashboard module: Displays overview metrics, recent activity, and system status.
"""

from typing import Dict, Any, Optional
import pandas as pd
import altair as alt
import streamlit as st

try:
    from services.campaign_service import get_platform_kpi_summary, get_all_campaigns
except ImportError:
    get_platform_kpi_summary = None
    get_all_campaigns = None


# ==============================================================================
# DATA PROVIDERS (LIVE DATABASE WITH MOCK FALLBACK)
# ==============================================================================

def get_kpi_metrics() -> Dict[str, Any]:
    """Return platform KPI metrics: live from database if populated, otherwise mock baseline."""
    fallback_metrics = {
        "total_contacts": 12450,
        "eligible_contacts": 10820,
        "total_campaigns": 38,
        "messages_sent": 84200,
        "delivered_messages": 81674,
        "read_messages": 64522,
        "failed_messages": 2526,
        # Performance percentages
        "delivery_rate": 97.0,
        "read_rate": 79.0,
        "failure_rate": 3.0,
        "ctr_rate": 18.4,
    }

    if get_platform_kpi_summary:
        try:
            live = get_platform_kpi_summary()
            if live and (live.get("total_contacts", 0) > 0 or live.get("total_campaigns", 0) > 0):
                # Ensure percentage rates default gracefully if no messages sent yet
                if live.get("messages_sent", 0) == 0:
                    live["delivery_rate"] = fallback_metrics["delivery_rate"]
                    live["read_rate"] = fallback_metrics["read_rate"]
                    live["failure_rate"] = fallback_metrics["failure_rate"]
                    live["ctr_rate"] = fallback_metrics["ctr_rate"]
                return live
        except Exception:
            pass

    return fallback_metrics


def get_recent_campaigns_df() -> pd.DataFrame:
    """Return campaign data: live from database if available, otherwise mock data."""
    if get_all_campaigns:
        try:
            db_campaigns = get_all_campaigns(limit=20)
            if db_campaigns:
                rows = []
                for c in db_campaigns:
                    sent = c.sent_count or 0
                    delivered = c.delivered_count or 0
                    read = c.read_count or 0
                    del_rate = round(delivered / sent, 2) if sent > 0 else 0.0
                    read_rate = round(read / delivered, 2) if delivered > 0 else 0.0
                    rows.append({
                        "Campaign Name": c.name,
                        "Target Segment": c.target_segment or "All Contacts",
                        "Status": c.status.capitalize(),
                        "Sent": sent,
                        "Delivered": delivered,
                        "Read": read,
                        "Delivery Rate": del_rate,
                        "Read Rate": read_rate,
                        "Launched": c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "N/A",
                    })
                return pd.DataFrame(rows)
        except Exception:
            pass

    data = [
        {
            "Campaign Name": "Diwali Mega Sale 2026",
            "Target Segment": "High Spenders (Tier 1)",
            "Status": "Completed",
            "Sent": 18500,
            "Delivered": 18130,
            "Read": 14685,
            "Delivery Rate": 0.98,
            "Read Rate": 0.81,
            "Launched": "2026-11-03 14:00",
        },
        {
            "Campaign Name": "VIP Early Access Preview",
            "Target Segment": "VIP Loyalty Club",
            "Status": "Completed",
            "Sent": 6200,
            "Delivered": 6076,
            "Read": 5164,
            "Delivery Rate": 0.98,
            "Read Rate": 0.85,
            "Launched": "2026-11-02 10:30",
        },
        {
            "Campaign Name": "Cart Abandonment Recovery #4",
            "Target Segment": "Cart Drop-offs (48h)",
            "Status": "Active",
            "Sent": 3400,
            "Delivered": 3230,
            "Read": 2422,
            "Delivery Rate": 0.95,
            "Read Rate": 0.75,
            "Launched": "2026-11-04 09:15",
        },
        {
            "Campaign Name": "Product Feature Announcement",
            "Target Segment": "All Opted-in Users",
            "Status": "Completed",
            "Sent": 28500,
            "Delivered": 27360,
            "Read": 20520,
            "Delivery Rate": 0.96,
            "Read Rate": 0.75,
            "Launched": "2026-10-29 16:45",
        },
        {
            "Campaign Name": "Weekend Flash Promotion",
            "Target Segment": "Weekend Shoppers",
            "Status": "Completed",
            "Sent": 15600,
            "Delivered": 15132,
            "Read": 12105,
            "Delivery Rate": 0.97,
            "Read Rate": 0.80,
            "Launched": "2026-10-25 11:00",
        },
        {
            "Campaign Name": "Post-Purchase Review Request",
            "Target Segment": "Delivered Orders (7d)",
            "Status": "Active",
            "Sent": 4500,
            "Delivered": 4365,
            "Read": 3361,
            "Delivery Rate": 0.97,
            "Read Rate": 0.77,
            "Launched": "2026-11-04 12:00",
        },
        {
            "Campaign Name": "Winter Wardrobe Launch",
            "Target Segment": "Fashion Enthusiasts",
            "Status": "Scheduled",
            "Sent": 0,
            "Delivered": 0,
            "Read": 0,
            "Delivery Rate": 0.0,
            "Read Rate": 0.0,
            "Launched": "2026-11-06 18:00",
        },
    ]
    return pd.DataFrame(data)


def get_performance_trend_df() -> pd.DataFrame:
    """Return daily message volume performance data over the past 7 days."""
    dates = [
        "Oct 29", "Oct 30", "Oct 31", "Nov 01", "Nov 02", "Nov 03", "Nov 04"
    ]
    sent = [28500, 4200, 5800, 7100, 6200, 18500, 7900]
    delivered = [27360, 4074, 5626, 6887, 6076, 18130, 7595]
    read = [20520, 3180, 4390, 5370, 5164, 14685, 5783]

    rows = []
    for d, s, dl, rd in zip(dates, sent, delivered, read):
        rows.append({"Date": d, "Metric": "Sent", "Count": s})
        rows.append({"Date": d, "Metric": "Delivered", "Count": dl})
        rows.append({"Date": d, "Metric": "Read", "Count": rd})

    return pd.DataFrame(rows)


def get_delivery_breakdown_df(metrics: Dict[str, Any]) -> pd.DataFrame:
    """Return breakdown data of message delivery status."""
    delivered_unread = metrics["delivered_messages"] - metrics["read_messages"]
    return pd.DataFrame([
        {"Status": "Read", "Messages": metrics["read_messages"], "Color": "#10B981"},
        {"Status": "Delivered (Unread)", "Messages": delivered_unread, "Color": "#3B82F6"},
        {"Status": "Failed", "Messages": metrics["failed_messages"], "Color": "#EF4444"},
    ])


# ==============================================================================
# UI COMPONENTS
# ==============================================================================

def render_metric_cards(metrics: Dict[str, Any]) -> None:
    """Render top-level KPI metric cards in responsive rows."""
    st.markdown("### 📌 Platform Overview")

    # Row 1: Audience & Campaign Metrics
    col1, col2, col3 = st.columns(3)
    with col1:
        with st.container(border=True):
            st.metric(
                label="Total Contacts",
                value=f"{metrics['total_contacts']:,}",
                delta="+340 this week",
                help="Total contacts currently stored in the database.",
            )
    with col2:
        with st.container(border=True):
            opt_in_pct = (metrics['eligible_contacts'] / metrics['total_contacts']) * 100
            st.metric(
                label="Eligible Contacts",
                value=f"{metrics['eligible_contacts']:,}",
                delta=f"{opt_in_pct:.1f}% Opt-in rate",
                delta_color="normal",
                help="Contacts with valid WhatsApp numbers and active opt-in consent.",
            )
    with col3:
        with st.container(border=True):
            st.metric(
                label="Total Campaigns",
                value=f"{metrics['total_campaigns']}",
                delta="+4 this month",
                help="Total marketing and notification campaigns created.",
            )

    st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)

    # Row 2: Message Delivery Funnel Metrics
    st.markdown("### 📨 Message Funnel & Delivery")
    col4, col5, col6, col7 = st.columns(4)
    with col4:
        with st.container(border=True):
            st.metric(
                label="Messages Sent",
                value=f"{metrics['messages_sent']:,}",
                delta="+12.4% vs last week",
                help="Total broadcast messages dispatched to WhatsApp Cloud API.",
            )
    with col5:
        with st.container(border=True):
            st.metric(
                label="Delivered Messages",
                value=f"{metrics['delivered_messages']:,}",
                delta=f"{metrics['delivery_rate']}% delivery rate",
                delta_color="normal",
                help="Messages successfully delivered to the recipient's handset.",
            )
    with col6:
        with st.container(border=True):
            st.metric(
                label="Read Messages",
                value=f"{metrics['read_messages']:,}",
                delta=f"{metrics['read_rate']}% open/read rate",
                delta_color="normal",
                help="Messages opened and read by recipients (read receipts).",
            )
    with col7:
        with st.container(border=True):
            st.metric(
                label="Failed Messages",
                value=f"{metrics['failed_messages']:,}",
                delta=f"-{metrics['failure_rate']}% error rate",
                delta_color="inverse",
                help="Messages that failed due to invalid numbers, user blocking, or API limits.",
            )


def render_performance_and_stats(metrics: Dict[str, Any]) -> None:
    """Render campaign performance chart and delivery statistics side by side."""
    col_chart, col_stats = st.columns([1.6, 1.0], gap="medium")

    with col_chart:
        with st.container(border=True):
            st.markdown("#### 📊 Campaign Performance Trend")
            st.caption("Daily volume of Sent, Delivered, and Read messages across the past 7 days.")

            trend_df = get_performance_trend_df()

            color_scale = alt.Scale(
                domain=["Sent", "Delivered", "Read"],
                range=["#6366F1", "#3B82F6", "#10B981"]
            )

            chart = (
                alt.Chart(trend_df)
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=alt.X("Date:N", title="Date", axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("Count:Q", title="Message Volume"),
                    color=alt.Color("Metric:N", scale=color_scale, legend=alt.Legend(orient="top", title=None)),
                    xOffset="Metric:N",
                    tooltip=["Date:N", "Metric:N", alt.Tooltip("Count:Q", format=",d")],
                )
                .properties(height=310)
                .configure_view(strokeWidth=0)
            )

            st.altair_chart(chart, use_container_width=True)

    with col_stats:
        with st.container(border=True):
            st.markdown("#### 🎯 Delivery & Read Statistics")
            st.caption("Overall health and engagement distribution.")

            breakdown_df = get_delivery_breakdown_df(metrics)

            donut_chart = (
                alt.Chart(breakdown_df)
                .mark_arc(innerRadius=52, outerRadius=90)
                .encode(
                    theta=alt.Theta("Messages:Q"),
                    color=alt.Color(
                        "Status:N",
                        scale=alt.Scale(
                            domain=["Read", "Delivered (Unread)", "Failed"],
                            range=["#10B981", "#3B82F6", "#EF4444"]
                        ),
                        legend=alt.Legend(orient="bottom", title=None),
                    ),
                    tooltip=["Status:N", alt.Tooltip("Messages:Q", format=",d")],
                )
                .properties(height=200)
            )

            st.altair_chart(donut_chart, use_container_width=True)

            # High-level efficiency metrics
            st.markdown(
                f"""
                <div style="font-size: 0.85rem; line-height: 1.8; margin-top: 6px;">
                    <div style="display: flex; justify-content: space-between; border-bottom: 1px solid #f0f2f6; padding: 4px 0;">
                        <span style="color: #64748B;">Delivery Efficiency</span>
                        <strong style="color: #1E293B;">{metrics['delivery_rate']}%</strong>
                    </div>
                    <div style="display: flex; justify-content: space-between; border-bottom: 1px solid #f0f2f6; padding: 4px 0;">
                        <span style="color: #64748B;">Read / Engagement Rate</span>
                        <strong style="color: #10B981;">{metrics['read_rate']}%</strong>
                    </div>
                    <div style="display: flex; justify-content: space-between; border-bottom: 1px solid #f0f2f6; padding: 4px 0;">
                        <span style="color: #64748B;">Est. Click-Through Rate</span>
                        <strong style="color: #6366F1;">{metrics['ctr_rate']}%</strong>
                    </div>
                    <div style="display: flex; justify-content: space-between; padding: 4px 0;">
                        <span style="color: #64748B;">Failure / Bounce Rate</span>
                        <strong style="color: #EF4444;">{metrics['failure_rate']}%</strong>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_recent_campaigns_table() -> None:
    """Render the recent campaigns table with interactive filters and progress bars."""
    with st.container(border=True):
        st.markdown("#### 📋 Recent Campaigns")
        st.caption("Review performance and status of recent broadcast campaigns.")

        campaigns_df = get_recent_campaigns_df()

        # Filter bar
        col_filter1, col_filter2 = st.columns([1, 2])
        with col_filter1:
            status_filter = st.selectbox(
                "Filter by Status",
                options=["All", "Completed", "Active", "Scheduled"],
                index=0,
                label_visibility="collapsed",
            )
        with col_filter2:
            search_query = st.text_input(
                "Search campaigns",
                placeholder="🔍 Search campaign or segment...",
                label_visibility="collapsed",
            )

        # Apply filters
        filtered_df = campaigns_df.copy()
        if status_filter != "All":
            filtered_df = filtered_df[filtered_df["Status"] == status_filter]
        if search_query:
            query = search_query.lower()
            filtered_df = filtered_df[
                filtered_df["Campaign Name"].str.lower().str.contains(query)
                | filtered_df["Target Segment"].str.lower().str.contains(query)
            ]

        # Configure columns for modern SaaS styling
        st.dataframe(
            filtered_df,
            column_config={
                "Campaign Name": st.column_config.TextColumn("Campaign Name", width="medium"),
                "Target Segment": st.column_config.TextColumn("Audience Segment", width="medium"),
                "Status": st.column_config.TextColumn("Status", width="small"),
                "Sent": st.column_config.NumberColumn("Sent", format="%d"),
                "Delivered": st.column_config.NumberColumn("Delivered", format="%d"),
                "Read": st.column_config.NumberColumn("Read", format="%d"),
                "Delivery Rate": st.column_config.ProgressColumn(
                    "Delivery %",
                    format="%.0f%%",
                    min_value=0.0,
                    max_value=1.0,
                    width="small",
                ),
                "Read Rate": st.column_config.ProgressColumn(
                    "Read %",
                    format="%.0f%%",
                    min_value=0.0,
                    max_value=1.0,
                    width="small",
                ),
                "Launched": st.column_config.TextColumn("Launched At", width="medium"),
            },
            hide_index=True,
            use_container_width=True,
        )


# ==============================================================================
# MAIN DASHBOARD ENTRY POINT
# ==============================================================================

def render_dashboard() -> None:
    """Main rendering entry point for the Dashboard page."""
    # Header
    st.title("📊 Campaign Operations Dashboard")
    st.markdown(
        "Real-time analytics and performance monitoring across all WhatsApp broadcast campaigns."
    )
    st.write("---")

    # Fetch metrics
    metrics = get_kpi_metrics()

    # 1. KPI Cards (Total Contacts, Eligible Contacts, Total Campaigns, Sent, Delivered, Read, Failed)
    render_metric_cards(metrics)

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # 2. Campaign Performance Chart & Delivery Statistics
    render_performance_and_stats(metrics)

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # 3. Recent Campaigns Table
    render_recent_campaigns_table()
