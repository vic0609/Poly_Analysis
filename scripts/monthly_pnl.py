import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket_edge.db")

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT
        strftime('%Y-%m', executed_at) as month,
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
        SUM(realized_pnl) as total_pnl,
        SUM(size_usdc) as wagered,
        AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
        AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END) as avg_loss,
        MAX(realized_pnl) as best_trade,
        MIN(realized_pnl) as worst_trade
    FROM executed_trades
    WHERE realized_pnl IS NOT NULL
      AND strftime('%Y-%m', executed_at) >= strftime('%Y-%m', datetime('now', '-3 months'))
    GROUP BY month
    ORDER BY month
""")
months = cur.fetchall()

cur.execute("""
    SELECT
        strftime('%Y-%m', executed_at) as month,
        signal_type,
        COUNT(*) as trades,
        SUM(realized_pnl) as pnl,
        SUM(size_usdc) as wagered,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins
    FROM executed_trades
    WHERE realized_pnl IS NOT NULL
      AND strftime('%Y-%m', executed_at) >= strftime('%Y-%m', datetime('now', '-3 months'))
    GROUP BY month, signal_type
    ORDER BY month, pnl DESC
""")
by_signal = cur.fetchall()
conn.close()

sep  = '=' * 100
thin = '-' * 100
now  = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

print()
print(sep)
print('  P&L COMPARISON -- LAST 3 MONTHS  --  ' + now)
print(sep)

cum = 0.0
for r in months:
    cum += r['total_pnl'] or 0
    wr  = (r['wins'] / r['trades'] * 100) if r['trades'] else 0
    roi = (r['total_pnl'] / r['wagered'] * 100) if r['wagered'] else 0
    wl  = abs((r['avg_win'] or 0) / (r['avg_loss'] or -1)) if r['avg_loss'] else 0
    bar = '#' * int(wr / 100 * 20) + '-' * (20 - int(wr / 100 * 20))
    pnl_arrow = '+' if (r['total_pnl'] or 0) >= 0 else ''

    print()
    print('  MONTH: %s' % r['month'])
    print(thin)
    print('  Trades : %d total  |  Wins: %d  |  Losses: %d' % (
        r['trades'], r['wins'], r['losses']))
    print('  Win Rate: %.1f%%  [%s]' % (wr, bar))
    print('  P&L    : %s$%.2f  |  Wagered: $%s  |  ROI: %+.2f%%  |  Cumulative: %+.2f' % (
        pnl_arrow, r['total_pnl'] or 0, '{:,.0f}'.format(r['wagered'] or 0), roi, cum))
    print('  Avg Win: %+.2f  |  Avg Loss: %+.2f  |  W/L Ratio: %.2fx' % (
        r['avg_win'] or 0, r['avg_loss'] or 0, wl))
    print('  Best trade: %+.2f  |  Worst trade: %+.2f' % (
        r['best_trade'] or 0, r['worst_trade'] or 0))

    sigs = [s for s in by_signal if s['month'] == r['month']]
    if sigs:
        print('  By signal type:')
        for s in sigs:
            sroi = (s['pnl'] / s['wagered'] * 100) if s['wagered'] else 0
            swr  = (s['wins'] / s['trades'] * 100) if s['trades'] else 0
            print('    %-32s  %3d trades  %5.1f%% WR  %+8.2f  ROI: %+.1f%%' % (
                s['signal_type'] or 'unknown', s['trades'], swr, s['pnl'] or 0, sroi))

print()
print(sep)
