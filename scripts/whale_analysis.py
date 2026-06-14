import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket_edge.db")
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# By side (YES/NO) and action
cur.execute("""
    SELECT side, action,
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
        SUM(realized_pnl) as pnl,
        AVG(realized_pnl) as avg_pnl
    FROM executed_trades
    WHERE signal_type = 'whale_accumulation'
      AND realized_pnl IS NOT NULL
    GROUP BY side, action
    ORDER BY side, action
""")
by_side = cur.fetchall()

# By month
cur.execute("""
    SELECT strftime('%Y-%m', executed_at) as month,
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(realized_pnl) as pnl,
        SUM(size_usdc) as wagered
    FROM executed_trades
    WHERE signal_type = 'whale_accumulation'
      AND realized_pnl IS NOT NULL
    GROUP BY month
    ORDER BY month
""")
by_month = cur.fetchall()

# By direction from edge_signals
cur.execute("""
    SELECT es.direction,
        COUNT(*) as trades,
        SUM(CASE WHEN et.realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(et.realized_pnl) as pnl,
        AVG(et.realized_pnl) as avg_pnl
    FROM executed_trades et
    LEFT JOIN edge_signals es ON es.market_id = et.market_id
    WHERE et.signal_type = 'whale_accumulation'
      AND et.realized_pnl IS NOT NULL
    GROUP BY es.direction
    ORDER BY es.direction
""")
by_dir = cur.fetchall()

# Compare whale_accumulation vs volume_spike head-to-head
cur.execute("""
    SELECT signal_type,
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        ROUND(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as wr,
        SUM(realized_pnl) as pnl,
        AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
        AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END) as avg_loss
    FROM executed_trades
    WHERE signal_type IN ('whale_accumulation', 'volume_spike')
      AND realized_pnl IS NOT NULL
    GROUP BY signal_type
""")
comparison = cur.fetchall()
conn.close()

sep  = '=' * 90
thin = '-' * 90
print()
print(sep)
print('  WHALE SIGNAL DEEP ANALYSIS  --  ' + datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))
print(sep)

print()
print('-- Signal Comparison: whale_accumulation vs volume_spike --')
print('  %-30s  %6s  %5s  %6s  %9s  %8s  %8s' % (
    'Signal', 'Trades', 'WR%', 'P&L', 'ROI', 'AvgWin', 'AvgLoss'))
print('  ' + thin)
for r in comparison:
    wagered_approx = abs((r['pnl'] or 0) / ((r['pnl'] or 1) / max(r['trades'], 1))) if r['trades'] else 1
    print('  %-30s  %6d  %5.1f%%  %+9.2f  %+8.2f  %+8.2f' % (
        r['signal_type'], r['trades'], r['wr'] or 0,
        r['pnl'] or 0, r['avg_win'] or 0, r['avg_loss'] or 0))

print()
print('-- Whale Trades by Side & Action (what positions were taken) --')
print('  %-6s  %-8s  %6s  %5s  %5s  %6s  %9s  %8s' % (
    'Side', 'Action', 'Trades', 'Wins', 'Loss', 'WR%', 'Total P&L', 'Avg P&L'))
print('  ' + thin)
for r in by_side:
    wr = (r['wins'] / r['trades'] * 100) if r['trades'] else 0
    print('  %-6s  %-8s  %6d  %5d  %5d  %5.1f%%  %+9.2f  %+8.2f' % (
        r['side'] or '?', r['action'] or '?',
        r['trades'], r['wins'], r['losses'], wr,
        r['pnl'] or 0, r['avg_pnl'] or 0))

print()
print('-- Whale Trades by Month --')
print('  %-8s  %6s  %5s  %6s  %9s  %8s' % (
    'Month', 'Trades', 'WR%', 'Wins', 'P&L', 'ROI%'))
print('  ' + thin)
for r in by_month:
    wr  = (r['wins'] / r['trades'] * 100) if r['trades'] else 0
    roi = (r['pnl'] / r['wagered'] * 100) if r['wagered'] else 0
    print('  %-8s  %6d  %5.1f%%  %5d  %+9.2f  %+7.1f%%' % (
        r['month'], r['trades'], wr, r['wins'], r['pnl'] or 0, roi))

print()
print('-- Whale Signal Direction vs Trade Outcome --')
print('  %-10s  %6s  %5s  %9s  %8s' % ('Direction', 'Trades', 'WR%', 'P&L', 'Avg P&L'))
print('  ' + thin)
for r in by_dir:
    wr = (r['wins'] / r['trades'] * 100) if r['trades'] else 0
    print('  %-10s  %6d  %5.1f%%  %+9.2f  %+8.2f' % (
        r['direction'] or 'unknown', r['trades'], wr, r['pnl'] or 0, r['avg_pnl'] or 0))

print()
print(sep)
print()
print('  DIAGNOSIS:')
print('  - If YES and NO both lose: signal direction is WRONG (inverted BUY/SELL)')
print('  - If only one side loses badly: payoff asymmetry or size issue')
print('  - If WR < 40% on both sides: predictions are genuinely bad')
print()
print(sep)
