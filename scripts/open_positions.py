import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "polymarket_edge.db")

conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT p.id, p.side, p.entry_price, p.size_usdc, p.shares,
           p.kelly_fraction, p.edge_score, p.status, p.opened_at,
           p.is_paper, m.question
    FROM positions p
    LEFT JOIN markets m ON m.id = p.market_id
    WHERE p.status = 'open'
    ORDER BY p.opened_at DESC
""")
rows = cur.fetchall()

cur.execute("""
    SELECT COUNT(*) as cnt,
           SUM(size_usdc) as total_deployed,
           AVG(edge_score) as avg_score
    FROM positions
    WHERE status = 'open'
""")
summary = cur.fetchone()
conn.close()

now  = datetime.utcnow()
sep  = '=' * 115
thin = '-' * 115

print()
print(sep)
print('  OPEN POSITIONS  --  %s' % now.strftime('%Y-%m-%d %H:%M UTC'))
print(sep)
print('  Total open: %d  |  Deployed: $%.2f  |  Avg edge score: %.1f' % (
    summary['cnt'] or 0,
    summary['total_deployed'] or 0,
    summary['avg_score'] or 0))
print()

if not rows:
    print('  No open positions.')
else:
    print('  %-4s  %-4s  %-7s  %-8s  %-7s  %-6s  %-6s  %-8s  %-12s  %-35s' % (
        'ID', 'Side', 'Entry', 'Size', 'Shares', 'Kelly%', 'Score', 'Mode', 'Age', 'Market'))
    print('  ' + thin)
    for r in rows:
        try:
            dt = datetime.strptime(str(r['opened_at'])[:19], '%Y-%m-%d %H:%M:%S')
            age_min = int((now - dt).total_seconds() / 60)
            age_s = '%dm' % age_min if age_min < 60 else '%dh%dm' % (age_min // 60, age_min % 60)
        except Exception:
            age_s = '?'
        mode = 'PAPER' if r['is_paper'] else 'LIVE'
        mkt  = (r['question'] or 'unknown')[:34]
        print('  %-4d  %-4s  %-7.3f  $%-7.2f  %-7.3f  %-6.1f  %-6.1f  %-8s  %-12s  %-35s' % (
            r['id'], r['side'] or '?', r['entry_price'] or 0,
            r['size_usdc'] or 0, r['shares'] or 0,
            (r['kelly_fraction'] or 0) * 100, r['edge_score'] or 0,
            mode, age_s, mkt))

print()
print(sep)
