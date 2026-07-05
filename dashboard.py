import os
import re
import django
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta, timezone

# Initialize Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "walletbot.settings")
django.setup()

from django.conf import settings
from tracker.models import Wallet, TokenBuy, MatchAlert
from tracker import helius as helius_api
from tracker.tasks import backfill_wallet_history_task

# Regular expression for Solana address validation
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Set up page configurations
st.set_page_config(
    page_title="WalletBot Console",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Manage Theme state
if "theme" not in st.session_state:
    st.session_state.theme = "dark"

def toggle_theme():
    st.session_state.theme = "light" if st.session_state.theme == "dark" else "dark"

IS_DARK = st.session_state.theme == "dark"

# Inject Custom Zinc styling
CSS = f"""
<style>
/* Hide default Streamlit headers, footers and main menus */
header[data-testid="stHeader"], #MainMenu, footer, [data-testid="stDeployButton"],
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
div[data-testid="stSidebarCollapsedControl"] {{
    display: none !important;
}}

:root {{
    --bg: {"#09090b" if IS_DARK else "#ffffff"};
    --bg-subtle: {"#0c0c0f" if IS_DARK else "#f9fafb"};
    --card: {"#0c0c0f" if IS_DARK else "#ffffff"};
    --card-hover: {"#131316" if IS_DARK else "#f4f4f5"};
    --border: {"#1e1e24" if IS_DARK else "#e4e4e7"};
    --border-subtle: {"#16161a" if IS_DARK else "#f0f0f2"};
    --text: {"#fafafa" if IS_DARK else "#09090b"};
    --text-muted: #71717a;
    --text-dim: {"#52525b" if IS_DARK else "#a1a1aa"};
    --accent: #2563eb;
    --accent-muted: #1d4ed8;
    --green: {"#22c55e" if IS_DARK else "#16a34a"};
    --green-muted: {"rgba(34,197,94,0.12)" if IS_DARK else "rgba(22,163,74,0.08)"};
    --red: {"#ef4444" if IS_DARK else "#dc2626"};
    --red-muted: {"rgba(239,68,68,0.12)" if IS_DARK else "rgba(220,38,38,0.08)"};
    --amber: {"#f59e0b" if IS_DARK else "#d97706"};
    --amber-muted: {"rgba(245,158,11,0.12)" if IS_DARK else "rgba(217,119,6,0.08)"};
    --radius: 8px;
    --shadow: {"none" if IS_DARK else "0 1px 2px 0 rgba(0, 0, 0, 0.05)"};
}}

html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"], .main, .block-container, section[data-testid="stMain"] {{
    background-color: var(--bg) !important;
    color: var(--text) !important;
    font-family: 'DM Sans', -apple-system, sans-serif !important;
}}

.block-container {{
    padding: 1.5rem 2rem 2.5rem !important;
    max-width: 1360px !important;
}}

[data-testid="stHorizontalBlock"] {{
    gap: 1.25rem !important;
}}

/* Custom component containers */
.metric-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem 1.4rem;
    box-shadow: var(--shadow);
}}
.metric-label {{
    font-size: 0.78rem;
    color: var(--text-muted);
    font-weight: 500;
}}
.metric-value {{
    font-size: 1.75rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.03em;
    margin-top: 0.1rem;
}}
.metric-subtitle {{
    font-size: 0.72rem;
    color: var(--text-dim);
    margin-top: 0.2rem;
}}

.chart-wrap {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.2rem;
    box-shadow: var(--shadow);
    margin-bottom: 1.25rem;
}}
.chart-title {{
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--text);
}}
.chart-subtitle {{
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-bottom: 1rem;
}}

.table-wrap {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.2rem;
    box-shadow: var(--shadow);
    overflow: hidden;
}}
.data-table {{
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-size: 0.8rem;
}}
.data-table th {{
    text-align: left;
    padding: 0.6rem 0.8rem;
    color: var(--text-muted);
    font-weight: 500;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    border-bottom: 1px solid var(--border);
}}
.data-table td {{
    padding: 0.65rem 0.8rem;
    color: var(--text);
    border-bottom: 1px solid var(--border-subtle);
    vertical-align: middle;
}}
.data-table tr:last-child td {{
    border-bottom: none;
}}

.badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 0.72rem;
    font-weight: 500;
}}
.badge-green {{
    color: var(--green);
    background: var(--green-muted);
}}
.badge-red {{
    color: var(--red);
    background: var(--red-muted);
}}
.badge-amber {{
    color: var(--amber);
    background: var(--amber-muted);
}}
.badge-blue {{
    color: var(--accent);
    background: rgba(37,99,235,0.1);
}}

/* Custom pills navigation */
button[data-baseweb="tab"] {{
    background: transparent !important;
    color: var(--text-muted) !important;
    font-size: 0.835rem !important;
    font-weight: 500 !important;
    padding: 0.55rem 1rem !important;
    border: 1px solid transparent !important;
    border-radius: 7px !important;
}}
button[data-baseweb="tab"][aria-selected="true"] {{
    color: var(--text) !important;
    background: var(--card) !important;
    border-color: var(--border) !important;
}}
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] {{
    display: none !important;
}}
[data-baseweb="tab-list"] {{
    gap: 4px !important;
    background: var(--bg-subtle) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    padding: 3px;
    margin-bottom: 1.5rem;
}}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# Helper function to render KPI cards
def metric_card(label, value, subtitle=None):
    subtitle_html = f'<div class="metric-subtitle">{subtitle}</div>' if subtitle else ""
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">{label}</div>
        <div class="metric-value">{value}</div>
        {subtitle_html}
    </div>
    """, unsafe_allow_html=True)

