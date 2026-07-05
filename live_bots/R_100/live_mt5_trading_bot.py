"""
live_mt5_trading_bot.py

Automated trading bot for Deriv MT5 demo account, connected
DIRECTLY via the MetaTrader5 Python library (no MetaAPI).

Strategy (unchanged):
  - EMA(1H) trend filter
  - Support/Resistance + Engulfing candle entry (tolerance=0.05%)
  - Candle-based trailing stop exit (window=2)
  - Trend-reversal safety check while a position is open
  - Position sizing: risk a fixed % of balance per trade, sized in MT5 lots

Run with:
    python live_bots/1HZ25V/live_mt5_trading_bot.py
"""

import asyncio
import logging
import os
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import MetaTrader5 as mt5
import aiohttp
import pandas as pd
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Repo-root discovery (unchanged)
# ---------------------------------------------------------------------------

def _find_repo_root(start_path: str) -> str:
    current = os.path.abspath(start_path)
    for _ in range(10):
        candidate_strategy = os.path.join(current, "src", "strategy")
        candidate_data = os.path.join(current, "src", "data")
        if os.path.isdir(candidate_strategy) and os.path.isdir(candidate_data):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise RuntimeError(
        f"Could not find repo root by walking up from {start_path}. "
        f"Check your folder structure."
    )


