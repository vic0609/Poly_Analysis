#!/bin/bash
# Start both the monitor and Telegram bot, killing any existing instances first.

PYTHON=".venv/Scripts/python.exe"
LOG_DIR="logs"
PID_FILE="$LOG_DIR/pids"

echo "Stopping any running instances..."
if [ -f "$PID_FILE" ]; then
    while read -r pid; do
        kill "$pid" 2>/dev/null
    done < "$PID_FILE"
    rm -f "$PID_FILE"
fi
# Fallback: kill any remaining python processes with these scripts
taskkill //F //IM python.exe 2>/dev/null || true
sleep 3

echo "Starting monitor..."
$PYTHON main.py > "$LOG_DIR/monitor.log" 2>&1 &
MONITOR_PID=$!

echo "Starting Telegram bot..."
$PYTHON telegram_bot.py > "$LOG_DIR/telegram_bot.log" 2>&1 &
BOT_PID=$!

# Save PIDs for next clean shutdown
echo "$MONITOR_PID" > "$PID_FILE"
echo "$BOT_PID" >> "$PID_FILE"

echo "Monitor PID:      $MONITOR_PID"
echo "Telegram bot PID: $BOT_PID"

# Wait and confirm
sleep 5
if ps -p $MONITOR_PID > /dev/null 2>&1; then
    echo "Monitor:      running"
else
    echo "Monitor:      FAILED — check logs/monitor.log"
fi

if ps -p $BOT_PID > /dev/null 2>&1; then
    echo "Telegram bot: running"
else
    echo "Telegram bot: FAILED — check logs/telegram_bot.log"
fi
