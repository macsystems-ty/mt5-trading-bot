"""
telegram_responder.py

A SEPARATE, standalone process (runs independently of all trading
bots) that provides INTERACTIVE Telegram buttons:
  - Status, Trade Details, Show Peak, Pause/Resume, Why No Trade?,
    Force Refresh Data, Show Settings, Connection Health, Recent Trades

Reads market status from STATUS_DIR (written by each trading bot's
write_status_file()) -- automatically discovers ANY market with a
status file there, so adding XAUUSD/BTCUSD later requires NO changes
here at all.

Run this as its OWN persistent process (its own tmux session),
SEPARATE from the trading bots:
    python telegram_responder.py
"""

import asyncio
import glob
import json
import os
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATUS_DIR = os.path.join(os.path.expanduser("~"), "mt5-trading-bot", "bot_status")
CONTROL_DIR = os.path.join(os.path.expanduser("~"), "mt5-trading-bot", "bot_control")

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def list_known_markets():
    if not os.path.isdir(STATUS_DIR):
        return []
    files = glob.glob(os.path.join(STATUS_DIR, "*.json"))
    return sorted(os.path.splitext(os.path.basename(f))[0] for f in files)


def load_status(market: str):
    path = os.path.join(STATUS_DIR, f"{market}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def build_market_menu_keyboard():
    markets = list_known_markets()
    if not markets:
        return {"inline_keyboard": [[{"text": "No markets found yet", "callback_data": "noop"}]]}
    rows = [[{"text": "🌐 All Markets Overview", "callback_data": "overview"}]]
    rows += [[{"text": m, "callback_data": f"market:{m}"}] for m in markets]
    return {"inline_keyboard": rows}


def build_action_menu_keyboard(market: str):
    actions = [
        ("📊 Status", "status"),
        ("🔍 Trade Details", "details"),
        ("📈 Show Peak", "peak"),
        ("⏸️ Pause/Resume", "pause"),
        ("📉 Why No Trade?", "why"),
        ("🔄 Force Refresh Data", "refresh"),
        ("⚙️ Show Settings", "settings"),
        ("🌡️ Connection Health", "health"),
        ("📜 Recent Trades", "recent"),
    ]
    rows = [[{"text": label, "callback_data": f"action:{market}:{code}"}] for label, code in actions]
    rows.append([{"text": "« Back to markets", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def format_overview() -> str:
    markets = list_known_markets()
    if not markets:
        return "No markets found yet."

    lines = ["<b>🌐 All Markets Overview</b>\n"]
    for market in markets:
        status = load_status(market)
        if status is None:
            lines.append(f"<b>{market}</b>: no status data yet")
            continue

        trend = status.get("current_trend", "?")
        trend_emoji = {"UP": "📈", "DOWN": "📉", "FLAT": "➖"}.get(trend, "❔")

        pos = status.get("open_position")
        pos_text = "no open position"
        if pos:
            pos_text = f"{pos['direction']} @ {pos['entry_price']:.4f}"

        recent = status.get("recent_trades", [])
        last10 = recent[-10:]
        wins = sum(1 for t in last10 if t.get("pct_change", 0) > 0)
        losses = len(last10) - wins

        lines.append(
            f"<b>{market}</b>\n"
            f"  Trend: {trend_emoji} {trend} | Position: {pos_text}\n"
            f"  Balance: ${status.get('balance', 0):.2f} | "
            f"Last {len(last10)}: {wins}W/{losses}L"
        )

    return "\n".join(lines)


def format_status(status) -> str:
    if status is None:
        return "No status data found for this market yet -- the bot may not have completed its first heartbeat."
    pos = status.get("open_position")
    pos_text = "None (no open position)"
    if pos:
        pos_text = (
            f"{pos['direction']} @ {pos['entry_price']:.4f}, vol={pos['volume']}, "
            f"stop={pos['trailing_stop']:.4f}, Stage3 active={pos['stage3_active']}"
        )
    return (
        f"<b>{status.get('symbol', '?')}</b>\n"
        f"Balance: ${status.get('balance', 0):.2f}\n"
        f"Open position: {pos_text}\n"
        f"Trades completed: {status.get('trades_completed', 0)}\n"
        f"Last updated: {status.get('last_updated', '?')}"
    )


def format_details(status) -> str:
    if status is None or not status.get("open_position"):
        return "No open position right now, so there's nothing to show details for."
    pos = status["open_position"]
    entry = pos["entry_price"]
    stop = pos["trailing_stop"]
    distance_pct = abs(entry - stop) / entry * 100 if entry else 0
    return (
        f"<b>Open Position Details</b>\n"
        f"Direction: {pos['direction']}\n"
        f"Entry: {entry:.4f}\n"
        f"Current stop: {stop:.4f}\n"
        f"Distance to stop: {distance_pct:.4f}%\n"
        f"Volume: {pos['volume']}\n"
        f"Stage 3 (tight trailing) active: {pos['stage3_active']}"
    )


def format_peak(status) -> str:
    if status is None:
        return "No status data available."
    recent = status.get("recent_trades", [])
    if not recent:
        return "No completed trades recorded yet for this market."
    lines = ["<b>Peak Profit vs Final Result (most recent trades)</b>"]
    for t in recent[-5:]:
        lines.append(
            f"{t.get('direction', '?')}: peak ≈${t.get('peak_dollar', 0):+.2f} "
            f"-> closed ≈${t.get('dollar_pnl', 0):+.2f} ({t.get('reason', '?')})"
        )
    return "\n".join(lines)


def format_why_no_trade(status) -> str:
    if status is None:
        return "No status data available."
    rejections = status.get("trend_filter_rejections", 0)
    return (
        f"<b>Why no trade right now?</b>\n"
        f"Trend-strength filter rejections so far: {rejections}\n"
        f"(If this number is climbing steadily with no trades opening, "
        f"price is likely sitting in the 'overextended' zone our filter "
        f"deliberately skips. If trend itself is FLAT, no entry is "
        f"possible at all regardless of the filter -- check the bot's "
        f"own heartbeat logs for the current trend value.)"
    )


def format_settings(status) -> str:
    if status is None:
        return "No status data available."
    return (
        f"<b>Current Settings — {status.get('symbol', '?')}</b>\n"
        f"Fixed Stage 1 stop: {status.get('fixed_stage1_stop_pct', '?')}%\n"
        f"Max trend strength (entry filter): {status.get('max_trend_strength_pct', '?')}%\n"
        f"Risk per trade: {status.get('risk_per_trade_pct', '?')}%"
    )


def format_health(status) -> str:
    if status is None:
        return "No status data found -- this bot may not be running, or hasn't written its first status yet."
    last_updated_str = status.get("last_updated")
    try:
        last_updated = datetime.fromisoformat(last_updated_str)
        age_seconds = (datetime.now(timezone.utc) - last_updated).total_seconds()
        freshness = "🟢 Fresh" if age_seconds < 600 else "🟡 Stale" if age_seconds < 3600 else "🔴 Very stale"
        return (
            f"<b>Connection/Process Health — {status.get('symbol', '?')}</b>\n"
            f"Last status update: {last_updated_str}\n"
            f"Age: {age_seconds:.0f}s ago\n"
            f"{freshness} (status files update every ~5 minutes during normal operation)"
        )
    except Exception:  # noqa: BLE001
        return f"Could not parse last update time: {last_updated_str}"


def format_recent_trades(status) -> str:
    if status is None:
        return "No status data available."
    recent = status.get("recent_trades", [])
    if not recent:
        return "No completed trades recorded yet for this market."
    lines = ["<b>Last 10 Trades</b>"]
    for t in recent[-10:]:
        emoji = "✅" if t.get("pct_change", 0) > 0 else "❌"
        lines.append(
            f"{emoji} {t.get('direction', '?')} {t.get('pct_change', 0):+.4f}% "
            f"(≈${t.get('dollar_pnl', 0):+.2f}) [{t.get('reason', '?')}]"
        )
    return "\n".join(lines)


async def handle_pause_toggle(market: str) -> str:
    """
    NOTE: writes/removes a pause-flag file. This REQUIRES the trading
    bot itself to check for this flag before opening new entries --
    NOT yet wired into live_mt5_trading_bot.py. This responder writes
    the flag correctly, but check_for_entry() needs a small addition
    to actually honor it. Flagging clearly so it isn't mistaken for
    already done.
    """
    os.makedirs(CONTROL_DIR, exist_ok=True)
    flag_path = os.path.join(CONTROL_DIR, f"{market}.paused")
    if os.path.exists(flag_path):
        os.remove(flag_path)
        return f"▶️ {market}: RESUMED. The bot will look for new entries again (once it checks this flag)."
    else:
        with open(flag_path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        return f"⏸️ {market}: PAUSED. No new entries will open (once the bot checks this flag). Existing open positions are NOT affected."


async def handle_force_refresh(market: str) -> str:
    os.makedirs(CONTROL_DIR, exist_ok=True)
    flag_path = os.path.join(CONTROL_DIR, f"{market}.refresh_requested")
    with open(flag_path, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    return (
        f"🔄 {market}: refresh requested. (NOTE: the trading bot's background "
        f"refresh task needs a small addition to check for this flag -- not yet "
        f"wired in, this just writes the request.)"
    )


async def send_message(session, text, reply_markup=None):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with session.post(f"{API_BASE}/sendMessage", json=payload) as resp:
        return await resp.json()


async def edit_message(session, chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with session.post(f"{API_BASE}/editMessageText", json=payload) as resp:
        return await resp.json()


async def answer_callback_query(session, callback_query_id):
    async with session.post(f"{API_BASE}/answerCallbackQuery", json={"callback_query_id": callback_query_id}):
        pass


async def handle_callback(session, callback_query):
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    await answer_callback_query(session, callback_query.get("id"))

    if data == "menu" or data == "noop":
        await edit_message(session, chat_id, message_id, "Choose a market:", build_market_menu_keyboard())
        return

    if data == "overview":
        back_keyboard = {"inline_keyboard": [[{"text": "« Back to markets", "callback_data": "menu"}]]}
        await edit_message(session, chat_id, message_id, format_overview(), back_keyboard)
        return

    if data.startswith("market:"):
        market = data.split(":", 1)[1]
        await edit_message(session, chat_id, message_id, f"<b>{market}</b> -- choose an action:", build_action_menu_keyboard(market))
        return

    if data.startswith("action:"):
        _, market, action = data.split(":", 2)
        status = load_status(market)

        if action == "status":
            text = format_status(status)
        elif action == "details":
            text = format_details(status)
        elif action == "peak":
            text = format_peak(status)
        elif action == "pause":
            text = await handle_pause_toggle(market)
        elif action == "why":
            text = format_why_no_trade(status)
        elif action == "refresh":
            text = await handle_force_refresh(market)
        elif action == "settings":
            text = format_settings(status)
        elif action == "health":
            text = format_health(status)
        elif action == "recent":
            text = format_recent_trades(status)
        else:
            text = "Unknown action."

        await edit_message(session, chat_id, message_id, text, build_action_menu_keyboard(market))
        return


async def poll_loop():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        return

    offset = 0
    async with aiohttp.ClientSession() as session:
        print("Telegram responder running. Send /menu in your Telegram chat to start.")
        while True:
            try:
                async with session.get(
                    f"{API_BASE}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    data = await resp.json()

                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    if "callback_query" in update:
                        await handle_callback(session, update["callback_query"])
                        continue

                    message = update.get("message", {})
                    text = message.get("text", "")
                    if text in ("/menu", "/start"):
                        await send_message(session, "Choose a market:", build_market_menu_keyboard())

            except Exception as exc:  # noqa: BLE001
                print(f"Poll loop error (will retry): {exc!r}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(poll_loop())