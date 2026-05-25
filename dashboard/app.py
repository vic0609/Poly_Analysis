"""Streamlit dashboard — real-time view of edge signals, arb, and whale activity."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db.database import init_db, get_db
from db.models import (
    Market, MarketSnapshot, EdgeSignal, ArbitrageOpportunity,
    WhaleTrade, WhaleWallet, SentimentRecord, LeaderboardEntry, Position
)
from analysis.signals import get_top_signals, get_top_arbitrage, get_recent_whale_activity

# ─── Page Config ──────────────────────────────────────────────
st.set_page_config(
    page_title="Polymarket Edge Monitor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Init DB ──────────────────────────────────────────────────
init_db()

# ─── Sidebar ──────────────────────────────────────────────────
st.sidebar.title("📊 Polymarket Edge")
page = st.sidebar.radio(
    "Navigate",
    ["Positions & P&L", "Edge Signals", "Arbitrage", "Whale Tracker", "Market Browser", "Sentiment", "Raw DB"],
)
auto_refresh = st.sidebar.checkbox("Auto-refresh (30s)", value=True)
min_edge_score = st.sidebar.slider("Min Edge Score", 0, 100, 25)

if auto_refresh:
    time.sleep(0.1)
    st.rerun() if st.sidebar.button("Refresh Now") else None

st.sidebar.markdown("---")
st.sidebar.caption(f"Last updated: {datetime.utcnow().strftime('%H:%M:%S UTC')}")


# ─── Helper ───────────────────────────────────────────────────
def df_from_list(data: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(data) if data else pd.DataFrame()


# ═══════════════════════════════════════════════════════════════
# PAGE: Positions & P&L
# ═══════════════════════════════════════════════════════════════
if page == "Positions & P&L":
    st.title("💼 Positions & P&L")

    # ── Refresh interval selector (outside fragment — controls run_every) ────
    _INTERVALS = {"10s": 10, "30s": 30, "60s": 60, "Off": None}
    rc1, rc2, _ = st.columns([1, 1, 4])
    chosen_interval = rc1.selectbox(
        "Auto-refresh", list(_INTERVALS.keys()), index=1, key="pos_refresh_interval"
    )
    interval = _INTERVALS[chosen_interval]

    @st.fragment(run_every=1)
    def _refresh_countdown():
        last = st.session_state.get("pos_last_refresh")
        if interval is None or last is None:
            st.caption("Auto-refresh: Off")
            return
        elapsed = time.time() - last
        remaining = max(0.0, interval - elapsed)
        pct = remaining / interval
        st.progress(pct, text=f"Next refresh in **{remaining:.0f}s** (every {chosen_interval})")

    _refresh_countdown()

    @st.fragment(run_every=interval)
    def _positions_view():
        if st.button("↻ Refresh Now", key="pos_refresh_btn"):
            pass  # button click reruns this fragment; fall through to reload data
        st.session_state["pos_last_refresh"] = time.time()
        # ── Load data (all attribute access inside session scope) ─
        from sqlalchemy import func as _func
        rows = []
        with get_db() as db:
            all_positions = (
                db.query(Position, Market)
                .outerjoin(Market, Position.market_id == Market.id)
                .order_by(Position.opened_at.desc())
                .all()
            )
            open_market_ids = [p.market_id for p, _ in all_positions if p.status == "open"]
            # Latest snapshot price per open market via GROUP BY subquery (SQLite-safe)
            snap_prices: dict[str, float] = {}
            if open_market_ids:
                subq = (
                    db.query(
                        MarketSnapshot.market_id,
                        _func.max(MarketSnapshot.timestamp).label("max_ts"),
                    )
                    .filter(MarketSnapshot.market_id.in_(open_market_ids))
                    .group_by(MarketSnapshot.market_id)
                    .subquery()
                )
                latest_snaps = (
                    db.query(MarketSnapshot.market_id, MarketSnapshot.yes_price)
                    .join(
                        subq,
                        (MarketSnapshot.market_id == subq.c.market_id)
                        & (MarketSnapshot.timestamp == subq.c.max_ts),
                    )
                    .all()
                )
                snap_prices = {s.market_id: s.yes_price for s in latest_snaps}

            for pos, mkt in all_positions:
                cp = snap_prices.get(pos.market_id) if pos.status == "open" else None
                if pos.status == "open" and pos.shares and cp is not None:
                    upnl = round((cp - pos.entry_price) * pos.shares, 2) if pos.side == "YES" \
                           else round((pos.entry_price - cp) * pos.shares, 2)
                else:
                    upnl = None
                rows.append({
                    "id": pos.id,
                    "status": pos.status,
                    "side": pos.side,
                    "question": (mkt.question if mkt else pos.market_id)[:65],
                    "entry_price": pos.entry_price,
                    "current_price": cp,
                    "exit_price": pos.exit_price,
                    "size_usdc": pos.size_usdc,
                    "shares": round(pos.shares, 2) if pos.shares else None,
                    "edge_score": pos.edge_score,
                    "realized_pnl": pos.realized_pnl,
                    "unrealized_pnl": upnl,
                    "exit_reason": pos.exit_reason,
                    "opened_at": pos.opened_at,
                    "closed_at": pos.closed_at,
                })

        df_all = pd.DataFrame(rows)
        df_open = df_all[df_all["status"] == "open"].copy()
        df_closed = df_all[df_all["status"] == "closed"].copy()

        # ── KPI Row ───────────────────────────────────────────────
        total_realized = df_closed["realized_pnl"].sum() if not df_closed.empty else 0
        total_unrealized = df_open["unrealized_pnl"].dropna().sum() if not df_open.empty else 0
        net_pnl = total_realized + total_unrealized
        total_deployed = df_all["size_usdc"].sum() if not df_all.empty else 0
        wins = (df_closed["realized_pnl"] > 0).sum() if not df_closed.empty else 0
        losses = (df_closed["realized_pnl"] < 0).sum() if not df_closed.empty else 0
        win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("Realized PnL", f"${total_realized:+.2f}")
        k2.metric("Unrealized PnL", f"${total_unrealized:+.2f}")
        k3.metric("Net PnL", f"${net_pnl:+.2f}")
        k4.metric("Win Rate", f"{win_rate:.1f}%", f"{wins}W / {losses}L")
        k5.metric("Open Positions", len(df_open))
        k6.metric("Capital Deployed", f"${total_deployed:,.2f}")

        st.markdown("---")

        # ── Styling helpers ───────────────────────────────────────
        def color_pnl(val):
            if pd.isna(val):
                return ""
            color = "#1a9e3f" if val > 0 else "#c0392b" if val < 0 else ""
            return f"color: {color}; font-weight: bold"

        def color_side(val):
            return "color: #2980b9; font-weight: bold" if val == "YES" else "color: #e67e22; font-weight: bold"

        # ── Open Positions Table ──────────────────────────────────
        st.subheader(f"Open Positions ({len(df_open)})")
        if df_open.empty:
            st.info("No open positions.")
        else:
            open_display = df_open[[
                "side", "question", "entry_price", "current_price",
                "size_usdc", "shares", "edge_score", "unrealized_pnl", "opened_at"
            ]].copy()
            open_display.columns = [
                "Side", "Market", "Entry", "Current",
                "Size ($)", "Shares", "Edge Score", "Unreal. PnL ($)", "Opened At"
            ]
            styled = (
                open_display.style
                .map(color_pnl, subset=["Unreal. PnL ($)"])
                .map(color_side, subset=["Side"])
                .background_gradient(subset=["Edge Score"], cmap="YlOrRd", vmin=0, vmax=100)
                .format({
                    "Entry": "{:.3f}",
                    "Current": lambda v: f"{v:.3f}" if pd.notna(v) else "—",
                    "Size ($)": "${:.2f}",
                    "Unreal. PnL ($)": lambda v: f"${v:+.2f}" if pd.notna(v) else "—",
                    "Edge Score": "{:.1f}",
                })
            )
            st.dataframe(styled, use_container_width=True, height=min(42 + len(df_open) * 38, 500))

            chart_df = df_open.dropna(subset=["unrealized_pnl"]).copy()
            if not chart_df.empty:
                chart_df["label"] = chart_df["question"].str[:40]
                chart_df["color"] = chart_df["unrealized_pnl"].apply(lambda x: "gain" if x >= 0 else "loss")
                fig_open = px.bar(
                    chart_df.sort_values("unrealized_pnl"),
                    x="unrealized_pnl", y="label",
                    orientation="h",
                    color="color",
                    color_discrete_map={"gain": "#1a9e3f", "loss": "#c0392b"},
                    title="Unrealized PnL by Open Position",
                    labels={"unrealized_pnl": "Unrealized PnL ($)", "label": ""},
                )
                fig_open.update_layout(showlegend=False, height=350, margin=dict(l=10, r=10))
                st.plotly_chart(fig_open, use_container_width=True)

        st.markdown("---")

        # ── Monthly PnL Chart ─────────────────────────────────────
        st.subheader("Monthly P&L (Closed Trades)")
        if not df_closed.empty:
            df_closed["month"] = pd.to_datetime(df_closed["closed_at"]).dt.to_period("M").astype(str)
            monthly = (
                df_closed.groupby("month")
                .agg(pnl=("realized_pnl", "sum"), trades=("id", "count"))
                .reset_index()
            )
            monthly["color"] = monthly["pnl"].apply(lambda x: "profit" if x >= 0 else "loss")
            fig_monthly = px.bar(
                monthly, x="month", y="pnl",
                color="color",
                color_discrete_map={"profit": "#1a9e3f", "loss": "#c0392b"},
                text=monthly["pnl"].apply(lambda v: f"${v:+.2f}"),
                title="Realized PnL by Month",
                labels={"pnl": "PnL ($)", "month": ""},
            )
            fig_monthly.update_layout(showlegend=False, height=280, margin=dict(l=10, r=10))
            fig_monthly.update_traces(textposition="outside")
            st.plotly_chart(fig_monthly, use_container_width=True)

        # ── Closed Trades Table ───────────────────────────────────
        st.subheader(f"Closed Trades ({len(df_closed)})")
        if df_closed.empty:
            st.info("No closed trades yet.")
        else:
            fc1, fc2 = st.columns(2)
            side_filter = fc1.selectbox("Side", ["All", "YES", "NO"], key="pos_side_filter")
            reason_filter = fc2.selectbox(
                "Exit Reason",
                ["All"] + sorted(df_closed["exit_reason"].dropna().unique().tolist()),
                key="pos_reason_filter",
            )
            df_view = df_closed.copy()
            if side_filter != "All":
                df_view = df_view[df_view["side"] == side_filter]
            if reason_filter != "All":
                df_view = df_view[df_view["exit_reason"] == reason_filter]

            closed_display = df_view[[
                "side", "question", "entry_price", "exit_price",
                "size_usdc", "realized_pnl", "exit_reason", "closed_at"
            ]].copy()
            closed_display.columns = [
                "Side", "Market", "Entry", "Exit",
                "Size ($)", "Realized PnL ($)", "Exit Reason", "Closed At"
            ]
            styled_closed = (
                closed_display.style
                .map(color_pnl, subset=["Realized PnL ($)"])
                .map(color_side, subset=["Side"])
                .format({
                    "Entry": "{:.3f}",
                    "Exit": lambda v: f"{v:.3f}" if pd.notna(v) else "—",
                    "Size ($)": "${:.2f}",
                    "Realized PnL ($)": lambda v: f"${v:+.2f}" if pd.notna(v) else "—",
                })
            )
            st.dataframe(styled_closed, use_container_width=True, height=400)

    _positions_view()


# ═══════════════════════════════════════════════════════════════
# PAGE: Edge Signals
# ═══════════════════════════════════════════════════════════════
elif page == "Edge Signals":
    st.title("🎯 Edge Signals")
    st.caption("Markets with statistically significant mispricing or opportunity")

    signals = get_top_signals(limit=50, min_score=min_edge_score)
    df = df_from_list(signals)

    if df.empty:
        st.info("No active edge signals. Run the monitor to populate data.")
    else:
        # KPI row
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Signals", len(df))
        col2.metric("Top Edge Score", f"{df['edge_score'].max():.1f}")
        yes_count = (df["direction"] == "YES").sum()
        no_count = (df["direction"] == "NO").sum()
        col3.metric("YES Signals", yes_count)
        col4.metric("NO Signals", no_count)

        # Distribution chart
        if "signal_type" in df.columns:
            fig = px.bar(
                df.groupby("signal_type").size().reset_index(name="count"),
                x="signal_type", y="count", title="Signal Type Distribution",
                color="signal_type",
            )
            st.plotly_chart(fig, use_container_width=True)

        # Main table
        st.subheader("All Active Signals")
        display_cols = [
            "edge_score", "direction", "signal_type", "category",
            "current_price", "implied_fair_price", "confidence",
            "sentiment_score", "whale_signal", "volume_24h", "question"
        ]
        display_cols = [c for c in display_cols if c in df.columns]

        st.dataframe(
            df[display_cols].style.background_gradient(subset=["edge_score"], cmap="RdYlGn"),
            use_container_width=True,
        )

        # Signal detail
        st.subheader("Signal Detail")
        if signals:
            selected_q = st.selectbox(
                "Select a market",
                options=[s["question"][:80] for s in signals],
            )
            selected = next((s for s in signals if s["question"][:80] == selected_q), None)
            if selected:
                c1, c2 = st.columns(2)
                c1.json({k: v for k, v in selected.items() if k not in ("detected_at",)})

                # Price history chart
                with get_db() as db:
                    snaps = (
                        db.query(MarketSnapshot)
                        .filter(MarketSnapshot.market_id == selected["market_id"])
                        .order_by(MarketSnapshot.timestamp.asc())
                        .limit(200)
                        .all()
                    )
                if snaps:
                    snap_df = pd.DataFrame([
                        {"timestamp": s.timestamp, "yes_price": s.yes_price,
                         "volume_24h": s.volume_24h}
                        for s in snaps
                    ])
                    fig2 = go.Figure()
                    fig2.add_trace(go.Scatter(
                        x=snap_df["timestamp"], y=snap_df["yes_price"],
                        name="YES Price", line=dict(color="green"),
                    ))
                    if selected.get("implied_fair_price"):
                        fig2.add_hline(
                            y=selected["implied_fair_price"],
                            line_dash="dash", line_color="orange",
                            annotation_text="Implied Fair Price",
                        )
                    fig2.update_layout(title="Price History", height=300)
                    c2.plotly_chart(fig2, use_container_width=True)


# ═══════════════════════════════════════════════════════════════
# PAGE: Arbitrage
# ═══════════════════════════════════════════════════════════════
elif page == "Arbitrage":
    st.title("⚡ Arbitrage Opportunities")

    opps = get_top_arbitrage(limit=30)
    df = df_from_list(opps)

    if df.empty:
        st.info("No active arbitrage opportunities detected.")
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("Active Opportunities", len(df))
        col2.metric("Best Profit %", f"{df['profit_pct'].max():.2f}%")
        col3.metric("Avg Profit %", f"{df['profit_pct'].mean():.2f}%")

        fig = px.histogram(df, x="profit_pct", nbins=20, title="Profit % Distribution")
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            df[["profit_pct", "arb_type", "poly_yes_price", "kalshi_yes_price",
                "price_gap", "direction", "description"]].style.background_gradient(
                subset=["profit_pct"], cmap="Greens"
            ),
            use_container_width=True,
        )


# ═══════════════════════════════════════════════════════════════
# PAGE: Whale Tracker
# ═══════════════════════════════════════════════════════════════
elif page == "Whale Tracker":
    st.title("🐋 Whale Activity Monitor")

    hours = st.slider("Lookback (hours)", 1, 72, 12)
    trades = get_recent_whale_activity(hours=hours, limit=100)
    df = df_from_list(trades)

    with get_db() as db:
        whales = db.query(WhaleWallet).order_by(WhaleWallet.total_profit_usdc.desc()).limit(50).all()
        whale_df = pd.DataFrame([
            {
                "address": w.address,
                "label": w.label or w.address[:10] + "…",
                "profit_usdc": w.total_profit_usdc,
                "win_rate": w.win_rate,
                "total_trades": w.total_trades,
                "last_active": w.last_active,
                "source": w.source,
            }
            for w in whales
        ])

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Tracked Whale Wallets")
        if not whale_df.empty:
            st.dataframe(whale_df, use_container_width=True)
        else:
            st.info("No whales tracked yet. Run the monitor to populate.")

    with col2:
        st.subheader(f"Recent Trades (last {hours}h)")
        if not df.empty:
            # Color-code by action
            st.dataframe(df, use_container_width=True)

            # Volume by side
            if "action" in df.columns and "size_usdc" in df.columns:
                vol_by_action = df.groupby(["action", "side"])["size_usdc"].sum().reset_index()
                fig = px.bar(
                    vol_by_action, x="action", y="size_usdc",
                    color="side", title="Whale Volume by Action & Side",
                    barmode="group",
                )
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No whale trades recorded in this window.")


# ═══════════════════════════════════════════════════════════════
# PAGE: Market Browser
# ═══════════════════════════════════════════════════════════════
elif page == "Market Browser":
    st.title("🏪 Market Browser")

    with get_db() as db:
        markets = (
            db.query(Market)
            .filter(Market.is_active == True)
            .order_by(Market.volume_24h.desc())
            .limit(500)
            .all()
        )

    if not markets:
        st.info("No markets in database. Run the monitor to populate.")
    else:
        df = pd.DataFrame([
            {
                "id": m.id,
                "question": m.question,
                "category": m.category,
                "yes_price": m.yes_price,
                "no_price": m.no_price,
                "spread": m.spread,
                "volume_24h": m.volume_24h,
                "volume_total": m.volume_total,
                "liquidity": m.liquidity,
                "end_date": m.end_date,
            }
            for m in markets
        ])

        col1, col2, col3 = st.columns(3)
        col1.metric("Active Markets", len(df))
        col2.metric("Total Liquidity", f"${df['liquidity'].sum():,.0f}")
        col3.metric("Total 24h Volume", f"${df['volume_24h'].sum():,.0f}")

        # Category filter
        cats = ["All"] + sorted(df["category"].dropna().unique().tolist())
        selected_cat = st.selectbox("Filter by Category", cats)
        if selected_cat != "All":
            df = df[df["category"] == selected_cat]

        # Volume chart
        fig = px.treemap(
            df.head(50),
            path=["category", "question"],
            values="volume_24h",
            title="Top 50 Markets by 24h Volume",
            color="yes_price",
            color_continuous_scale="RdYlGn",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(df, use_container_width=True)


# ═══════════════════════════════════════════════════════════════
# PAGE: Sentiment
# ═══════════════════════════════════════════════════════════════
elif page == "Sentiment":
    st.title("💬 Social Sentiment")

    with get_db() as db:
        records = (
            db.query(SentimentRecord, Market)
            .join(Market, SentimentRecord.market_id == Market.id)
            .filter(SentimentRecord.source == "aggregate")
            .order_by(SentimentRecord.timestamp.desc())
            .limit(200)
            .all()
        )

    if not records:
        st.info("No sentiment data yet. Run the monitor to populate.")
    else:
        df = pd.DataFrame([
            {
                "market": market.question[:60],
                "category": market.category,
                "compound": rec.compound_score,
                "positive": rec.positive_ratio,
                "negative": rec.negative_ratio,
                "mentions": rec.mention_count,
                "timestamp": rec.timestamp,
            }
            for rec, market in records
        ])

        # Latest per market
        latest = df.sort_values("timestamp").groupby("market").last().reset_index()

        col1, col2 = st.columns(2)
        with col1:
            fig = px.bar(
                latest.sort_values("compound"),
                x="compound", y="market",
                orientation="h",
                title="Latest Sentiment by Market",
                color="compound",
                color_continuous_scale="RdYlGn",
            )
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            fig2 = px.scatter(
                latest,
                x="compound", y="mentions",
                color="category",
                hover_data=["market"],
                title="Sentiment vs Mention Volume",
            )
            st.plotly_chart(fig2, use_container_width=True)


# ═══════════════════════════════════════════════════════════════
# PAGE: Raw DB
# ═══════════════════════════════════════════════════════════════
elif page == "Raw DB":
    st.title("🗄️ Raw Database Inspector")

    table_choice = st.selectbox(
        "Table",
        ["markets", "edge_signals", "arbitrage_opportunities",
         "whale_trades", "whale_wallets", "sentiment_records", "leaderboard"]
    )
    limit = st.number_input("Row limit", 10, 1000, 100)

    with get_db() as db:
        from sqlalchemy import text
        rows = db.execute(
            text(f"SELECT * FROM {table_choice} ORDER BY rowid DESC LIMIT {limit}")
        ).fetchall()
        if rows:
            col_names = db.execute(
                text(f"PRAGMA table_info({table_choice})")
            ).fetchall()
            cols = [c[1] for c in col_names]
            df = pd.DataFrame(rows, columns=cols)
            st.dataframe(df, use_container_width=True)
        else:
            st.info(f"Table '{table_choice}' is empty.")

# ─── Auto-refresh footer ──────────────────────────────────────
if auto_refresh:
    st.empty()
    time.sleep(30)
    st.rerun()
