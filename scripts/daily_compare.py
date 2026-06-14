import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket_edge.db")

# Accept optional days argument: python daily_compare.py 5
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute(f"""
    SELECT
        date(executed_at) as day,
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
        SUM(realized_pnl) as pnl,
        SUM(size_usdc) as wagered,
        AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
        AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END) as avg_loss,
        MAX(realized_pnl) as best,
        MIN(realized_pnl) as worst
    FROM executed_trades
    WHERE realized_pnl IS NOT NULL
      AND date(executed_at) >= date('now', '-{DAYS} days')
    GROUP BY day
    ORDER BY day ASC
""")
days = cur.fetchall()

cur.execute(f"""
    SELECT
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
        SUM(realized_pnl) as pnl,
        SUM(size_usdc) as wagered
    FROM executed_trades
    WHERE realized_pnl IS NOT NULL
      AND date(executed_at) >= date('now', '-{DAYS} days')
""")
total = cur.fetchone()

cur.execute("""
    SELECT
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
        SUM(realized_pnl) as pnl,
        SUM(size_usdc) as wagered
    FROM executed_trades
    WHERE realized_pnl IS NOT NULL
""")
alltime = cur.fetchone()
conn.close()

sep  = '=' * 100
thin = '-' * 100
now  = datetime.utcnow()
today = now.strftime('%Y-%m-%d')

print()
print(sep)
print('  DAILY PERFORMANCE -- LAST %d DAYS vs TODAY  --  %s' % (DAYS, now.strftime('%Y-%m-%d %H:%M UTC')))
print(sep)
print()
print('  %-12s  %6s  %5s  %5s  %6s  %9s  %8s  %7s  %8s  %8s' % (
    'Date', 'Trades', 'Wins', 'Loss', 'WR%', 'P&L', 'Wagered', 'ROI%', 'AvgWin', 'AvgLoss'))
print('  ' + thin)

cum = 0.0
for r in days:
    wr  = (r['wins'] / r['trades'] * 100) if r['trades'] else 0
    roi = (r['pnl'] / r['wagered'] * 100) if r['wagered'] else 0
    cum += r['pnl'] or 0
    tag = '  <-- TODAY' if r['day'] == today else ''
    print('  %-12s  %6d  %5d  %5d  %5.1f%%  %+9.2f  %8s  %+6.2f%%  %+8.2f  %+8.2f%s' % (
        r['day'], r['trades'], r['wins'], r['losses'], wr,
        r['pnl'] or 0, '$'+'{:,.0f}'.format(r['wagered'] or 0), roi,
        r['avg_win'] or 0, r['avg_loss'] or 0, tag))

print('  ' + thin)
t_wr  = (total['wins'] / total['trades'] * 100) if total['trades'] else 0
t_roi = (total['pnl'] / total['wagered'] * 100) if total['wagered'] else 0
print('  %-12s  %6d  %5d  %5d  %5.1f%%  %+9.2f  %8s  %+6.2f%%' % (
    '%d-DAY TOTAL' % (DAYS + 1), total['trades'], total['wins'], total['losses'],
    t_wr, total['pnl'] or 0, '$'+'{:,.0f}'.format(total['wagered'] or 0), t_roi))

print()
print(sep)
print()
print('  ALL-TIME TOTAL')
print('  ' + thin)
at_wr  = (alltime['wins'] / alltime['trades'] * 100) if alltime['trades'] else 0
at_roi = (alltime['pnl'] / alltime['wagered'] * 100) if alltime['wagered'] else 0
print('  Trades: %d  |  Wins: %d  |  Losses: %d  |  Win Rate: %.1f%%' % (
    alltime['trades'], alltime['wins'], alltime['losses'], at_wr))
print('  Total P&L: %+.2f  |  Wagered: $%s  |  ROI: %+.2f%%' % (
    alltime['pnl'] or 0, '{:,.0f}'.format(alltime['wagered'] or 0), at_roi))
print()
print(sep)
