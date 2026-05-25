import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db.database import get_db
from db.models import EdgeSignal
from datetime import datetime

with get_db() as db:
    signals = (
        db.query(EdgeSignal)
        .filter(EdgeSignal.is_active == True)
        .order_by(EdgeSignal.edge_score.desc())
        .limit(20)
        .all()
    )
    rows = [
        {
            'q':     (s.question or s.market_id or '')[:47],
            'type':  s.signal_type or '?',
            'dir':   s.direction or '?',
            'score': s.edge_score or 0,
            'conf':  s.confidence or 0,
            'price': s.current_price or 0,
            'fair':  s.implied_fair_price or 0,
            'whale': s.whale_signal or 0,
            'notes': (s.notes or '')[:35],
        }
        for s in signals
    ]

sep  = '=' * 110
thin = '-' * 110

print(sep)
print('  TOP ACTIVE SIGNALS  --  %s' % datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))
print('  %d signals shown' % len(rows))
print(sep)
print('  %-3s  %-47s  %-24s  %-7s  %-5s  %-5s  %-6s  %-6s  %-5s' % (
    '#', 'Market', 'Type', 'Dir', 'Score', 'Conf', 'Price', 'Fair', 'Gap'))
print(thin)
for i, s in enumerate(rows, 1):
    gap = s['fair'] - s['price']
    print('  %-3d  %-47s  %-24s  %-7s  %-5.1f  %-4.0f%%  %-6.3f  %-6.3f  %+.3f' % (
        i, s['q'], s['type'], s['dir'],
        s['score'], s['conf'] * 100,
        s['price'], s['fair'], gap))
    if s['notes']:
        print('       Notes: %s' % s['notes'])
print(sep)