_REPO_ROOT = _find_repo_root(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH     = os.path.join(_REPO_ROOT, "src", "data")
STRATEGY_PATH = os.path.join(_REPO_ROOT, "src", "strategy")
MARKET_DIR    = os.path.join(_REPO_ROOT, "markets", "R_100")  # EDIT per-market copy
HISTORICAL_DATA_DIR = os.path.join(MARKET_DIR, "data")
sys.path.insert(0, DATA_PATH)
sys.path.insert(0, STRATEGY_PATH)
sys.path.insert(0, MARKET_DIR)

HISTORICAL_DATA_REFRESH_INTERVAL_HOURS = 24

from candle_builder import Candle, CandleAggregator  # noqa: E402
import indicators  # noqa: E402
from candlestick_patterns import (  # noqa: E402
    Candle as PatternCandle,
    is_bullish_engulfing as patterns_is_bullish_engulfing,
    is_bearish_engulfing as patterns_is_bearish_engulfing,
    is_piercing_line,
    is_three_black_crows,
    is_falling_three_methods,
    is_shooting_star,
    is_morning_star,
    is_evening_star,
    is_dark_cloud_cover,
    is_three_white_soldiers,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL                        = "Volatility 100 Index"  # EDIT per-market copy
HISTORICAL_DATA_SYMBOL_PREFIX = "R_100"                    # EDIT per-market copy
STATUS_DIR = os.path.join(os.path.expanduser("~"), "mt5-trading-bot", "bot_status")

FIXED_STAGE1_STOP_PCT           = 0.463   # EDIT PER MARKET: 1HZ25V=0.057, 1HZ75V=0.344, 1HZ90V=0.417, 1HZ100V=0.476, R_100=0.463
MAX_TREND_STRENGTH_PCT          = 1.0407  # EDIT PER MARKET: 1HZ25V=0.2838, 1HZ75V=0.7879, 1HZ90V=0.9165, 1HZ100V=1.0529, R_100=1.0407
NUM_FAVORABLE_CANDLES_REQUIRED  = 2
STAGE3_TRAILING_WINDOW          = 2
RETEST_TOLERANCE_PCT            = 0.05
SWING_LOOKBACK                  = 3
LEVEL_AGE_CAP                   = 200
MAX_PRICE_STALENESS_SECONDS     = 30

RISK_PER_TRADE_PCT = 1.0
MIN_VOLUME         = 1.0
MAX_VOLUME         = 220.0
VOLUME_STEP        = 0.01
MAGIC_NUMBER       = 234001  # unique identifier for orders placed by this bot

PRICE_POLL_INTERVAL_SECONDS = 1.0

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(
    LOG_DIR,
    f"mt5_trading_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
)
LIVE_CANDLES_CSV_PATH = os.path.join(
    LOG_DIR, f"{SYMBOL.replace(' ', '_')}_5min_live.csv"
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger("mt5_live_bot")
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Dataclasses (unchanged)
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    position_id: int       # MT5 ticket number
    direction: str
    entry_price: float
    volume: float
    trailing_stop: float
    level_price: float
    entry_time: Optional[datetime] = None
    favorable_candle_count: int = 0
    stage3_active: bool = False


@dataclass
class SwingLevel:
    index: int
    price: float
    level_type: str


@dataclass
class BotState:
    candles_5min: List[Candle] = field(default_factory=list)
    candles_1h: List[Candle] = field(default_factory=list)
    swing_levels: List[SwingLevel] = field(default_factory=list)
    open_position: Optional[OpenPosition] = None
    current_balance: float = 0.0
    trades_completed: int = 0
    trend_filter_rejections: int = 0
    recent_trades_log: list = field(default_factory=list)
    last_known_trend: Optional[str] = None


# ---------------------------------------------------------------------------
# MT5 helpers
# ---------------------------------------------------------------------------

def mt5_initialize() -> bool:
    """Initialize MT5 connection. Returns True on success."""
    if not mt5.initialize():
        logger.error(f"MT5 initialize failed: {mt5.last_error()}")
        return False
    account = mt5.account_info()
    if account is None:
        logger.error("Could not get MT5 account info.")
        return False
    logger.info(
        f"MT5 connected: account={account.login} "
        f"server={account.server} "
        f"balance={account.balance:.2f} {account.currency}"
    )
    return True


def get_balance() -> float:
    info = mt5.account_info()
    return info.balance if info else 0.0


def get_current_tick() -> Optional[dict]:
    """Get current tick for SYMBOL. Returns dict with bid/ask/time or None."""
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    return {
        "bid":  tick.bid,
        "ask":  tick.ask,
        "time": datetime.fromtimestamp(tick.time, tz=timezone.utc),
    }


def round_volume(volume: float) -> float:
    steps  = int(volume / VOLUME_STEP)
    result = steps * VOLUME_STEP
    result = max(MIN_VOLUME, min(MAX_VOLUME, result))
    return round(result, 3)


def calculate_volume(balance: float, current_price: float, stop_distance_pct: float) -> float:
    risk_dollars          = balance * (RISK_PER_TRADE_PCT / 100)
    stop_distance_fraction = max(stop_distance_pct, 0.001) / 100
    raw_volume            = risk_dollars / (current_price * stop_distance_fraction)
    return round_volume(raw_volume)


def get_open_position() -> Optional[mt5.TradePosition]:
    """Return the first open position for SYMBOL placed by this bot, or None."""
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        return None
    for p in positions:
        if p.magic == MAGIC_NUMBER:
            return p
    return None


def fetch_1h_candles_from_mt5(count: int = 500) -> List[Candle]:
    """
    Fetch 1H candles DIRECTLY from MT5 — much more accurate than
    building them from 5min CSV data which can be stale.
    """
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, count)
    if rates is None or len(rates) == 0:
        logger.error(f"Could not fetch 1H candles from MT5: {mt5.last_error()}")
        return []

    candles = []
    for r in rates:
        candles.append(Candle(
            timeframe="1h",
            open_time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            tick_count=0,
        ))
    logger.info(f"Fetched {len(candles)} 1H candles directly from MT5.")
    return candles


def refresh_1h_candles_from_mt5(state: BotState) -> None:
    """
    Update state.candles_1h with the latest 1H candles from MT5.
    Called periodically to keep the trend filter accurate.
    """
    new_candles = fetch_1h_candles_from_mt5(count=500)
    if new_candles:
        state.candles_1h = new_candles
        logger.info(f"1H candles refreshed from MT5: {len(state.candles_1h)} candles.")



    """Modify the broker-side stop loss for an open position."""
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol":   SYMBOL,
        "sl":       new_sl,
        "tp":       0.0,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.warning(
            f"SL update failed for ticket {ticket}: "
            f"{result.retcode if result else 'None'} - "
            f"{result.comment if result else mt5.last_error()}"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Telegram (unchanged)
# ---------------------------------------------------------------------------

async def send_telegram_notification(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    labeled = f"<b>[{SYMBOL}]</b>\n{message}"
    url      = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload  = {"chat_id": TELEGRAM_CHAT_ID, "text": labeled, "parse_mode": "HTML"}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Telegram failed ({resp.status}): {body}")
    except Exception as exc:
        logger.warning(f"Telegram failed: {exc!r}")


# ---------------------------------------------------------------------------
# Status file (unchanged)
# ---------------------------------------------------------------------------

def write_status_file(state: BotState) -> None:
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        status = {
            "symbol":       SYMBOL,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "balance":      state.current_balance,
            "open_position": (
                {
                    "position_id":   state.open_position.position_id,
                    "direction":     state.open_position.direction,
                    "entry_price":   state.open_position.entry_price,
                    "volume":        state.open_position.volume,
                    "trailing_stop": state.open_position.trailing_stop,
                    "stage3_active": state.open_position.stage3_active,
                }
                if state.open_position else None
            ),
            "trades_completed":        state.trades_completed,
            "trend_filter_rejections": state.trend_filter_rejections,
            "current_trend":           state.last_known_trend,
            "recent_trades":           state.recent_trades_log[-10:],
            "fixed_stage1_stop_pct":   FIXED_STAGE1_STOP_PCT,
            "max_trend_strength_pct":  MAX_TREND_STRENGTH_PCT,
            "risk_per_trade_pct":      RISK_PER_TRADE_PCT,
        }
        tmp   = os.path.join(STATUS_DIR, f"{HISTORICAL_DATA_SYMBOL_PREFIX}.json.tmp")
        final = os.path.join(STATUS_DIR, f"{HISTORICAL_DATA_SYMBOL_PREFIX}.json")
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2, default=str)
        os.replace(tmp, final)
    except Exception as exc:
        logger.warning(f"Could not write status file: {exc!r}")


# ---------------------------------------------------------------------------
# CSV helper (unchanged)
# ---------------------------------------------------------------------------

def append_live_candle_to_csv(candle: Candle) -> None:
    file_exists = os.path.exists(LIVE_CANDLES_CSV_PATH)
    try:
        with open(LIVE_CANDLES_CSV_PATH, "a", newline="") as f:
            if not file_exists:
                f.write("open_time,open,high,low,close\n")
            f.write(
                f"{candle.open_time.isoformat()},{candle.open},{candle.high},"
                f"{candle.low},{candle.close}\n"
            )
    except OSError as exc:
        logger.error(f"Failed to write live candle to CSV: {exc!r}")


# ---------------------------------------------------------------------------
# Strategy helpers (100% unchanged)
# ---------------------------------------------------------------------------

def _to_pattern_candle(c: Candle) -> PatternCandle:
    return PatternCandle(open=c.open, high=c.high, low=c.low, close=c.close)


def matches_any_buy_pattern(candles_5min: list) -> bool:
    if len(candles_5min) < 2:
        return False
    prev, cur = candles_5min[-2], candles_5min[-1]
    if patterns_is_bullish_engulfing(_to_pattern_candle(prev), _to_pattern_candle(cur)):
        return True
    if is_piercing_line(_to_pattern_candle(prev), _to_pattern_candle(cur)):
        return True
    if len(candles_5min) >= 3:
        c1, c2, c3 = candles_5min[-3], candles_5min[-2], candles_5min[-1]
        if is_morning_star(_to_pattern_candle(c1), _to_pattern_candle(c2), _to_pattern_candle(c3)):
            return True
        if is_three_white_soldiers(_to_pattern_candle(c1), _to_pattern_candle(c2), _to_pattern_candle(c3)):
            return True
    return False


def matches_any_sell_pattern(candles_5min: list) -> bool:
    if len(candles_5min) < 1:
        return False
    cur = candles_5min[-1]
    if is_shooting_star(_to_pattern_candle(cur)):
        return True
    if len(candles_5min) >= 2:
        prev = candles_5min[-2]
        if patterns_is_bearish_engulfing(_to_pattern_candle(prev), _to_pattern_candle(cur)):
            return True
        if is_dark_cloud_cover(_to_pattern_candle(prev), _to_pattern_candle(cur)):
            return True
    if len(candles_5min) >= 3:
        c1, c2, c3 = candles_5min[-3], candles_5min[-2], candles_5min[-1]
        if is_three_black_crows(_to_pattern_candle(c1), _to_pattern_candle(c2), _to_pattern_candle(c3)):
            return True
        if is_evening_star(_to_pattern_candle(c1), _to_pattern_candle(c2), _to_pattern_candle(c3)):
            return True
    if len(candles_5min) >= 5:
        last5 = [_to_pattern_candle(c) for c in candles_5min[-5:]]
        if is_falling_three_methods(last5):
            return True
    return False


def update_swing_levels(state: BotState, quiet: bool = False) -> None:
    candles = state.candles_5min
    if len(candles) < SWING_LOOKBACK * 2 + 1:
        return
    i = len(candles) - SWING_LOOKBACK - 1
    if i < SWING_LOOKBACK:
        return
    window = candles[i - SWING_LOOKBACK: i + SWING_LOOKBACK + 1]
    candle = candles[i]
    if candle.high == max(c.high for c in window):
        state.swing_levels.append(SwingLevel(index=i, price=candle.high, level_type="resistance"))
        if not quiet:
            logger.info(f"New resistance level: {candle.high:.5f}")
    if candle.low == min(c.low for c in window):
        state.swing_levels.append(SwingLevel(index=i, price=candle.low, level_type="support"))
        if not quiet:
            logger.info(f"New support level: {candle.low:.5f}")
    state.swing_levels = state.swing_levels[-500:]


def compute_current_trend(state: BotState):
    if len(state.candles_1h) < 20:
        return None, None
    df = pd.DataFrame({
        "open":  [c.open  for c in state.candles_1h],
        "high":  [c.high  for c in state.candles_1h],
        "low":   [c.low   for c in state.candles_1h],
        "close": [c.close for c in state.candles_1h],
    })
    result   = indicators.add_all_indicators(df)
    ema      = result["ema_14"]
    last_ema = ema.iloc[-1]
    prev_ema = ema.iloc[-2]
    if pd.isna(last_ema) or pd.isna(prev_ema):
        return None, None
    current_price = state.candles_5min[-1].close if state.candles_5min else None
    if current_price is None:
        return None, None
    ema_rising  = last_ema > prev_ema
    price_above = current_price > last_ema
    if price_above and ema_rising:
        return "UP", last_ema
    if not price_above and not ema_rising:
        return "DOWN", last_ema
    return "FLAT", last_ema


# ---------------------------------------------------------------------------
# Trade execution (rewritten for direct MT5)
# ---------------------------------------------------------------------------

async def open_trade(state: BotState, direction: str, level_price: float, stop_distance_pct: float) -> None:
    tick = get_current_tick()
    if tick is None:
        logger.error("Cannot get current tick — aborting trade open.")
        return

    current_price = (tick["bid"] + tick["ask"]) / 2
    volume        = calculate_volume(state.current_balance, current_price, stop_distance_pct)

    if direction == "BUY":
        order_type     = mt5.ORDER_TYPE_BUY
        price          = tick["ask"]
        stop_loss      = price * (1 - FIXED_STAGE1_STOP_PCT / 100)
    else:
        order_type     = mt5.ORDER_TYPE_SELL
        price          = tick["bid"]
        stop_loss      = price * (1 + FIXED_STAGE1_STOP_PCT / 100)

    # Get symbol info for filling mode
    symbol_info = mt5.symbol_info(SYMBOL)
    if symbol_info is None:
        logger.error(f"Symbol info not available for {SYMBOL}")
        return

    filling_type = mt5.ORDER_FILLING_IOC
    if symbol_info.filling_mode == 1:
        filling_type = mt5.ORDER_FILLING_FOK

    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      SYMBOL,
        "volume":      volume,
        "type":        order_type,
        "price":       price,
        "sl":          stop_loss,
        "tp":          0.0,
        "deviation":   20,
        "magic":       MAGIC_NUMBER,
        "comment":     "sr_engulfing_trend",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }

    logger.info(
        f"Opening {direction}: volume={volume} lots, "
        f"price={price:.5f}, sl={stop_loss:.5f}"
    )

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error = result.comment if result else str(mt5.last_error())
        retcode = result.retcode if result else "None"
        logger.error(f"Order failed: retcode={retcode} comment={error}")
        return

    ticket      = result.order
    entry_price = result.price

    state.open_position = OpenPosition(
        position_id=ticket,
        direction=direction,
        entry_price=entry_price,
        volume=volume,
        trailing_stop=stop_loss,
        level_price=level_price,
        entry_time=datetime.now(timezone.utc),
    )

    logger.info(
        f"✅ TRADE OPENED: {direction} ticket={ticket} "
        f"volume={volume} entry={entry_price:.5f}"
    )
    await send_telegram_notification(
        f"🟢 <b>TRADE OPENED</b>\n"
        f"Direction: {direction}\n"
        f"Volume: {volume} lots\n"
        f"Entry: {entry_price:.5f}\n"
        f"Stop Loss: {stop_loss:.5f}\n"
        f"Balance: ${state.current_balance:.2f}"
    )


async def close_trade(state: BotState, reason: str) -> None:
    position = state.open_position
    if position is None:
        return

    tick = get_current_tick()
    if tick is None:
        logger.error("Cannot get tick for close — will retry next cycle.")
        return

    if position.direction == "BUY":
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick["bid"]
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick["ask"]

    symbol_info  = mt5.symbol_info(SYMBOL)
    filling_type = mt5.ORDER_FILLING_IOC
    if symbol_info and symbol_info.filling_mode == 1:
        filling_type = mt5.ORDER_FILLING_FOK

    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      SYMBOL,
        "volume":      position.volume,
        "type":        order_type,
        "position":    position.position_id,
        "price":       price,
        "deviation":   20,
        "magic":       MAGIC_NUMBER,
        "comment":     f"close:{reason}",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }

    result = mt5.order_send(request)

    # Check if position is actually closed regardless of retcode
    # retcode 1 = 'Success' on some brokers (non-standard but valid)
    # retcode 10009 = TRADE_RETCODE_DONE (standard success)
    position_still_open = get_open_position() is not None

    if result is None or (result.retcode != mt5.TRADE_RETCODE_DONE and result.retcode != 1):
        if position_still_open:
            error = result.comment if result else str(mt5.last_error())
            logger.error(f"Close failed: retcode={result.retcode if result else 'None'} error={error}")
            await send_telegram_notification(
                f"⚠️ <b>CLOSE FAILED</b>\n"
                f"Ticket: {position.position_id}\n"
                f"Retcode: {result.retcode if result else 'None'}\n"
                f"Error: {error}"
            )
            return
        else:
            logger.warning(f"Close returned retcode={result.retcode if result else 'None'} but position is gone -- treating as closed.")

    elif position_still_open:
        logger.warning(f"Close retcode={result.retcode} but position still open -- retrying...")
        await asyncio.sleep(2)
        position_still_open = get_open_position() is not None
        if position_still_open:
            logger.error(f"Position still open after retry -- giving up.")
            await send_telegram_notification(
                f"⚠️ <b>CLOSE FAILED - POSITION STILL OPEN</b>\n"
                f"Ticket: {position.position_id}\n"
                f"Please close manually in MT5!"
            )
            return

    state.trades_completed += 1
    exit_price = result.price if (result and result.retcode == mt5.TRADE_RETCODE_DONE) else price

    if position.direction == "BUY":
        pct_change = (exit_price - position.entry_price) / position.entry_price * 100
    else:
        pct_change = (position.entry_price - exit_price) / position.entry_price * 100

    dollar_pnl    = position.volume * position.entry_price * (pct_change / 100)
    outcome_emoji = "✅" if pct_change > 0 else ("➖" if abs(pct_change) < 0.001 else "❌")

    state.recent_trades_log.append({
        "position_id": position.position_id,
        "direction":   position.direction,
        "entry_price": position.entry_price,
        "exit_price":  exit_price,
        "pct_change":  pct_change,
        "dollar_pnl":  dollar_pnl,
        "reason":      reason,
        "closed_at":   datetime.now(timezone.utc).isoformat(),
    })
    state.recent_trades_log = state.recent_trades_log[-20:]

    logger.info(
        f"🔚 TRADE CLOSED ({reason}): ticket={position.position_id} "
        f"P&L: {pct_change:+.4f}% (≈${dollar_pnl:+.2f}) "
        f"[total: {state.trades_completed}]"
    )
    await send_telegram_notification(
        f"{outcome_emoji} <b>TRADE CLOSED</b>\n"
        f"Direction: {position.direction}\n"
        f"Reason: {reason}\n"
        f"Entry: {position.entry_price:.5f} → Exit: {exit_price:.5f}\n"
        f"Result: {pct_change:+.4f}% (≈${dollar_pnl:+.2f})\n"
        f"Balance: ${state.current_balance:.2f}\n"
        f"Total trades: {state.trades_completed}"
    )
    state.open_position = None


async def recover_open_position_if_any(state: BotState) -> None:
    """Check for existing open positions at startup and resume managing them."""
    mt5_pos = get_open_position()
    if mt5_pos is None:
        logger.info("No existing open position found. Starting clean.")
        return

    direction = "BUY" if mt5_pos.type == mt5.ORDER_TYPE_BUY else "SELL"
    if direction == "BUY":
        recovered_stop = mt5_pos.sl if mt5_pos.sl > 0 else mt5_pos.price_open * (1 - FIXED_STAGE1_STOP_PCT / 100)
    else:
        recovered_stop = mt5_pos.sl if mt5_pos.sl > 0 else mt5_pos.price_open * (1 + FIXED_STAGE1_STOP_PCT / 100)

    state.open_position = OpenPosition(
        position_id=mt5_pos.ticket,
        direction=direction,
        entry_price=mt5_pos.price_open,
        volume=mt5_pos.volume,
        trailing_stop=recovered_stop,
        level_price=mt5_pos.price_open,
        entry_time=datetime.fromtimestamp(mt5_pos.time, tz=timezone.utc),
        favorable_candle_count=0,
        stage3_active=False,
    )
    logger.warning(
        f"⚠️ POSITION RECOVERED: {direction} ticket={mt5_pos.ticket} "
        f"entry={mt5_pos.price_open} sl={recovered_stop:.5f}"
    )
    await send_telegram_notification(
        f"⚠️ <b>POSITION RECOVERED ON STARTUP</b>\n"
        f"Direction: {direction}\n"
        f"Ticket: {mt5_pos.ticket}\n"
        f"Entry: {mt5_pos.price_open:.5f}\n"
        f"Resuming management now."
    )


# ---------------------------------------------------------------------------
# Position management (unchanged logic)
# ---------------------------------------------------------------------------

async def manage_open_position(state: BotState) -> None:
    position = state.open_position
    if position is None:
        return

    # Verify position still exists on broker
    mt5_pos = get_open_position()
    if mt5_pos is None:
        logger.warning(
            f"Position {position.position_id} no longer exists on broker "
            f"(likely hit broker-side SL). Clearing local state."
        )
        state.trades_completed += 1
        state.open_position = None
        await send_telegram_notification(
            f"⚠️ <b>POSITION CLOSED BY BROKER</b>\n"
            f"Ticket: {position.position_id}\n"
            f"Likely hit broker-side stop loss."
        )
        return

    current_trend, _ = compute_current_trend(state)
    position_expects  = "UP" if position.direction == "BUY" else "DOWN"
    if (
        current_trend is not None
        and current_trend != position_expects
        and current_trend != "FLAT"
    ):
        logger.warning(f"Trend reversed to {current_trend} -- closing early.")
        await close_trade(state, reason=f"trend reversed to {current_trend}")
        return

    current_candle = state.candles_5min[-1]
    if len(state.candles_5min) < 2:
        return

    if position.stage3_active:
        prior_candles = state.candles_5min[-(STAGE3_TRAILING_WINDOW + 1):-1]
        if position.direction == "BUY":
            new_stop = min(c.low for c in prior_candles) if prior_candles else position.trailing_stop
            if new_stop > position.trailing_stop:
                position.trailing_stop = new_stop
                update_sl_on_broker(position.position_id, position.trailing_stop)
            if current_candle.low <= position.trailing_stop:
                await close_trade(state, reason="stage3 trailing stop hit")
        else:
            new_stop = max(c.high for c in prior_candles) if prior_candles else position.trailing_stop
            if new_stop < position.trailing_stop:
                position.trailing_stop = new_stop
                update_sl_on_broker(position.position_id, position.trailing_stop)
            if current_candle.high >= position.trailing_stop:
                await close_trade(state, reason="stage3 trailing stop hit")
        return

    hit_stop = (
        current_candle.low  <= position.trailing_stop if position.direction == "BUY"
        else current_candle.high >= position.trailing_stop
    )
    if hit_stop:
        await close_trade(state, reason="initial or breakeven stop hit")
        return

    is_favorable = (
        current_candle.close > current_candle.open
        if position.direction == "BUY"
        else current_candle.close < current_candle.open
    )
    if is_favorable:
        position.favorable_candle_count += 1
        if position.favorable_candle_count >= NUM_FAVORABLE_CANDLES_REQUIRED:
            position.trailing_stop = position.entry_price
            position.stage3_active = True
            update_sl_on_broker(position.position_id, position.trailing_stop)
            logger.info(
                f"Breakeven reached after {position.favorable_candle_count} candles "
                f"-- Stage 3 active. SL moved to entry {position.entry_price:.5f}"
            )
    else:
        position.favorable_candle_count = 0


async def check_for_entry(state: BotState) -> None:
    if state.open_position is not None:
        return
    if len(state.candles_5min) < SWING_LOOKBACK + 2:
        return

    trend, ema_value = compute_current_trend(state)
    if trend not in ("UP", "DOWN"):
        return

    prev_candle = state.candles_5min[-2]
    cur_candle  = state.candles_5min[-1]

    if ema_value is not None and ema_value != 0:
        trend_strength_pct = abs(cur_candle.close - ema_value) / ema_value * 100
        if trend_strength_pct > MAX_TREND_STRENGTH_PCT:
            state.trend_filter_rejections += 1
            if state.trend_filter_rejections % 10 == 1:
                logger.info(
                    f"Trend-strength rejection #{state.trend_filter_rejections}: "
                    f"{trend_strength_pct:.4f}% > {MAX_TREND_STRENGTH_PCT}%"
                )
            return

    current_index = len(state.candles_5min) - 1
    for level in reversed(state.swing_levels[-50:]):
        level_age = current_index - level.index
        if level_age > LEVEL_AGE_CAP:
            continue
        tolerance    = level.price * (RETEST_TOLERANCE_PCT / 100)
        retest_price = prev_candle.low if level.level_type == "support" else prev_candle.high
        if not (level.price - tolerance <= retest_price <= level.price + tolerance):
            continue
        if level.level_type == "support" and trend == "UP":
            if matches_any_buy_pattern(state.candles_5min):
                await open_trade(state, "BUY", level.price, FIXED_STAGE1_STOP_PCT)
                return
        if level.level_type == "resistance" and trend == "DOWN":
            if matches_any_sell_pattern(state.candles_5min):
                await open_trade(state, "SELL", level.price, FIXED_STAGE1_STOP_PCT)
                return


# ---------------------------------------------------------------------------
# Historical data preload (unchanged)
# ---------------------------------------------------------------------------

def preload_historical_data(state: BotState) -> None:
    csv_path = os.path.join(HISTORICAL_DATA_DIR, f"{HISTORICAL_DATA_SYMBOL_PREFIX}_5min.csv")
    if not os.path.exists(csv_path):
        logger.warning(f"No historical data at {csv_path} -- starting cold.")
        return
    logger.info(f"Preloading from {csv_path} ...")
    df = pd.read_csv(csv_path, index_col="open_time", parse_dates=True)
    df = df.sort_index()

    last_data_time = df.index[-1]
    now_utc = datetime.now(timezone.utc)
    if last_data_time.tzinfo is None:
        last_data_time = last_data_time.tz_localize(timezone.utc)
    staleness_hours = (now_utc - last_data_time).total_seconds() / 3600
    if staleness_hours > 24:
        logger.warning(f"⚠️ Historical data is {staleness_hours:.1f}h old.")
    else:
        logger.info(f"Historical data freshness OK: {staleness_hours:.1f}h old.")

    historical_candles: List[Candle] = []
    for open_time, row in df.iterrows():
        historical_candles.append(Candle(
            timeframe="5min",
            open_time=open_time.to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_count=0,
        ))

    state.candles_5min = historical_candles[-2000:]
    logger.info(f"Loaded {len(state.candles_5min)} historical 5min candles.")

    # Fetch 1H candles DIRECTLY from MT5 instead of building from 5min CSV
    # This ensures the trend filter uses accurate, up-to-date 1H data
    mt5_1h = fetch_1h_candles_from_mt5(count=500)
    if mt5_1h:
        state.candles_1h = mt5_1h
        logger.info(f"Loaded {len(state.candles_1h)} 1H candles directly from MT5.")
    else:
        # Fallback: build from 5min CSV if MT5 fetch fails
        logger.warning("MT5 1H fetch failed — building 1H from 5min CSV as fallback.")
        chunk_size  = 12
        full_chunks = len(historical_candles) // chunk_size
        for i in range(full_chunks):
            chunk = historical_candles[i * chunk_size: (i + 1) * chunk_size]
            state.candles_1h.append(Candle(
                timeframe="1h",
                open_time=chunk[0].open_time,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low  for c in chunk),
                close=chunk[-1].close,
                tick_count=0,
            ))
        state.candles_1h = state.candles_1h[-500:]
        logger.info(f"Built {len(state.candles_1h)} 1H candles from 5min CSV (fallback).")

    full_history = state.candles_5min[:]
    state.candles_5min = []
    for candle in full_history:
        state.candles_5min.append(candle)
        update_swing_levels(state, quiet=True)

    logger.info(f"Swing-level replay done: {len(state.swing_levels)} levels.")
    trend_check, _ = compute_current_trend(state)
    logger.info(f"Trend filter ready: trend={trend_check}")