# Plotly theme configuration
PLOT_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="DM Sans, sans-serif", color="#71717a" if not IS_DARK else "#a1a1aa", size=11),
    margin=dict(l=0, r=0, t=10, b=0),
    xaxis=dict(
        gridcolor="rgba(0,0,0,0.04)" if not IS_DARK else "rgba(255,255,255,0.04)",
        zerolinecolor="rgba(0,0,0,0.04)" if not IS_DARK else "rgba(255,255,255,0.04)",
        tickfont=dict(size=10, color="#71717a"),
    ),
    yaxis=dict(
        gridcolor="rgba(0,0,0,0.04)" if not IS_DARK else "rgba(255,255,255,0.04)",
        zerolinecolor="rgba(0,0,0,0.04)" if not IS_DARK else "rgba(255,255,255,0.04)",
        tickfont=dict(size=10, color="#71717a"),
    ),
)

# Header Section
head_left, head_right = st.columns([8, 1])
with head_left:
    st.markdown(f"""
    <div style="margin-bottom: 1.5rem;">
        <h2 style="margin: 0; font-weight: 700; font-size: 1.4rem; letter-spacing: -0.02em;">◆ WalletBot Console</h2>
        <p style="margin: 0.1rem 0 0 0; font-size: 0.8rem; color: var(--text-muted);">Real-time Solana tracker system dashboard</p>
    </div>
    """, unsafe_allow_html=True)
with head_right:
    theme_btn_label = "☀️ Light" if IS_DARK else "🌙 Dark"
    st.button(theme_btn_label, on_click=toggle_theme, use_container_width=True)

# Query database data
wallets = list(Wallet.objects.all())
buys = list(TokenBuy.objects.all())
alerts = list(MatchAlert.objects.all())

total_wallets = len(wallets)
total_buys = len(buys)
total_alerts = len(alerts)

last_active = "No activity"
if buys:
    latest_buy = max(buys, key=lambda x: x.timestamp)
    last_active = latest_buy.timestamp.strftime("%Y-%m-%d %H:%M UTC")

# KPI Summary Row
kpi1, kpi2, kpi3, kpi4 = st.columns(4)
with kpi1:
    metric_card("Tracked Wallets", f"{total_wallets} / {settings.MAX_WALLETS}", "Monitored wallets capacity")
with kpi2:
    metric_card("Total Swaps Recorded", f"{total_buys:,}", f"Parsed via Raydium webhooks")
with kpi3:
    metric_card("Similarity Match Alerts", f"{total_alerts:,}", "Suspicious buy overlap alerts")
with kpi4:
    metric_card("Last Activity", last_active, "Latest transaction ingestion time")

st.write("")

