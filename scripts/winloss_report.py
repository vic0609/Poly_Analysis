import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db.database import get_db
from db.models import ExecutedTrade
from datetime import datetime
from collections import defaultdict

with get_db() as db:
    closed = [
        {
            'executed_at': t.executed_at,
            'side': t.side or '?',
            'price': t.price or 0,
            'size_usdc': t.size_usdc or 0,
            'realized_pnl': t.realized_pnl,
            'signal_type': t.signal_type or 'unknown',
            'edge_score': t.edge_score or 0,
        }
        for t in db.query(ExecutedTrade).filter(ExecutedTrade.realized_pnl != None).all()
    ]

wins   = [t for t in closed if t['realized_pnl'] > 0]
losses = [t for t in closed if t['realized_pnl'] <= 0]
total  = len(closed)
wagered = sum(t['size_usdc'] for t in closed)
pnl     = sum(t['realized_pnl'] for t in closed)
avg_win  = sum(t['realized_pnl'] for t in wins)   / len(wins)   if wins   else 0
avg_loss = sum(t['realized_pnl'] for t in losses) / len(losses) if losses else 0
gross_win  = sum(t['realized_pnl'] for t in wins)
gross_loss = abs(sum(t['realized_pnl'] for t in losses))

def bar(w, n, width=20):
    filled = int(w / n * width) if n else 0
    return '[' + '#' * filled + '-' * (width - filled) + ']'

sep = '=' * 60
thin = '-' * 60

print(sep)
print('  WIN / LOSS ANALYSIS  --  ALL TIME')
print(f'  {total} closed trades  |  {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
print(sep)
print()
print(f'  Total closed:    {total}')
print(f'  Wins:            {len(wins)}  ({len(wins)/total*100:.1f}%)')
print(f'  Losses:          {len(losses)}  ({len(losses)/total*100:.1f}%)')
print(f'  Win bar:         {bar(len(wins), total)}  {len(wins)/total*100:.1f}%')
print(f'  Realized P&L:    ${pnl:+,.2f}')
print(f'  Total wagered:   ${wagered:,.2f}')
print(f'  ROI:             {pnl/wagered*100:+.2f}%')
print(f'  Avg win:         ${avg_win:+.2f}')
print(f'  Avg loss:        ${avg_loss:+.2f}')
if avg_loss:
    print(f'  Win/loss ratio:  {abs(avg_win/avg_loss):.2f}x')
if gross_loss:
    print(f'  Profit factor:   {gross_win/gross_loss:.2f}x')
print()

# By signal type
print('-- BY SIGNAL TYPE -----------------------------------')
print(f'  {"Type":<30} {"W":>4} {"L":>4}  {"Win%":>5}  {"P&L":>9}  {"ROI":>6}  Bar')
print(f'  {"-"*30} {"-"*4} {"-"*4}  {"-"*5}  {"-"*9}  {"-"*6}  {"-"*14}')
by_type = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0, 'wagered': 0.0})
for t in closed:
    k = t['signal_type']
    by_type[k]['wins']    += 1 if t['realized_pnl'] > 0 else 0
    by_type[k]['losses']  += 1 if t['realized_pnl'] <= 0 else 0
    by_type[k]['pnl']     += t['realized_pnl']
    by_type[k]['wagered'] += t['size_usdc']
for k, v in sorted(by_type.items(), key=lambda x: -(x[1]['wins'] + x[1]['losses'])):
    n = v['wins'] + v['losses']
    wr = v['wins'] / n * 100 if n else 0
    roi = v['pnl'] / v['wagered'] * 100 if v['wagered'] else 0
    print(f'  {k:<30} {v["wins"]:>4} {v["losses"]:>4}  {wr:>4.1f}%  ${v["pnl"]:>+8.2f}  {roi:>+5.1f}%  {bar(v["wins"], n, 14)}')
print()

# By side
print('-- BY SIDE ------------------------------------------')
print(f'  {"Side":<6} {"W":>4} {"L":>4}  {"Win%":>5}  {"P&L":>9}  {"Avg Win":>8}  {"Avg Loss":>9}  Bar')
print(f'  {"-"*6} {"-"*4} {"-"*4}  {"-"*5}  {"-"*9}  {"-"*8}  {"-"*9}  {"-"*14}')
by_side = defaultdict(lambda: {'wins': [], 'losses': [], 'pnl': 0.0})
for t in closed:
    if t['realized_pnl'] > 0:
        by_side[t['side']]['wins'].append(t['realized_pnl'])
    else:
        by_side[t['side']]['losses'].append(t['realized_pnl'])
    by_side[t['side']]['pnl'] += t['realized_pnl']