# ---------------------------------------------------------------------------
# Background historical data refresh (unchanged)
# ---------------------------------------------------------------------------

async def refresh_historical_data_background(state: BotState) -> None:
    import fetch_historical as market_fetcher

    async def do_refresh(reason: str) -> None:
        try:
            logger.info(f"Background refresh ({reason}) ...")
            tmp_path = market_fetcher.OUTPUT_PATH + ".tmp"
            original = market_fetcher.OUTPUT_PATH
            market_fetcher.OUTPUT_PATH = tmp_path
            try:
                await market_fetcher.main_async()
            finally:
                market_fetcher.OUTPUT_PATH = original
            if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                os.replace(tmp_path, original)
                logger.info("Background refresh done.")
            else:
                logger.warning("Background refresh produced no output.")
        except Exception as exc:
            logger.warning(f"Background refresh failed: {exc!r}")

    try:
        existing_path = market_fetcher.OUTPUT_PATH
        if os.path.exists(existing_path):
            existing_df = pd.read_csv(
                existing_path, usecols=["open_time"], parse_dates=["open_time"]
            )
            if len(existing_df) > 0:
                last_time = existing_df["open_time"].iloc[-1]
                if last_time.tzinfo is None:
                    last_time = last_time.tz_localize("UTC")
                age_hours = (datetime.now(timezone.utc) - last_time).total_seconds() / 3600
                if age_hours > HISTORICAL_DATA_REFRESH_INTERVAL_HOURS:
                    await do_refresh("startup catch-up")
    except Exception as exc:
        logger.warning(f"Startup freshness check failed: {exc!r}")

    while True:
        await asyncio.sleep(HISTORICAL_DATA_REFRESH_INTERVAL_HOURS * 3600)
        await do_refresh("scheduled 24h cycle")


# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    logger.info(f"Starting MT5 live trading bot for '{SYMBOL}' ...")
    logger.info(
        f"Strategy: EMA(14)-1H trend + S/R (lookback={SWING_LOOKBACK}, "
        f"tolerance={RETEST_TOLERANCE_PCT}%) + 10-pattern entry"
    )
    logger.info(
        f"Exit: fixed stop ({FIXED_STAGE1_STOP_PCT}%) -> breakeven after "
        f"{NUM_FAVORABLE_CANDLES_REQUIRED} candles -> "
        f"Stage3 trail (window={STAGE3_TRAILING_WINDOW})"
    )
    logger.info(f"Risk per trade: {RISK_PER_TRADE_PCT}% of balance")

    # Initialize MT5
    if not mt5_initialize():
        logger.error("Failed to initialize MT5. Is MT5 running?")
        sys.exit(1)

    state = BotState()
    state.current_balance = get_balance()
    logger.info(f"Balance: ${state.current_balance:.2f}")

    preload_historical_data(state)

    background_refresh_task = asyncio.create_task(
        refresh_historical_data_background(state)
    )
    background_refresh_task.set_name("historical_data_refresh")

    await recover_open_position_if_any(state)

    await send_telegram_notification(
        f"🟢 <b>BOT STARTED</b>\n"
        f"Symbol: {SYMBOL}\n"
        f"Balance: ${state.current_balance:.2f}\n"
        f"Trend: {state.last_known_trend or 'calculating...'}\n"
        f"Levels tracked: {len(state.swing_levels)}"
    )

    aggregator = CandleAggregator()

    def on_candle_close(candle: Candle) -> None:
        if candle.timeframe == "5min":
            state.candles_5min.append(candle)
            state.candles_5min = state.candles_5min[-2000:]
            append_live_candle_to_csv(candle)

    aggregator.on_candle_close = on_candle_close

    last_seen_open_time  = None
    last_h1_build_count  = 0
    last_staleness_warn  = None
    last_1h_refresh      = datetime.now(timezone.utc)  # track when we last refreshed 1H from MT5

    logger.info("Polling live prices ... (Ctrl+C to stop)\n")

    try:
        while True:
            try:
                # Check MT5 is still connected
                if not mt5.terminal_info():
                    logger.error("MT5 terminal connection lost. Reconnecting ...")
                    await send_telegram_notification("🔴 <b>MT5 CONNECTION LOST</b>\nAttempting to reconnect ...")
                    mt5.shutdown()
                    await asyncio.sleep(5)
                    if not mt5_initialize():
                        logger.error("Reconnection failed. Retrying in 30s ...")
                        await asyncio.sleep(30)
                        continue
                    await send_telegram_notification("🟢 <b>MT5 RECONNECTED</b>")

                tick = get_current_tick()
                if tick is None:
                    await asyncio.sleep(1)
                    continue

                # Staleness check
                now      = datetime.now(timezone.utc)
                staleness = (now - tick["time"]).total_seconds()
                if staleness > MAX_PRICE_STALENESS_SECONDS:
                    if last_staleness_warn is None or (now - last_staleness_warn).total_seconds() > 30:
                        logger.warning(f"STALE TICK: {staleness:.0f}s old -- skipping.")
                        last_staleness_warn = now
                    await asyncio.sleep(0.5)
                    continue

                mid_price = (tick["bid"] + tick["ask"]) / 2
                aggregator.add_tick(now.timestamp(), mid_price)

                # Use aggregator's current live candle open_time for heartbeat
                live_candle = aggregator.get_current("5min")
                current_open_time = live_candle.open_time if live_candle else None

                if (
                    current_open_time is not None
                    and current_open_time != last_seen_open_time
                ):
                    last_seen_open_time = current_open_time
                    total_candles = len(state.candles_5min)

                    # Refresh 1H candles from MT5 every 60 minutes
                    # This is far more accurate than building from 5min data
                    now_utc = datetime.now(timezone.utc)
                    if (now_utc - last_1h_refresh).total_seconds() >= 3600:
                        refresh_1h_candles_from_mt5(state)
                        last_1h_refresh = now_utc

                    update_swing_levels(state)
                    trend_status, _ = compute_current_trend(state)

                    if (
                        trend_status is not None
                        and trend_status != state.last_known_trend
                    ):
                        if state.last_known_trend is not None:
                            emoji = {"UP": "📈", "DOWN": "📉", "FLAT": "➖"}.get(trend_status, "🔔")
                            await send_telegram_notification(
                                f"{emoji} <b>TREND CHANGED</b>\n"
                                f"{state.last_known_trend} → {trend_status}"
                            )
                        state.last_known_trend = trend_status

                    state.current_balance = get_balance()

                    logger.info(
                        f"Heartbeat: 5min={len(state.candles_5min)} "
                        f"1h={len(state.candles_1h)} "
                        f"levels={len(state.swing_levels)} "
                        f"trend={trend_status} "
                        f"position={'YES' if state.open_position else 'no'} "
                        f"balance=${state.current_balance:.2f} "
                        f"rejections={state.trend_filter_rejections}"
                    )

                    write_status_file(state)

                    if state.open_position is not None:
                        await manage_open_position(state)
                    else:
                        await check_for_entry(state)

                await asyncio.sleep(PRICE_POLL_INTERVAL_SECONDS)

            except Exception as exc:
                logger.exception(f"Error in main loop: {exc!r}. Continuing in 5s ...")
                await asyncio.sleep(5)

    finally:
        logger.info("Shutting down ...")
        background_refresh_task.cancel()
        try:
            await background_refresh_task
        except asyncio.CancelledError:
            pass
        mt5.shutdown()
        logger.info("MT5 shutdown. Goodbye.")


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as exc:
        logger.exception(f"Unexpected error: {exc!r}")


if __name__ == "__main__":
    main()