# Navigation Tabs
tab_monitor, tab_wallets, tab_alerts = st.tabs(["🔍 Monitor Activity", "⚙️ Manage Wallets", "⚠️ Similarity Alerts"])

# ── TAB 1: MONITOR ACTIVITY ──────────────────────────────────────────────────
with tab_monitor:
    st.markdown("""
    <div class="chart-wrap" style="padding-bottom: 0.5rem; margin-bottom: 1.5rem;">
        <div class="chart-title">Activity Metrics</div>
        <div class="chart-subtitle">Distribution and transaction history charts</div>
    </div>
    """, unsafe_allow_html=True)

    c_left, c_right = st.columns(2)

    with c_left:
        # Chart 1: Activity by Wallet
        if buys and wallets:
            wallet_counts = []
            for w in wallets:
                count = TokenBuy.objects.filter(wallet=w).count()
                wallet_counts.append({"Wallet": w.nickname, "Buys": count})
            df_wallets = pd.DataFrame(wallet_counts)

            fig1 = px.bar(
                df_wallets,
                x="Wallet",
                y="Buys",
                color_discrete_sequence=["#2563eb"],
                labels={"Buys": "Transactions", "Wallet": "Wallet Nickname"},
            )
            fig1.update_layout(**PLOT_LAYOUT)

            st.markdown('<div class="chart-wrap"><div class="chart-title">Transaction Distribution by Wallet</div><div class="chart-subtitle">Number of swaps recorded per tracked address</div>', unsafe_allow_html=True)
            st.plotly_chart(fig1, use_container_width=True, config={"displayModeBar": False})
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.info("No swap records available to render distribution.")

    with c_right:
        # Chart 2: Daily Transaction Volume
        if buys:
            buy_dates = [b.timestamp.date() for b in buys]
            df_dates = pd.DataFrame({"Date": buy_dates})
            df_grouped = df_dates.groupby("Date").size().reset_index(name="Volume")
            df_grouped = df_grouped.sort_values("Date")

            fig2 = px.line(
                df_grouped,
                x="Date",
                y="Volume",
                color_discrete_sequence=["#2563eb"],
                labels={"Volume": "Total Swaps"},
            )
            fig2.update_layout(**PLOT_LAYOUT)

            st.markdown('<div class="chart-wrap"><div class="chart-title">Daily Swap Volume</div><div class="chart-subtitle">Aggregated daily purchases across all wallets</div>', unsafe_allow_html=True)
            st.plotly_chart(fig2, use_container_width=True, config={"displayModeBar": False})
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.info("No swap records available to render volume over time.")

    # Recent Transactions Table
    st.markdown('<div class="table-wrap">', unsafe_allow_html=True)
    st.markdown('<div class="chart-title">Recent Wallet Swaps</div>', unsafe_allow_html=True)
    st.markdown('<div class="chart-subtitle" style="margin-bottom: 0.8rem;">Latest swaps captured by Helius webhook</div>', unsafe_allow_html=True)

    if buys:
        recent_buys = sorted(buys, key=lambda x: x.timestamp, reverse=True)[:15]
        
        # Build Table rows
        table_rows = ""
        for buy in recent_buys:
            time_str = buy.timestamp.strftime("%b %d, %H:%M")
            solscan_url = f"https://solscan.io/tx/{buy.tx_signature}" if buy.tx_signature else "#"
            sig_html = f'<a href="{solscan_url}" target="_blank" style="color: var(--accent); text-decoration: none;">{buy.tx_signature[:8]}...</a>' if buy.tx_signature else "-"
            
            logo_html = ""
            if buy.logo_url:
                logo_html = f'<img src="{buy.logo_url}" width="20" height="20" style="border-radius: 50%; margin-right: 8px; vertical-align: middle;">'
            
            token_display = f'<div style="display: flex; align-items: center;">{logo_html} <span><b>{buy.name or "Unknown"}</b> ({buy.symbol or "?"})</span></div>'

            row = f"""
            <tr>
                <td>{time_str}</td>
                <td><b>{buy.wallet.nickname}</b></td>
                <td>{token_display}</td>
                <td><code>{buy.contract_address[:8]}...</code></td>
                <td>{buy.amount:,.2f}</td>
                <td><b>{buy.amount_spent:.3f} {buy.spent_symbol}</b></td>
                <td>{sig_html}</td>
            </tr>
            """
            table_rows += row

        table_html = f"""
        <table class="data-table">
            <thead>
                <tr>
                    <th>Timestamp</th>
                    <th>Wallet</th>
                    <th>Token Name</th>
                    <th>Mint Address</th>
                    <th>Tokens Bought</th>
                    <th>Spent</th>
                    <th>Signature</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
        </table>
        """
        st.markdown(table_html, unsafe_allow_html=True)
    else:
        st.markdown("<p style='font-size: 0.8rem; color: var(--text-muted);'>No swap records found in database.</p>", unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

# ── TAB 2: MANAGE WALLETS ─────────────────────────────────────────────────────
with tab_wallets:
    w_left, w_right = st.columns([5, 4])

    with w_left:
        st.markdown('<div class="table-wrap">', unsafe_allow_html=True)
        st.markdown('<div class="chart-title">Monitored Solana Wallets</div>', unsafe_allow_html=True)
        st.markdown('<div class="chart-subtitle" style="margin-bottom: 0.8rem;">Currently active target wallets list</div>', unsafe_allow_html=True)

        if wallets:
            wallet_rows = ""
            for idx, w in enumerate(wallets):
                date_str = w.date_added.strftime("%Y-%m-%d")
                wallet_rows += f"""
                <tr>
                    <td>{idx + 1}</td>
                    <td><b>{w.nickname}</b></td>
                    <td><code>{w.address}</code></td>
                    <td>{date_str}</td>
                </tr>
                """
            
            w_table_html = f"""
            <table class="data-table">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Nickname</th>
                        <th>Solana Address</th>
                        <th>Monitored Since</th>
                    </tr>
                </thead>
                <tbody>
                    {wallet_rows}
                </tbody>
            </table>
            """
            st.markdown(w_table_html, unsafe_allow_html=True)
        else:
            st.markdown("<p style='font-size: 0.8rem; color: var(--text-muted);'>Not monitoring any wallets yet.</p>", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with w_right:
        # Add Wallet Form
        st.markdown('<div class="table-wrap" style="margin-bottom: 1.25rem;">', unsafe_allow_html=True)
        st.markdown('<div class="chart-title">Add Wallet</div>', unsafe_allow_html=True)
        st.markdown('<div class="chart-subtitle">Track transactions and backfill history</div>', unsafe_allow_html=True)
        
        with st.form("add_wallet_form", clear_on_submit=True):
            address_input = st.text_input("Solana Wallet Address", placeholder="e.g. 6oQadxW73dSQ2TQ429LcSauAxEEpsQfW3saT598m9PrY")
            nickname_input = st.text_input("Wallet Nickname", placeholder="e.g. Whale Trader")
            submit_add = st.form_submit_button("Track Wallet", use_container_width=True)

            if submit_add:
                address = address_input.strip()
                nickname = nickname_input.strip()

                if not address or not nickname:
                    st.error("Please fill in both the Wallet Address and Nickname fields.")
                elif not SOLANA_ADDRESS_RE.match(address):
                    st.error("That does not seem to be a valid Solana address.")
                elif len(wallets) >= settings.MAX_WALLETS:
                    st.error(f"Limit exceeded! You can only track up to {settings.MAX_WALLETS} wallets.")
                elif Wallet.objects.filter(address=address).exists():
                    st.error("This address is already being tracked.")
                elif Wallet.objects.filter(nickname__iexact=nickname).exists():
                    st.error("The nickname chosen is already in use.")
                else:
                    try:
                        # 1. Save to Database
                        Wallet.objects.create(
                            address=address,
                            nickname=nickname,
                            added_by_telegram_id=settings.TELEGRAM_ALLOWED_USER_ID
                        )
                        # 2. Register Webhook
                        ok = helius_api.register_wallet(address)
                        
                        # 3. Trigger history backfill Celery task
                        backfill_wallet_history_task.delay(address, nickname, settings.TELEGRAM_ALLOWED_USER_ID)
                        
                        st.success(f"Successfully added '{nickname}'! Helius registration status: {'Active' if ok else 'Fallback'}. History backfill scheduled in background.")
                        # Force rerun to refresh database values
                        st.rerun()
                    except Exception as e:
                        st.error(f"An error occurred: {e}")
        st.markdown('</div>', unsafe_allow_html=True)

        # Remove Wallet Form
        st.markdown('<div class="table-wrap">', unsafe_allow_html=True)
        st.markdown('<div class="chart-title">Remove Wallet</div>', unsafe_allow_html=True)
        st.markdown('<div class="chart-subtitle">Stop tracking wallet address and delete stored history</div>', unsafe_allow_html=True)

        if wallets:
            with st.form("remove_wallet_form", clear_on_submit=True):
                target_wallet = st.selectbox("Select Wallet to Remove", options=wallets, format_func=lambda w: f"{w.nickname} ({w.address[:8]}...)")
                submit_remove = st.form_submit_button("Stop Tracking Wallet", use_container_width=True)

                if submit_remove and target_wallet:
                    try:
                        address = target_wallet.address
                        nickname = target_wallet.nickname
                        
                        # 1. Unregister Webhook
                        helius_api.unregister_wallet(address)
                        
                        # 2. Remove Wallet from DB (cascades deleting TokenBuy and MatchAlert records)
                        target_wallet.delete()
                        
                        st.success(f"Stopped tracking wallet '{nickname}' successfully.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"An error occurred: {e}")
        else:
            st.markdown("<p style='font-size: 0.8rem; color: var(--text-muted);'>No wallets to remove.</p>", unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

# ── TAB 3: SIMILARITY ALERTS ──────────────────────────────────────────────────
with tab_alerts:
    st.markdown('<div class="table-wrap">', unsafe_allow_html=True)
    st.markdown('<div class="chart-title">Token Similarity Alerts Logs</div>', unsafe_allow_html=True)
    st.markdown('<div class="chart-subtitle" style="margin-bottom: 0.8rem;">Historical records of purchases of similar tokens across wallets</div>', unsafe_allow_html=True)

    if alerts:
        sorted_alerts = sorted(alerts, key=lambda x: x.sent_at, reverse=True)[:25]
        
        alert_rows = ""
        for alert in sorted_alerts:
            time_str = alert.sent_at.strftime("%b %d, %H:%M")
            
            # Badge styles
            badge_class = "badge-blue"
            if alert.match_type == "name":
                badge_class = "badge-green"
            elif alert.match_type == "symbol":
                badge_class = "badge-amber"
            elif alert.match_type == "logo":
                badge_class = "badge-red"
                
            match_type_badge = f'<span class="badge {badge_class}">{alert.match_type.upper()}</span>'
            
            score_details = []
            if alert.name_score is not None:
                score_details.append(f"Name: {alert.name_score:.0f}%")
            if alert.symbol_score is not None:
                score_details.append(f"Symbol: {alert.symbol_score:.0f}%")
            if alert.logo_distance is not None:
                score_details.append(f"Logo Dist: {alert.logo_distance}")
                
            scores_str = " | ".join(score_details)

            row = f"""
            <tr>
                <td>{time_str}</td>
                <td><b>{alert.new_buy.wallet.nickname}</b></td>
                <td><b>{alert.new_buy.name}</b> ({alert.new_buy.symbol})</td>
                <td><b>{alert.matched_buy.wallet.nickname}</b></td>
                <td><b>{alert.matched_buy.name}</b> ({alert.matched_buy.symbol})</td>
                <td>{match_type_badge}</td>
                <td><small>{scores_str}</small></td>
            </tr>
            """
            alert_rows += row

        alerts_table_html = f"""
        <table class="data-table">
            <thead>
                <tr>
                    <th>Alert Date</th>
                    <th>New Buy Wallet</th>
                    <th>New Token</th>
                    <th>Matched Wallet</th>
                    <th>Matched Token</th>
                    <th>Match Type</th>
                    <th>Similarity Metrics</th>
                </tr>
            </thead>
            <tbody>
                {alert_rows}
            </tbody>
        </table>
        """
        st.markdown(alerts_table_html, unsafe_allow_html=True)
    else:
        st.markdown("<p style='font-size: 0.8rem; color: var(--text-muted);'>No similarity alerts generated yet.</p>", unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