for k, v in sorted(by_side.items(), key=lambda x: -x[1]['pnl']):
    w, l = len(v['wins']), len(v['losses'])
    wr = w / (w + l) * 100 if (w + l) else 0
    aw = sum(v['wins']) / w if w else 0
    al = sum(v['losses']) / l if l else 0
    print(f'  {k:<6} {w:>4} {l:>4}  {wr:>4.1f}%  ${v["pnl"]:>+8.2f}  ${aw:>+7.2f}  ${al:>+8.2f}  {bar(w, w+l, 14)}')
print()

# By edge score
print('-- BY EDGE SCORE ------------------------------------')
print(f'  {"Score":<8} {"W":>4} {"L":>4}  {"Win%":>5}  {"P&L":>9}  {"ROI":>6}  Bar')
print(f'  {"-"*8} {"-"*4} {"-"*4}  {"-"*5}  {"-"*9}  {"-"*6}  {"-"*14}')
buckets = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0, 'wagered': 0.0})
for t in closed:
    b = f'{int(t["edge_score"]//10)*10}-{int(t["edge_score"]//10)*10+9}'
    buckets[b]['wins']    += 1 if t['realized_pnl'] > 0 else 0
    buckets[b]['losses']  += 1 if t['realized_pnl'] <= 0 else 0
    buckets[b]['pnl']     += t['realized_pnl']
    buckets[b]['wagered'] += t['size_usdc']
for k in sorted(buckets):
    v = buckets[k]
    n = v['wins'] + v['losses']
    wr = v['wins'] / n * 100 if n else 0
    roi = v['pnl'] / v['wagered'] * 100 if v['wagered'] else 0
    print(f'  {k:<8} {v["wins"]:>4} {v["losses"]:>4}  {wr:>4.1f}%  ${v["pnl"]:>+8.2f}  {roi:>+5.1f}%  {bar(v["wins"], n, 14)}')
print()

# By entry price range
print('-- BY ENTRY PRICE RANGE (YES price) -----------------')
print(f'  {"Range":<12} {"W":>4} {"L":>4}  {"Win%":>5}  {"P&L":>9}  {"ROI":>6}  Bar')
print(f'  {"-"*12} {"-"*4} {"-"*4}  {"-"*5}  {"-"*9}  {"-"*6}  {"-"*14}')
price_buckets = [
    ('0.25-0.29', 0.25, 0.30),
    ('0.30-0.39', 0.30, 0.40),
    ('0.40-0.49', 0.40, 0.50),
    ('0.50-0.59', 0.50, 0.60),
    ('0.60-0.69', 0.60, 0.70),
    ('0.70-0.75', 0.70, 0.76),
]
for label, lo, hi in price_buckets:
    group = [t for t in closed if lo <= t['price'] < hi]
    if not group:
        continue
    w = sum(1 for t in group if t['realized_pnl'] > 0)
    l = len(group) - w
    p = sum(t['realized_pnl'] for t in group)
    wag = sum(t['size_usdc'] for t in group)
    wr = w / len(group) * 100
    roi = p / wag * 100 if wag else 0
    print(f'  {label:<12} {w:>4} {l:>4}  {wr:>4.1f}%  ${p:>+8.2f}  {roi:>+5.1f}%  {bar(w, len(group), 14)}')
print()

# By month
print('-- BY MONTH -----------------------------------------')
print(f'  {"Month":<10} {"W":>4} {"L":>4}  {"Win%":>5}  {"P&L":>9}  {"Cum P&L":>10}  Bar')
print(f'  {"-"*10} {"-"*4} {"-"*4}  {"-"*5}  {"-"*9}  {"-"*10}  {"-"*14}')
months = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0})
for t in closed:
    k = t['executed_at'].strftime('%Y-%m')
    months[k]['wins']   += 1 if t['realized_pnl'] > 0 else 0
    months[k]['losses'] += 1 if t['realized_pnl'] <= 0 else 0
    months[k]['pnl']    += t['realized_pnl']
cum = 0.0
for k in sorted(months):
    v = months[k]
    n = v['wins'] + v['losses']
    wr = v['wins'] / n * 100 if n else 0
    cum += v['pnl']
    print(f'  {k:<10} {v["wins"]:>4} {v["losses"]:>4}  {wr:>4.1f}%  ${v["pnl"]:>+8.2f}  ${cum:>+9.2f}  {bar(v["wins"], n, 14)}')

print()
print(sep)
