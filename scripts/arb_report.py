import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket_edge.db")

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT a.profit_pct, a.poly_yes_price, a.kalshi_yes_price, a.price_gap,
           a.direction, a.arb_type, a.detected_at,
           a.estimated_max_size_usdc, m.question
    FROM arbitrage_opportunities a
    LEFT JOIN markets m ON m.id = a.polymarket_market_id
    WHERE a.is_active = 1
      AND a.poly_yes_price  > 0.05 AND a.poly_yes_price  < 0.95
      AND a.kalshi_yes_price > 0.05 AND a.kalshi_yes_price < 0.95
    ORDER BY a.profit_pct DESC
    LIMIT 30
""")
active = cur.fetchall()

cur.execute("""
    SELECT COUNT(*) as total,
           AVG(profit_pct) as avg_pct,
           MAX(profit_pct) as max_pct,
           SUM(CASE WHEN is_active=1
                     AND poly_yes_price  > 0.05 AND poly_yes_price  < 0.95
                     AND kalshi_yes_price > 0.05 AND kalshi_yes_price < 0.95
                    THEN 1 ELSE 0 END) as active_count
    FROM arbitrage_opportunities
    WHERE detected_at >= datetime('now', '-24 hours')
""")
stats = cur.fetchone()

conn.close()

now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
sep  = '=' * 108
thin = '-' * 108

print()
print(sep)
print('  ARB REPORT  --  ' + now)
print(sep)
print()
print('  24h Stats:  Detected: %d  |  Active (live): %d  |  Avg gap: %.2f%%  |  Best gap: %.2f%%' % (
    stats['total'] or 0, stats['active_count'] or 0,
    stats['avg_pct'] or 0, stats['max_pct'] or 0))
print()

if not active:
    print('  No live arb opportunities (all markets near-resolved or no matches).')
else:
    print('-- LIVE ARB OPPORTUNITIES (%d) -----------------------------------------------' % len(active))
    print('  %-7s  %-8s  %-8s  %-5s  %-16s  %-8s  %-10s  %-35s' % (
        'Profit%', 'Poly', 'Kalshi', 'Gap', 'Type', 'MaxSize', 'Age', 'Market'))
    print('  ' + thin)
    for r in active:
        if r['detected_at']:
            try:
                dt = datetime.strptime(str(r['detected_at'])[:19], '%Y-%m-%d %H:%M:%S')
                age_min = int((datetime.utcnow() - dt).total_seconds() / 60)
                age_s = '%dm' % age_min if age_min < 60 else '%dh%dm' % (age_min // 60, age_min % 60)
            except Exception:
                age_s = '?'
        else:
            age_s = '?'
        mkt   = (r['question'] or 'unknown')[:34]
        maxsz = ('$%,.0f' % r['estimated_max_size_usdc']) if r['estimated_max_size_usdc'] else 'n/a'
        print('  %+6.2f%%  %8.3f  %8.3f  %5.3f  %-16s  %8s  %10s  %-35s' % (
            r['profit_pct'] or 0,
            r['poly_yes_price'] or 0,
            r['kalshi_yes_price'] or 0,
            r['price_gap'] or 0,
            (r['arb_type'] or '?')[:15],
            maxsz, age_s, mkt))

print()
print(sep)
