import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket_edge.db")

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Top whales by 24h volume
cur.execute("""
    SELECT w.address, w.label, w.source, w.total_profit_usdc, w.win_rate,
           w.total_trades, w.last_active,
           COUNT(t.id)                                                    AS recent_trades,
           SUM(t.size_usdc)                                               AS recent_volume,
           SUM(CASE WHEN t.action='BUY'  THEN t.size_usdc ELSE 0 END)    AS buy_vol,
           SUM(CASE WHEN t.action='SELL' THEN t.size_usdc ELSE 0 END)    AS sell_vol
    FROM whale_wallets w
    LEFT JOIN whale_trades t ON t.wallet_address = w.address
        AND t.timestamp >= datetime('now', '-24 hours')
    GROUP BY w.address
    HAVING recent_trades > 0
    ORDER BY recent_volume DESC
    LIMIT 20
""")
whales = cur.fetchall()

# Recent large trades with market names
cur.execute("""
    SELECT t.timestamp, t.wallet_address, w.label, t.action, t.side,
           t.size_usdc, t.price, m.question
    FROM whale_trades t
    LEFT JOIN whale_wallets w ON w.address = t.wallet_address
    LEFT JOIN markets m ON m.id = t.market_id
    WHERE t.timestamp >= datetime('now', '-3 hours')
    ORDER BY t.size_usdc DESC
    LIMIT 25
""")
recent = cur.fetchall()

conn.close()

sep  = '=' * 105
thin = '-' * 105
now  = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

print()
print(sep)
print('  WHALE REPORT  --  ' + now)
print(sep)

print()
print('-- TOP WHALES BY 24H VOLUME --------------------------------------------------')
print('  %-16s  %-15s  %-11s  %10s  %7s  %9s  %9s  %9s' % (
    'Wallet', 'Label', 'Source', '24h Vol', 'Trades', 'Net Flow', 'Buy', 'Sell'))
print('  ' + thin)
for w in whales:
    label  = (w['label'] or '')[:14]
    net    = (w['buy_vol'] or 0) - (w['sell_vol'] or 0)
    net_s  = ('+$' if net >= 0 else '-$') + '{:,.0f}'.format(abs(net))
    print('  %-16s  %-15s  %-11s  %10s  %7d  %9s  %9s  %9s' % (
        (w['address'] or '')[:15], label, w['source'] or '',
        '${:,.0f}'.format(w['recent_volume'] or 0), w['recent_trades'] or 0,
        net_s,
        '${:,.0f}'.format(w['buy_vol'] or 0),
        '${:,.0f}'.format(w['sell_vol'] or 0)))

print()
print('-- BIGGEST TRADES (last 3 hours) --------------------------------------------')
print('  %-16s  %-15s  %-4s  %-4s  %9s  %6s  %-40s' % (
    'Time (UTC)', 'Wallet', 'Act', 'Side', 'Size', 'Price', 'Market'))
print('  ' + thin)
for t in recent:
    ts     = (t['timestamp'] or '')[:16]
    label  = (t['label'] or '')[:14]
    wallet = (t['wallet_address'] or '')[:10]
    name   = label or wallet
    mkt    = (t['question'] or 'unknown market')[:39]
    print('  %-16s  %-15s  %-4s  %-4s  %9s  %6.3f  %-40s' % (
        ts, name, t['action'] or '?', t['side'] or '?',
        '${:,.0f}'.format(t['size_usdc'] or 0), t['price'] or 0, mkt))

print()
print(sep)
