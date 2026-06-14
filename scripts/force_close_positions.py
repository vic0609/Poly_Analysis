"""Force-close specific paper positions by ID using current market price."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from datetime import datetime
from db.database import get_db
from db.models import Position, ExecutedTrade, Market

POSITION_IDS = [370, 534, 591]
REASON = "manual_close"

with get_db() as db:
    closed = []
    for pid in POSITION_IDS:
        pos = db.query(Position).filter_by(id=pid, status="open").first()
        if not pos:
            print(f"  Position {pid}: not found or already closed — skipping")
            continue

        # Get current market price
        market = db.query(Market).filter_by(id=pos.market_id).first()
        yes_price = market.yes_price if market and market.yes_price else None

        if yes_price is None:
            # Market gone — settle at entry (zero P&L)
            yes_price = pos.entry_price if pos.side == "YES" else (1.0 - pos.entry_price)
            print(f"  Position {pid}: no current price found, settling at entry price")

        exit_price = yes_price if pos.side == "YES" else (1.0 - yes_price)
        pnl = round((exit_price - pos.entry_price) * (pos.shares or 0), 4)

        # Close position
        pos.status      = "closed"
        pos.exit_price  = exit_price
        pos.realized_pnl = pnl
        pos.exit_reason = REASON
        pos.closed_at   = datetime.utcnow()

        # Update matching trade record
        trade = (
            db.query(ExecutedTrade)
            .filter_by(market_id=pos.market_id, side=pos.side)
            .order_by(ExecutedTrade.executed_at.desc())
            .first()
        )
        if trade:
            trade.fill_price   = exit_price
            trade.realized_pnl = pnl

        closed.append((pid, pos.side, pos.entry_price, exit_price, pos.size_usdc, pnl))

sep  = '=' * 80
thin = '-' * 80
print()
print(sep)
print('  FORCE CLOSE RESULTS  --  ' + datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))
print(sep)
print()
print('  %-6s  %-4s  %-8s  %-8s  %-8s  %-10s' % (
    'ID', 'Side', 'Entry', 'Exit', 'Size', 'P&L'))
print('  ' + thin)
total_pnl = 0.0
for pid, side, entry, exit_p, size, pnl in closed:
    total_pnl += pnl
    print('  %-6d  %-4s  %-8.3f  %-8.3f  $%-7.2f  %+.2f' % (
        pid, side, entry, exit_p, size or 0, pnl))
print('  ' + thin)
print('  Total closed: %d  |  Net P&L: %+.2f' % (len(closed), total_pnl))
print()
print(sep)
