#!/bin/bash
# start_all_bots.sh
#
# Starts all 5 live trading bots (1HZ25V, R_100, 1HZ75V, 1HZ90V,
# 1HZ100V) AND the Telegram responder process, each in its own tmux
# session, with a deliberate delay between each start -- this avoids
# all bots fighting for MetaApi's single-subscription-per-account
# limit simultaneously (confirmed real production issue).
#
# Run this ON THE SERVER -- this single command starts EVERYTHING:
#   bash start_all_bots.sh
#
# Safe to re-run -- skips any bot/process whose session already
# exists rather than starting a duplicate.

PROJECT_DIR="$HOME/mt5-trading-bot"
DELAY_SECONDS=20

declare -A BOTS=(
  ["bot_1hz25v"]="src/live/live_mt5_trading_bot.py"
  ["bot_r100"]="live_bots/R_100/live_mt5_trading_bot.py"
  ["bot_1hz75v"]="live_bots/1HZ75V/live_mt5_trading_bot.py"
  ["bot_1hz90v"]="live_bots/1HZ90V/live_mt5_trading_bot.py"
  ["bot_1hz100v"]="live_bots/1HZ100V/live_mt5_trading_bot.py"
  ["telegram_responder"]="src/live/telegram_responder.py"
)

ORDER=("bot_1hz25v" "bot_r100" "bot_1hz75v" "bot_1hz90v" "bot_1hz100v" "telegram_responder")

for session_name in "${ORDER[@]}"; do
  script_path="${BOTS[$session_name]}"

  if tmux has-session -t "$session_name" 2>/dev/null; then
    echo "SKIP: session '$session_name' already exists -- leaving it alone."
    echo "      (if it's not actually running the bot, kill it first with: tmux kill-session -t $session_name)"
    continue
  fi

  echo "Starting '$session_name' ($script_path) ..."
  tmux new-session -d -s "$session_name" \
    "cd '$PROJECT_DIR' && source venv/bin/activate && while true; do python '$script_path'; echo '[$(date)] Bot process exited (code '\$?'). Restarting in 15s...'; sleep 15; done"

  echo "  Started. Waiting ${DELAY_SECONDS}s before starting the next bot ..."
  sleep "$DELAY_SECONDS"
done

echo ""
echo "Done. Current sessions:"
tmux list-sessions