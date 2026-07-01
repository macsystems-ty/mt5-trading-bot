"""
live_mt5_trading_bot.py

Automated trading bot for your real Deriv MT5 demo account, connected
via MetaApi. Implements our validated strategy:
  - EMA(1H) trend filter
  - Support/Resistance + Engulfing candle entry (tolerance=0.01%)
  - Candle-based trailing stop exit (window=3)
  - Trend-reversal safety check while a position is open
  - Position sizing: risk a fixed % of balance per trade, sized in
    MT5 lots (not Deriv's stake+multiplier system)

Trades placed by this bot will appear in your real MT5 account --
including on your phone's MT5 app.

Run with:
    python src/live/live_mt5_trading_bot.py
"""

import asyncio
import logging
import os
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi
import aiohttp
import pandas as pd

def _find_repo_root(start_path: str) -> str:
    """
    Walks upward from start_path until it finds a directory containing
    both 'src/strategy' and 'src/data' -- this lets the SAME file work
    correctly regardless of whether it's placed at src/live/ (the
    original location) or live_bots/<SYMBOL>/ (per-market copies for
    running multiple markets concurrently), without hardcoding a fixed
    relative path depth that would break depending on placement.
    """
    current = os.path.abspath(start_path)
    for _ in range(10):  # safety limit on how far up we'll search
        candidate_strategy = os.path.join(current, "src", "strategy")
        candidate_data = os.path.join(current, "src", "data")
        if os.path.isdir(candidate_strategy) and os.path.isdir(candidate_data):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise RuntimeError(
        f"Could not find repo root (a directory containing both src/strategy "
        f"and src/data) by walking up from {start_path}. Check your folder structure."
    )


_REPO_ROOT = _find_repo_root(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(_REPO_ROOT, "src", "data")
STRATEGY_PATH = os.path.join(_REPO_ROOT, "src", "strategy")
MARKET_DIR = os.path.join(_REPO_ROOT, "markets", "1HZ100V")  # EDIT THIS per-market copy: change "1HZ25V" to match this instance's market
HISTORICAL_DATA_DIR = os.path.join(MARKET_DIR, "data")  # using the FRESHER data we fetched directly from Deriv, since the old backtest CSV was several days stale and was causing the live trend filter to use an outdated EMA
sys.path.insert(0, DATA_PATH)
sys.path.insert(0, STRATEGY_PATH)
sys.path.insert(0, MARKET_DIR)

HISTORICAL_DATA_REFRESH_INTERVAL_HOURS = 24  # how often to auto-refresh historical data in the background, for long-running unattended deployments

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

METAAPI_TOKEN = os.getenv("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = "Volatility 100 (1s) Index"  # MT5 symbol name, used for live trading
HISTORICAL_DATA_SYMBOL_PREFIX = "1HZ100V"  # Deriv Options API symbol code, used for historical CSV filenames
STATUS_DIR = os.path.join(os.path.expanduser("~"), "mt5-trading-bot", "bot_status")  # shared by ALL bots (any market, including future XAUUSD/BTCUSD additions) for the Telegram interactive-button feature

TRAILING_WINDOW = 10  # NO LONGER USED for the initial stop (replaced by FIXED_STAGE1_STOP_PCT below) -- kept only as a historical reference constant; not referenced anywhere else
FIXED_STAGE1_STOP_PCT = 0.476  # EDIT PER MARKET: validated via backtesting to consistently outperform the old 10-candle-range initial stop, across multiple recent windows (not just full-history average). 1HZ25V=0.057, 1HZ75V=0.344, 1HZ90V=0.417, 1HZ100V=0.476, R_100=0.463
MAX_TREND_STRENGTH_PCT = 1.0529  # EDIT PER MARKET: skip entries where price is further than this from the EMA (the "overextended" zone) -- validated via backtesting, this is each market's real tercile boundary where win rate consistently dropped. 1HZ25V=0.2838, 1HZ75V=0.7879, 1HZ90V=0.9165, 1HZ100V=1.0529, R_100=1.0407
NUM_FAVORABLE_CANDLES_REQUIRED = 2  # candles required before stop moves to breakeven
STAGE3_TRAILING_WINDOW = 2  # tighter trail used AFTER breakeven activates
RETEST_TOLERANCE_PCT = 0.05  # adopted after testing: combined with SWING_LOOKBACK=3 gives +60.974% vs baseline's +42.593%
SWING_LOOKBACK = 3
LEVEL_AGE_CAP = 200  # adopted after testing: best performance on recent trade windows (last 100/50 trades)
MAX_PRICE_STALENESS_SECONDS = 30  # reject price quotes older than this -- guards against stale cached data after a sync disruption

RISK_PER_TRADE_PCT = 1.0
MIN_VOLUME = 0.005
MAX_VOLUME = 2.0
VOLUME_STEP = 0.001

PRICE_POLL_INTERVAL_SECONDS = 1.0

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(
    LOG_DIR, f"mt5_trading_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
)

# Live 5min candle data, saved in the SAME format as our 1-year historical
# CSVs (open_time,open,high,low,close) -- NOT timestamped per-run, so it
# accumulates continuously across restarts, building real live history
# over time.
LIVE_CANDLES_CSV_PATH = os.path.join(LOG_DIR, f"{SYMBOL.replace(' ', '_')}_5min_live.csv")

logging.basicConfig(
    level=logging.WARNING,  # root logger: quiet by default, catches any SDK logger by any name
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger("mt5_live_bot")
logger.setLevel(logging.INFO)  # our own bot's logs stay visible regardless of the root level above

# Belt-and-suspenders: also explicitly silence any SDK logger names we
# can guess, in case something re-configures its own logger level
# independently of the root logger.
for noisy_logger_name in [
    "MetaApi", "metaapi", "metaapi_cloud_sdk", "SynchronizationListener",
    "websockets", "socketio", "engineio",
]:
    logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)


@dataclass
class OpenPosition:
    position_id: str
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


def round_to_volume_step(volume: float, min_volume: float = None, max_volume: float = None, volume_step: float = None) -> float:
    min_volume = min_volume if min_volume is not None else MIN_VOLUME
    max_volume = max_volume if max_volume is not None else MAX_VOLUME
    volume_step = volume_step if volume_step is not None else VOLUME_STEP

    steps = int(volume / volume_step)
    rounded = steps * volume_step
    rounded = max(min_volume, min(max_volume, rounded))
    return round(rounded, 3)


def calculate_volume(
    balance: float, current_price: float, stop_distance_pct: float,
    min_volume: float = None, max_volume: float = None, volume_step: float = None,
) -> float:
    risk_dollars = balance * (RISK_PER_TRADE_PCT / 100)
    stop_distance_fraction = max(stop_distance_pct, 0.001) / 100

    raw_volume = risk_dollars / (current_price * stop_distance_fraction)
    return round_to_volume_step(raw_volume, min_volume, max_volume, volume_step)


def candle_color(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "GREEN"
    if close_price < open_price:
        return "RED"
    return "FLAT"


def is_bullish_engulfing(prev_open, prev_close, cur_open, cur_close) -> bool:
    prev_is_red = prev_close < prev_open
    cur_is_green = cur_close > cur_open
    if not (prev_is_red and cur_is_green):
        return False
    return cur_open <= prev_close and cur_close >= prev_open


def is_bearish_engulfing(prev_open, prev_close, cur_open, cur_close) -> bool:
    prev_is_green = prev_close > prev_open
    cur_is_red = cur_close < cur_open
    if not (prev_is_green and cur_is_red):
        return False
    return cur_open >= prev_close and cur_close <= prev_open


def _to_pattern_candle(c: Candle) -> PatternCandle:
    return PatternCandle(open=c.open, high=c.high, low=c.low, close=c.close)


def matches_any_buy_pattern(candles_5min: list) -> bool:
    """
    Checks the COMBINED 10-pattern set for a BUY signal, in the same
    priority order as our validated backtester
    (backtester_sr_patterns_combined.py BUY_PATTERNS list):
    bullish_engulfing, piercing_line, morning_star, three_white_soldiers.
    """
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
    """
    Checks the COMBINED 10-pattern set for a SELL signal, in the same
    priority order as our validated backtester
    (backtester_sr_patterns_combined.py SELL_PATTERNS list):
    bearish_engulfing, three_black_crows, falling_three_methods,
    shooting_star, evening_star, dark_cloud_cover.
    """
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

    window = candles[i - SWING_LOOKBACK : i + SWING_LOOKBACK + 1]
    window_highs = [c.high for c in window]
    window_lows = [c.low for c in window]
    candle = candles[i]

    if candle.high == max(window_highs):
        state.swing_levels.append(SwingLevel(index=i, price=candle.high, level_type="resistance"))
        if not quiet:
            logger.info(f"New resistance level identified: {candle.high:.2f}")

    if candle.low == min(window_lows):
        state.swing_levels.append(SwingLevel(index=i, price=candle.low, level_type="support"))
        if not quiet:
            logger.info(f"New support level identified: {candle.low:.2f}")

    state.swing_levels = state.swing_levels[-500:]


def compute_current_trend(state: BotState) -> "tuple[Optional[str], Optional[float]]":
    """
    Returns (trend_label, ema_value). ema_value is also returned so
    callers (like check_for_entry's trend-strength filter) can reuse
    the SAME EMA calculation rather than recomputing it separately,
    which would risk subtle inconsistency between the two.
    """
    if len(state.candles_1h) < 20:
        return None, None

    import pandas as pd

    df = pd.DataFrame(
        {
            "open": [c.open for c in state.candles_1h],
            "high": [c.high for c in state.candles_1h],
            "low": [c.low for c in state.candles_1h],
            "close": [c.close for c in state.candles_1h],
        }
    )
    result = indicators.add_all_indicators(df)
    ema = result["ema_14"]

    last_ema, prev_ema = ema.iloc[-1], ema.iloc[-2]

    if pd.isna(last_ema) or pd.isna(prev_ema):
        return None, None

    # IMPORTANT: compare the EMA against the CURRENT live price (the most
    # recent 5-minute candle's close), not the close of the last COMPLETED
    # 1H candle. The 1H close can be up to ~55 minutes stale relative to
    # the live market -- using it here was causing the trend label to lag
    # behind real, visible price action by up to an hour (confirmed: a
    # real trade opened BUY while the live chart showed price clearly
    # below a falling EMA14 on the 1H timeframe).
    current_price = state.candles_5min[-1].close if state.candles_5min else None
    if current_price is None:
        return None, None

    ema_rising = last_ema > prev_ema
    price_above = current_price > last_ema

    if price_above and ema_rising:
        return "UP", last_ema
    if not price_above and not ema_rising:
        return "DOWN", last_ema
    return "FLAT", last_ema


async def open_trade(connection, state: BotState, direction: str, level_price: float, stop_distance_pct: float) -> None:
    current_price = state.candles_5min[-1].close

    # Use the broker's REAL symbol specification (min/max/step volume)
    # instead of our hardcoded generic defaults. This directly fixes a
    # real bug found in production: Volatility 90 (1s) Index rejected
    # orders with "Invalid volume in the request" for volumes our
    # generic defaults considered valid (e.g. 1.967, 0.934) -- this
    # symbol's REAL broker-defined limits apparently differ from our
    # hardcoded MAX_VOLUME=2.0 / VOLUME_STEP=0.001.
    min_volume, max_volume, volume_step = None, None, None
    try:
        spec = connection.terminal_state.specification(symbol=SYMBOL)
        if spec:
            min_volume = spec.get("minVolume")
            max_volume = spec.get("maxVolume")
            volume_step = spec.get("volumeStep")
            logger.info(
                f"Using broker symbol specification: minVolume={min_volume}, "
                f"maxVolume={max_volume}, volumeStep={volume_step}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"Could not fetch symbol specification, falling back to generic "
            f"defaults (min={MIN_VOLUME}, max={MAX_VOLUME}, step={VOLUME_STEP}): {exc!r}"
        )

    volume = calculate_volume(
        state.current_balance, current_price, stop_distance_pct,
        min_volume, max_volume, volume_step,
    )

    # Compute the REAL stop price BEFORE placing the order, so we can
    # pass it as a genuine broker-side stop_loss -- not just track it
    # in our own memory. CRITICAL FIX: previously, NO trade had a
    # real stop-loss on the broker's side at all, meaning a
    # disconnected/delayed/restarted bot left positions completely
    # unprotected by anything except our own software being alive.
    # Confirmed via a real incident: a recovered position with no
    # broker-side stop lost $326.11 instead of the expected $124.48,
    # because the actual stop level was never communicated to the
    # broker -- only our internal monitoring loop knew about it.
    current_price = state.candles_5min[-1].close if state.candles_5min else None
    if current_price is None:
        logger.error("Cannot determine current price -- aborting trade open.")
        return

    if direction == "BUY":
        stop_loss_price = current_price * (1 - FIXED_STAGE1_STOP_PCT / 100)
    else:
        stop_loss_price = current_price * (1 + FIXED_STAGE1_STOP_PCT / 100)

    logger.info(
        f"Attempting to open {direction} trade: volume={volume} lots, "
        f"risk target={RISK_PER_TRADE_PCT}% of ${state.current_balance:.2f}, "
        f"stop_distance={stop_distance_pct:.4f}%, real broker stop_loss={stop_loss_price:.5f}"
    )

    try:
        if direction == "BUY":
            result = await connection.create_market_buy_order(
                symbol=SYMBOL, volume=volume, stop_loss=stop_loss_price,
                options={"comment": "sr_engulfing_trend"},
            )
        else:
            result = await connection.create_market_sell_order(
                symbol=SYMBOL, volume=volume, stop_loss=stop_loss_price,
                options={"comment": "sr_engulfing_trend"},
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Order failed: {exc!r}")
        return

    position_id = result.get("positionId") or result.get("orderId")
    if not position_id:
        logger.error(f"No position ID returned in order result: {result}")
        return

    entry_price = state.candles_5min[-1].close

    state.open_position = OpenPosition(
        position_id=str(position_id),
        direction=direction,
        entry_price=entry_price,
        volume=volume,
        trailing_stop=(
            entry_price * (1 - FIXED_STAGE1_STOP_PCT / 100)
            if direction == "BUY"
            else entry_price * (1 + FIXED_STAGE1_STOP_PCT / 100)
        ),
        level_price=level_price,
        entry_time=state.candles_5min[-1].open_time,
    )

    logger.info(
        f"✅ TRADE OPENED: {direction} position_id={position_id} volume={volume} "
        f"entry≈{entry_price:.2f}"
    )

    await send_telegram_notification(
        f"🟢 <b>TRADE OPENED</b>\n"
        f"Direction: {direction}\n"
        f"Volume: {volume} lots\n"
        f"Entry: ≈{entry_price:.2f}\n"
        f"Balance: ${state.current_balance:.2f}"
    )


async def close_trade(connection, state: BotState, reason: str) -> None:
    position = state.open_position
    if position is None:
        return

    try:
        await connection.close_position(position_id=position.position_id)
    except Exception as exc:  # noqa: BLE001
        # CRITICAL: do NOT assume the close succeeded just because the
        # close REQUEST failed. Check the broker's actual position list
        # before deciding whether to stop tracking this position --
        # blindly clearing local state here previously caused a real
        # position to go completely unmonitored after a transient
        # "Position not found" error, resulting in an avoidable loss.
        still_open = await position_still_open_on_broker(connection, position.position_id)

        if still_open:
            logger.error(
                f"Close failed for position {position.position_id}: {exc!r} -- "
                f"position CONFIRMED STILL OPEN on broker. Keeping it tracked "
                f"and will retry closing on the next cycle."
            )
            await send_telegram_notification(
                f"⚠️ <b>WARNING: Close failed, position still open</b>\n"
                f"Position: {position.position_id}\n"
                f"Error: {exc!r}\n"
                f"Bot will retry closing automatically. Consider checking MT5 directly."
            )
            return  # keep state.open_position as-is; manage_open_position will retry

        logger.warning(
            f"Close failed for position {position.position_id}: {exc!r} -- "
            f"but position is NOT in the broker's open positions list (likely "
            f"already closed externally, e.g. stop-out or manual close). "
            f"Clearing local state."
        )
        state.open_position = None
        state.trades_completed += 1
        return

    state.trades_completed += 1

    # Compute the REAL final profit/loss for this trade, using the
    # actual exit level our exit logic decided on (trailing_stop) --
    # this is a one-time calculation at close, NOT a running/live
    # update during the trade.
    exit_price = position.trailing_stop
    if position.direction == "BUY":
        pct_change = (exit_price - position.entry_price) / position.entry_price * 100
    else:
        pct_change = (position.entry_price - exit_price) / position.entry_price * 100

    dollar_pnl = position.volume * abs(exit_price - position.entry_price) * (
        1 if pct_change >= 0 else -1
    )
    # More precise dollar P&L: use the same contract-value logic as
    # position sizing, since volume is already in lots for this symbol.
    dollar_pnl = (position.volume * position.entry_price) * (pct_change / 100)

    outcome_emoji = "✅" if pct_change > 0 else ("➖" if abs(pct_change) < 0.001 else "❌")

    # Compute the REAL peak profit reached during this trade's
    # lifetime, using the candles already in memory -- this is what
    # powers the "Show Peak" / "Recent Trades" Telegram buttons,
    # answering exactly how high a trade got before the trailing
    # stop caught it on the way back down.
    peak_price = position.entry_price
    for c in state.candles_5min:
        if c.open_time is None or position.entry_time is None or c.open_time < position.entry_time:
            continue
        if position.direction == "BUY":
            peak_price = max(peak_price, c.high)
        else:
            peak_price = min(peak_price, c.low) if peak_price != position.entry_price else c.low

    if position.direction == "BUY":
        peak_pct = (peak_price - position.entry_price) / position.entry_price * 100
    else:
        peak_pct = (position.entry_price - peak_price) / position.entry_price * 100
    peak_pct = max(0.0, peak_pct)
    peak_dollar = (position.volume * position.entry_price) * (peak_pct / 100)

    state.recent_trades_log.append({
        "position_id": position.position_id,
        "direction": position.direction,
        "entry_price": position.entry_price,
        "exit_price": exit_price,
        "pct_change": pct_change,
        "dollar_pnl": dollar_pnl,
        "peak_pct": peak_pct,
        "peak_dollar": peak_dollar,
        "reason": reason,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    })
    state.recent_trades_log = state.recent_trades_log[-20:]  # keep a bounded, recent window only

    logger.info(
        f"🔚 TRADE CLOSED ({reason}): position_id={position.position_id} "
        f"P&L: {pct_change:+.4f}% (≈${dollar_pnl:+.2f}) "
        f"(total trades completed: {state.trades_completed})"
    )

    await send_telegram_notification(
        f"{outcome_emoji} <b>TRADE CLOSED</b>\n"
        f"Direction: {position.direction}\n"
        f"Reason: {reason}\n"
        f"Entry: ≈{position.entry_price:.2f} -> Exit: ≈{exit_price:.2f}\n"
        f"Result: {pct_change:+.4f}% (≈${dollar_pnl:+.2f})\n"
        f"Balance: ${state.current_balance:.2f}\n"
        f"Total trades completed: {state.trades_completed}"
    )

    state.open_position = None


async def recover_open_position_if_any(connection, state: BotState) -> None:
    """
    CRITICAL SAFETY MECHANISM: checks the broker's REAL open positions
    for our symbol at startup, and re-adopts any that exist into
    state.open_position. This directly fixes a real, confirmed
    incident: when the watchdog forces a restart (or any other
    restart happens) while a real trade is open, the fresh process's
    in-memory state starts EMPTY -- with no knowledge of that trade.
    Without this check, the bot would believe open_position=None and
    never manage that trade again (no breakeven, no Stage3 trailing,
    no stop monitoring), even though it's still genuinely open and
    accruing real profit or loss on the broker's side.

    HONEST LIMITATION: we can recover the real entry price, volume,
    direction, and the broker's CURRENT stop-loss (if one is set) --
    but we have NO way to know how many favorable candles had already
    occurred before the restart, or whether breakeven/Stage3 trailing
    had already activated. We deliberately treat a recovered position
    as if newly opened for breakeven-tracking purposes (starting its
    favorable-candle count at 0) -- this is a safe, conservative
    choice: worst case, breakeven takes a couple of extra candles to
    re-trigger from scratch; we NEVER assume MORE protection exists
    than we can actually verify.
    """
    try:
        positions = connection.terminal_state.positions
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Could not check for existing open positions during recovery: {exc!r}")
        return

    matching = [p for p in positions if p.get("symbol") == SYMBOL]

    if not matching:
        logger.info("Position recovery check: no existing open position for this symbol. Starting clean.")
        return

    if len(matching) > 1:
        logger.error(
            f"Position recovery check found {len(matching)} open positions for {SYMBOL} -- "
            f"expected at most 1. Recovering only the first one; please verify the others manually."
        )

    p = matching[0]
    direction = "BUY" if p.get("type") == "POSITION_TYPE_BUY" else "SELL"
    entry_price = p.get("openPrice")
    volume = p.get("volume")
    broker_stop_loss = p.get("stopLoss")

    # If the broker has no stop-loss recorded (e.g. it was a market
    # order with no SL field set -- exactly the gap that caused a
    # real incident: a recovered position lost $326.11 instead of
    # the expected $124.48, because only OUR software tracked the
    # stop, with no broker-side backup), set a REAL broker-side stop
    # immediately, rather than just estimating one in our own memory.
    if broker_stop_loss:
        recovered_stop = broker_stop_loss
        stop_source = "broker's recorded stop-loss"
    else:
        if direction == "BUY":
            recovered_stop = entry_price * (1 - FIXED_STAGE1_STOP_PCT / 100)
        else:
            recovered_stop = entry_price * (1 + FIXED_STAGE1_STOP_PCT / 100)
        stop_source = "estimated AND just set on the broker (previously had none)"
        await update_broker_stop_loss(connection, str(p.get("id")), recovered_stop)

    broker_open_time = p.get("time")
    if isinstance(broker_open_time, str):
        try:
            broker_open_time = datetime.fromisoformat(broker_open_time.replace("Z", "+00:00"))
        except ValueError:
            broker_open_time = None

    state.open_position = OpenPosition(
        position_id=str(p.get("id")),
        direction=direction,
        entry_price=entry_price,
        volume=volume,
        trailing_stop=recovered_stop,
        level_price=entry_price,  # unknown originally; not used after recovery
        entry_time=broker_open_time,  # best available: broker's own recorded open time
        favorable_candle_count=0,  # conservative: don't assume prior progress
        stage3_active=False,  # conservative: don't assume breakeven already triggered
    )

    logger.warning(
        f"⚠️ POSITION RECOVERED on startup: {direction} position_id={p.get('id')} "
        f"entry≈{entry_price} volume={volume}, stop={recovered_stop:.5f} ({stop_source}). "
        f"This position was found open on the broker but was NOT in this process's memory "
        f"(likely from a restart while it was open). Resuming management now."
    )
    await send_telegram_notification(
        f"⚠️ <b>POSITION RECOVERED ON STARTUP</b>\n"
        f"Direction: {direction}\n"
        f"Entry: ≈{entry_price}\n"
        f"Volume: {volume}\n"
        f"Stop: {recovered_stop:.5f} ({stop_source})\n"
        f"This trade was open on the broker but unmanaged before this restart. "
        f"Resuming tracking now -- please verify manually if anything looks off."
    )


def write_status_file(state: "BotState") -> None:
    """
    Writes this bot's current status to a dedicated JSON file, for a
    SEPARATE Telegram responder process to read when answering
    interactive button presses (Status, Why No Trade?, Recent
    Trades, etc.). Completely fire-and-forget: any failure here is
    caught and logged, NEVER allowed to affect live trading. Designed
    to scale to any number of markets (including future XAUUSD/BTCUSD
    additions) without any code changes in the responder, since each
    bot just writes its OWN file using its own symbol name.
    """
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        status = {
            "symbol": SYMBOL,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "balance": state.current_balance,
            "open_position": (
                {
                    "position_id": state.open_position.position_id,
                    "direction": state.open_position.direction,
                    "entry_price": state.open_position.entry_price,
                    "volume": state.open_position.volume,
                    "trailing_stop": state.open_position.trailing_stop,
                    "stage3_active": state.open_position.stage3_active,
                }
                if state.open_position is not None
                else None
            ),
            "trades_completed": state.trades_completed,
            "trend_filter_rejections": state.trend_filter_rejections,
            "current_trend": state.last_known_trend,
            "recent_trades": state.recent_trades_log[-10:],
            "fixed_stage1_stop_pct": FIXED_STAGE1_STOP_PCT,
            "max_trend_strength_pct": MAX_TREND_STRENGTH_PCT,
            "risk_per_trade_pct": RISK_PER_TRADE_PCT,
        }

        tmp_path = os.path.join(STATUS_DIR, f"{HISTORICAL_DATA_SYMBOL_PREFIX}.json.tmp")
        final_path = os.path.join(STATUS_DIR, f"{HISTORICAL_DATA_SYMBOL_PREFIX}.json")
        with open(tmp_path, "w") as f:
            json.dump(status, f, indent=2, default=str)
        os.replace(tmp_path, final_path)  # atomic, same safety pattern as the historical data refresh
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not write status file (non-critical, trading continues): {exc!r}")


async def position_still_open_on_broker(connection, position_id: str) -> bool:
    """
    Checks the broker's actual current open positions (via
    terminal_state.positions, MetaApi's standard live position list) to
    see whether the given position_id genuinely still exists AND
    belongs to OUR symbol. Used as a safety check before ever clearing
    local position tracking after a failed close request -- we must
    never assume a position is gone just because one API call failed.

    The symbol check matters specifically because multiple instances
    of this bot may run concurrently against the SAME MetaApi account
    (one process per market) -- this ensures a process never mistakes
    a position belonging to a DIFFERENT market/symbol as its own.
    """
    try:
        positions = connection.terminal_state.positions
        return any(
            str(p.get("id")) == str(position_id) and p.get("symbol") == SYMBOL
            for p in positions
        )
    except Exception as exc:  # noqa: BLE001
        # If we can't even check, err on the side of caution: assume it
        # MIGHT still be open, so we keep retrying rather than abandon it.
        logger.error(
            f"Could not verify position {position_id} status with broker: {exc!r}. "
            f"Assuming it may still be open, will keep tracking and retry."
        )
        return True


async def update_broker_stop_loss(connection, position_id: str, new_stop_loss: float) -> None:
    """
    Sends a real POSITION_MODIFY request to update this position's
    broker-side stop-loss. This is what makes our trailing stop
    GENUINELY effective even if our bot disconnects right after this
    call -- the broker will execute the stop on its own, with no
    dependency on our software remaining connected. Fire-and-forget:
    a failure here is logged but never allowed to crash the trading
    loop, since our own internal monitoring still provides a backup
    (just without the broker-side safety net for that specific
    interval until the next successful update).
    """
    try:
        await connection.modify_position(position_id=position_id, stop_loss=new_stop_loss)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"Could not update broker-side stop-loss for position {position_id} "
            f"(internal tracking continues regardless): {exc!r}"
        )


async def manage_open_position(connection, state: BotState) -> None:
    position = state.open_position
    if position is None:
        return

    current_trend, _ = compute_current_trend(state)
    position_expects_trend = "UP" if position.direction == "BUY" else "DOWN"

    if current_trend is not None and current_trend != position_expects_trend and current_trend != "FLAT":
        logger.warning(
            f"Trend reversal detected while position open: "
            f"position={position.direction} expected trend={position_expects_trend}, "
            f"current trend={current_trend}. Closing position early."
        )
        await close_trade(connection, state, reason=f"trend reversed to {current_trend}")
        return

    current_candle = state.candles_5min[-1]
    if len(state.candles_5min) < 2:
        return

    if position.stage3_active:
        # STAGE 3: tight trail using the STAGE3_TRAILING_WINDOW candles
        # strictly BEFORE the current one (matching the backtester's
        # lows[i-window:i] slicing exactly -- NOT candles_5min[-window:][:-1],
        # which only yields window-1 prior candles due to the current
        # candle already being included in that slice).
        prior_candles = state.candles_5min[-(STAGE3_TRAILING_WINDOW + 1):-1]
        if position.direction == "BUY":
            new_stop = min(c.low for c in prior_candles) if prior_candles else position.trailing_stop
            if new_stop > position.trailing_stop:
                position.trailing_stop = new_stop
                await update_broker_stop_loss(connection, position.position_id, position.trailing_stop)
            if current_candle.low <= position.trailing_stop:
                await close_trade(connection, state, reason="stage3 trailing stop hit")
        else:
            new_stop = max(c.high for c in prior_candles) if prior_candles else position.trailing_stop
            if new_stop < position.trailing_stop:
                position.trailing_stop = new_stop
                await update_broker_stop_loss(connection, position.position_id, position.trailing_stop)
            if current_candle.high >= position.trailing_stop:
                await close_trade(connection, state, reason="stage3 trailing stop hit")
        return

    # STAGE 1 / STAGE 2: check the current stop (initial or breakeven), then
    # check whether this candle is favorable enough to progress toward
    # breakeven / Stage 3 activation.
    if position.direction == "BUY":
        hit_stop = current_candle.low <= position.trailing_stop
    else:
        hit_stop = current_candle.high >= position.trailing_stop

    if hit_stop:
        await close_trade(connection, state, reason="initial or breakeven stop hit")
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
            await update_broker_stop_loss(connection, position.position_id, position.trailing_stop)
            logger.info(
                f"Position {position.position_id}: breakeven reached after "
                f"{position.favorable_candle_count} favorable candles -- "
                f"stop moved to entry price {position.entry_price:.2f}, Stage 3 trailing active."
            )
    else:
        position.favorable_candle_count = 0


async def check_for_entry(connection, state: BotState) -> None:
    if state.open_position is not None:
        return

    if len(state.candles_5min) < SWING_LOOKBACK + 2:
        return

    trend, ema_value = compute_current_trend(state)
    if trend not in ("UP", "DOWN"):
        return

    prev_candle = state.candles_5min[-2]
    cur_candle = state.candles_5min[-1]

    # TREND STRENGTH FILTER: skip entries where price is too far from
    # the EMA (the "overextended" zone). Validated via backtesting:
    # across all 5 markets, entries in this zone showed a large, very
    # consistent win-rate drop (e.g. 1HZ25V: 57% near the EMA vs only
    # 36% far from it) -- likely because an overextended move is more
    # likely already near its end than a fresh trend just developing.
    if ema_value is not None and ema_value != 0:
        trend_strength_pct = abs(cur_candle.close - ema_value) / ema_value * 100
        if trend_strength_pct > MAX_TREND_STRENGTH_PCT:
            state.trend_filter_rejections += 1
            # Rate-limited logging: a summary every 10 rejections, not
            # every single one, to avoid spamming the log on every
            # candle close while price sits in an overextended zone.
            if state.trend_filter_rejections % 10 == 1:
                logger.info(
                    f"Trend-strength filter rejection #{state.trend_filter_rejections}: "
                    f"strength={trend_strength_pct:.4f}% > max={MAX_TREND_STRENGTH_PCT}% "
                    f"(price={cur_candle.close}, ema={ema_value:.5f})"
                )
            return

    current_index = len(state.candles_5min) - 1

    for level in reversed(state.swing_levels[-50:]):
        level_age = current_index - level.index
        if level_age > LEVEL_AGE_CAP:
            continue

        tolerance = level.price * (RETEST_TOLERANCE_PCT / 100)

        if level.level_type == "support":
            retest_price = prev_candle.low
        else:
            retest_price = prev_candle.high

        if not (level.price - tolerance <= retest_price <= level.price + tolerance):
            continue

        if level.level_type == "support" and trend == "UP":
            if matches_any_buy_pattern(state.candles_5min):
                # Stop distance is now a FIXED PERCENTAGE (validated via
                # backtesting to outperform the old 10-candle-range
                # mechanism) -- MUST match exactly what open_trade uses
                # for the actual trailing_stop, or position sizing and
                # the real stop would disagree.
                await open_trade(connection, state, "BUY", level.price, FIXED_STAGE1_STOP_PCT)
                return

        if level.level_type == "resistance" and trend == "DOWN":
            if matches_any_sell_pattern(state.candles_5min):
                await open_trade(connection, state, "SELL", level.price, FIXED_STAGE1_STOP_PCT)
                return


async def refresh_historical_data_background(state: "BotState") -> None:
    """
    Periodically re-fetches historical 5min candle data in the
    background and atomically replaces the on-disk CSV, so a bot
    running unattended for months (e.g. on a server, with no restarts)
    doesn't silently drift into using stale data the way we found
    happened with a multi-day-old file.

    SAFETY GUARANTEES:
      - Runs in its own task, fully isolated from the trading loop.
        ANY failure here (network down, API error, disk full, etc.)
        is caught and logged -- NEVER propagated. A failed refresh
        simply means we keep using the existing data and try again
        next cycle.
      - Writes to a temporary file first, then uses os.replace() (an
        atomic rename on POSIX systems) to swap it in -- the real CSV
        is never left in a partially-written state, even if the
        process is killed mid-write.
      - Does NOT affect the currently-running bot's in-memory state
        at all (state.candles_1h etc. are untouched) -- this only
        updates the file that the NEXT startup's preload will read.
    """
    import fetch_historical as market_fetcher

    async def do_refresh(reason: str) -> None:
        try:
            logger.info(f"Background task: starting historical data refresh ({reason}) for {HISTORICAL_DATA_SYMBOL_PREFIX} ...")

            tmp_path = market_fetcher.OUTPUT_PATH + ".tmp"
            original_output_path = market_fetcher.OUTPUT_PATH
            market_fetcher.OUTPUT_PATH = tmp_path

            try:
                await market_fetcher.main_async()
            finally:
                market_fetcher.OUTPUT_PATH = original_output_path

            if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                os.replace(tmp_path, original_output_path)
                logger.info(f"Background task: historical data refreshed successfully -> {original_output_path}")
            else:
                logger.warning(
                    "Background task: refresh produced no/empty output -- "
                    "keeping existing historical data file untouched."
                )
        except Exception as exc:  # noqa: BLE001
            # CRITICAL: never let a background refresh failure affect
            # live trading. Just log it and try again next cycle.
            logger.warning(f"Background historical data refresh failed: {exc!r}")

    # STARTUP CHECK: if the EXISTING data is already older than our
    # refresh interval, refresh immediately rather than waiting a
    # full additional cycle. Without this, a bot that gets restarted
    # often (server resizes, code updates, watchdog-triggered
    # restarts) NEVER survives long enough for the scheduled refresh
    # below to fire even once -- confirmed in production: data was
    # found 5+ days stale despite this mechanism existing, because
    # every restart reset the 24h countdown back to zero.
    try:
        existing_path = market_fetcher.OUTPUT_PATH
        if os.path.exists(existing_path):
            import pandas as pd
            existing_df = pd.read_csv(existing_path, usecols=["open_time"], parse_dates=["open_time"])
            if len(existing_df) > 0:
                last_candle_time = existing_df["open_time"].iloc[-1]
                if last_candle_time.tzinfo is None:
                    last_candle_time = last_candle_time.tz_localize("UTC")
                age_hours = (datetime.now(timezone.utc) - last_candle_time).total_seconds() / 3600
                if age_hours > HISTORICAL_DATA_REFRESH_INTERVAL_HOURS:
                    logger.warning(
                        f"Background task: existing historical data is {age_hours:.1f}h old "
                        f"(threshold: {HISTORICAL_DATA_REFRESH_INTERVAL_HOURS}h) -- refreshing "
                        f"immediately on startup instead of waiting for the next scheduled cycle."
                    )
                    await do_refresh("startup catch-up, data was stale")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not check historical data freshness on startup: {exc!r}")

    while True:
        await asyncio.sleep(HISTORICAL_DATA_REFRESH_INTERVAL_HOURS * 3600)
        await do_refresh("scheduled 24h cycle")


async def send_telegram_notification(message: str) -> None:
    """
    Sends a message to Telegram via the bot API, if TELEGRAM_BOT_TOKEN
    and TELEGRAM_CHAT_ID are configured. Silently does nothing if not
    configured (so this is fully optional). Uses aiohttp for a
    non-blocking request -- a slow or failed Telegram call must NEVER
    delay or crash the trading logic itself, so all errors are caught
    and only logged, never raised.

    Automatically prepends the market SYMBOL to every message, so
    notifications are clearly identifiable when running multiple bot
    instances (one per market) sending to the same Telegram chat.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    labeled_message = f"<b>[{SYMBOL}]</b>\n{message}"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": labeled_message, "parse_mode": "HTML"}

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.warning(f"Telegram notification failed (status {response.status}): {body}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Telegram notification failed: {exc!r}")


def append_live_candle_to_csv(candle: Candle) -> None:
    """
    Appends a single completed 5min candle to LIVE_CANDLES_CSV_PATH, in
    the exact same format as our 1-year historical CSVs
    (open_time,open,high,low,close). Writes the header only if the file
    doesn't exist yet, so this safely accumulates real live history
    across bot restarts rather than overwriting it each time.
    """
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


def preload_historical_data(state: BotState) -> None:
    """
    Loads historical 5min candles from our existing Deriv-fetched CSVs
    (same underlying instrument/price data, just originally fetched
    via Deriv's Options API rather than MT5 -- the price history is
    identical since it's the same real-world market). Converts them
    into Candle objects, builds 1h candles, and replays swing-level
    detection across the whole history -- so the bot starts with real
    trend/level context instead of waiting hours/days to rebuild it
    from scratch via live MT5 ticks alone.
    """
    csv_path = os.path.join(HISTORICAL_DATA_DIR, f"{HISTORICAL_DATA_SYMBOL_PREFIX}_5min.csv")

    if not os.path.exists(csv_path):
        logger.warning(
            f"No historical data found at {csv_path} -- starting cold. "
            f"The bot will need to rebuild trend/level history from live "
            f"ticks (this is the original, slower behavior)."
        )
        return

    logger.info(f"Preloading historical data from {csv_path} ...")

    df = pd.read_csv(csv_path, index_col="open_time", parse_dates=True)
    df = df.sort_index()

    # STALENESS CHECK: warn loudly if the historical data is old. This
    # directly guards against a real bug we found: a multi-day-stale
    # historical file caused the EMA/trend filter to be computed from
    # outdated data at startup, with no visible warning -- the bot
    # silently used a trend value that didn't match the live market
    # for the first ~1 hour after every restart (until enough live
    # candles accumulated to build a fresh 1H candle).
    last_data_time = df.index[-1]
    now_utc = datetime.now(timezone.utc)
    if last_data_time.tzinfo is None:
        last_data_time = last_data_time.tz_localize(timezone.utc)
    staleness = now_utc - last_data_time
    staleness_hours = staleness.total_seconds() / 3600

    if staleness_hours > 24:
        logger.warning(
            f"⚠️ HISTORICAL DATA IS STALE: the most recent candle in {csv_path} "
            f"is {staleness_hours:.1f} hours old (from {last_data_time}). The "
            f"EMA/trend filter will be computed from outdated data until enough "
            f"live candles accumulate to replace it (~1 hour). Consider re-running "
            f"fetch_historical.py for this market before trusting early trend "
            f"readings. Continuing anyway."
        )
    else:
        logger.info(
            f"Historical data freshness OK: most recent candle is "
            f"{staleness_hours:.1f} hours old."
        )

    historical_candles: List[Candle] = []
    for open_time, row in df.iterrows():
        candle = Candle(
            timeframe="5min",
            open_time=open_time.to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_count=0,
        )
        historical_candles.append(candle)

    state.candles_5min = historical_candles[-2000:]
    logger.info(f"Loaded {len(state.candles_5min)} historical 5min candles.")

    chunk_size = 12  # 12 x 5min candles = 1 hour (was 60 for 1min candles)
    full_chunks = len(historical_candles) // chunk_size
    for i in range(full_chunks):
        chunk = historical_candles[i * chunk_size : (i + 1) * chunk_size]
        h1_candle = Candle(
            timeframe="1h",
            open_time=chunk[0].open_time,
            open=chunk[0].open,
            high=max(c.high for c in chunk),
            low=min(c.low for c in chunk),
            close=chunk[-1].close,
            tick_count=0,
        )
        state.candles_1h.append(h1_candle)

    state.candles_1h = state.candles_1h[-500:]
    logger.info(f"Built {len(state.candles_1h)} historical 1h candles.")

    full_history = state.candles_5min
    state.candles_5min = []

    for candle in full_history:
        state.candles_5min.append(candle)
        update_swing_levels(state, quiet=True)

    logger.info(
        f"Replayed swing-level detection -- {len(state.swing_levels)} levels "
        f"identified from historical data."
    )

    trend_check, _ = compute_current_trend(state)
    logger.info(f"Trend filter immediately usable after preload: trend={trend_check}")


async def run_bot() -> None:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        logger.error("Missing METAAPI_TOKEN or METAAPI_ACCOUNT_ID in .env file.")
        sys.exit(1)

    logger.info(f"Starting MT5 live trading bot for '{SYMBOL}' ...")
    logger.info(
        f"Strategy: EMA(14)-1H trend + S/R (lookback={SWING_LOOKBACK}, "
        f"tolerance={RETEST_TOLERANCE_PCT}%) + 10-pattern entry"
    )
    logger.info(
        f"Exit: initial stop (fixed {FIXED_STAGE1_STOP_PCT}%) -> breakeven after "
        f"{NUM_FAVORABLE_CANDLES_REQUIRED} favorable candles -> "
        f"Stage3 trail (window={STAGE3_TRAILING_WINDOW})"
    )
    logger.info(f"Risk per trade: {RISK_PER_TRADE_PCT}% of current balance")

    state = BotState()
    preload_historical_data(state)

    # Launch the background historical-data refresh task. This does NOT
    # block startup or trading in any way -- it just runs on its own
    # schedule, fully isolated, for long-running unattended deployments.
    background_refresh_task = asyncio.create_task(refresh_historical_data_background(state))
    background_refresh_task.set_name("historical_data_refresh")

    api = MetaApi(token=METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    if account.state != "DEPLOYED":
        await account.deploy()

    await account.wait_connected()

    connection = account.get_streaming_connection()
    await connection.connect()
    await connection.wait_synchronized()

    terminal_state = connection.terminal_state

    account_info = terminal_state.account_information
    state.current_balance = account_info.get("balance", 0.0)
    logger.info(f"Connected. Balance: ${state.current_balance:.2f}")

    await recover_open_position_if_any(connection, state)

    await connection.subscribe_to_market_data(symbol=SYMBOL)
    await asyncio.sleep(2)

    aggregator = CandleAggregator()

    def on_candle_close(candle: Candle) -> None:
        if candle.timeframe == "5min":
            state.candles_5min.append(candle)
            state.candles_5min = state.candles_5min[-2000:]
            append_live_candle_to_csv(candle)

    aggregator.on_candle_close = on_candle_close

    last_seen_open_time = None
    last_h1_build_count = 0

    logger.info("Polling live prices and building candles ... (Ctrl+C to stop)\n")

    last_staleness_warning_time = None
    last_health_check_time = None
    was_healthy = True
    unhealthy_since = None
    WATCHDOG_MAX_UNHEALTHY_MINUTES = 10  # force a restart if unhealthy this long, rather than waiting indefinitely (a real incident saw 2.5+ hours with no automatic recovery)

    try:
        while True:
            try:
                # PERIODIC HEALTH CHECK (every 60s): explicitly verify the
                # connection is genuinely healthy, using MetaApi's official
                # health_monitor API. This directly hardens against the
                # recurring "Task exception was never retrieved" /
                # subscription error we observed -- that error happens deep
                # inside the SDK's own retry logic and can fail silently,
                # leaving us with NO visibility into whether the subscription
                # (and therefore our price feed) actually recovered. Checking
                # health_status explicitly closes that gap.
                now_for_health = datetime.now(timezone.utc)
                if (
                    last_health_check_time is None
                    or (now_for_health - last_health_check_time).total_seconds() >= 60
                ):
                    last_health_check_time = now_for_health
                    try:
                        health = connection.health_monitor.health_status
                        is_healthy = (
                            health.get("connected", False)
                            and health.get("connectedToBroker", False)
                            and health.get("synchronized", False)
                            and health.get("quoteStreamingHealthy", False)
                        )

                        if not is_healthy:
                            reasons = []
                            if not health.get("connected", False):
                                reasons.append("API connection lost")
                            if not health.get("connectedToBroker", False):
                                reasons.append("broker connection lost")
                            if not health.get("synchronized", False):
                                reasons.append("terminal not synchronized")
                            if not health.get("quoteStreamingHealthy", False):
                                reasons.append("quote streaming unhealthy")

                            logger.error(f"CONNECTION HEALTH CHECK FAILED: {', '.join(reasons)}")
                            if was_healthy:
                                # only notify on the TRANSITION into unhealthy,
                                # to avoid spamming every 60s while it stays bad
                                await send_telegram_notification(
                                    f"🔴 <b>CONNECTION UNHEALTHY</b>\n"
                                    f"Reasons: {', '.join(reasons)}\n"
                                    f"Bot will keep retrying. Consider checking MT5/MetaApi directly "
                                    f"if this persists."
                                )
                                unhealthy_since = now_for_health
                            was_healthy = False

                            # WATCHDOG: if we've been continuously unhealthy
                            # for too long, MetaApi's own reconnection logic
                            # has clearly failed to recover on its own (a
                            # real incident saw this persist for 2.5+ hours
                            # straight) -- force a full process restart
                            # instead of waiting indefinitely. A fresh
                            # process re-establishes the connection from
                            # scratch, which is the same fix that worked
                            # manually every time this happened in practice.
                            if unhealthy_since is not None:
                                unhealthy_minutes = (now_for_health - unhealthy_since).total_seconds() / 60
                                if unhealthy_minutes >= WATCHDOG_MAX_UNHEALTHY_MINUTES:
                                    logger.error(
                                        f"WATCHDOG TRIGGERED: connection has been unhealthy for "
                                        f"{unhealthy_minutes:.1f} minutes (max allowed: "
                                        f"{WATCHDOG_MAX_UNHEALTHY_MINUTES}). Forcing a restart."
                                    )
                                    await send_telegram_notification(
                                        f"⚠️ <b>WATCHDOG: FORCING RESTART</b>\n"
                                        f"Connection unhealthy for {unhealthy_minutes:.1f} minutes with "
                                        f"no recovery. Restarting the bot process now."
                                    )
                                    raise SystemExit(
                                        f"Watchdog: unhealthy for {unhealthy_minutes:.1f} minutes, forcing restart."
                                    )
                        elif not was_healthy:
                            logger.info("Connection health RECOVERED.")
                            await send_telegram_notification("🟢 Connection health recovered.")
                            was_healthy = True
                            unhealthy_since = None
                    except Exception as health_exc:  # noqa: BLE001
                        logger.warning(f"Could not check connection health: {health_exc!r}")

                price = terminal_state.price(symbol=SYMBOL)
                if price:
                    # CRITICAL SAFETY CHECK: verify the quote is actually fresh
                    # before trading on it. terminal_state.price() can return a
                    # STALE cached value during/after a sync disruption (we
                    # confirmed this happened: 3 real trades were entered using
                    # a price quote that was ~3 hours 5 minutes old, right after
                    # a sync/reconnect disruption). The quote object includes
                    # its own 'time' field (the actual broker quote timestamp)
                    # -- we MUST check it rather than assume freshness just
                    # because our own clock says "now".
                    quote_time = price.get("time") or price.get("brokerTime") or price.get("quoteTime")
                    now = datetime.now(timezone.utc)

                    if quote_time is not None:
                        if quote_time.tzinfo is None:
                            quote_time = quote_time.replace(tzinfo=timezone.utc)
                        staleness_seconds = (now - quote_time).total_seconds()

                        if staleness_seconds > MAX_PRICE_STALENESS_SECONDS:
                            if (
                                last_staleness_warning_time is None
                                or (now - last_staleness_warning_time).total_seconds() > 30
                            ):
                                logger.warning(
                                    f"STALE PRICE DETECTED: quote_time={quote_time} is "
                                    f"{staleness_seconds:.0f}s old (max allowed: "
                                    f"{MAX_PRICE_STALENESS_SECONDS}s). Skipping this "
                                    f"price update -- NOT trading on stale data."
                                )
                                await send_telegram_notification(
                                    f"⚠️ <b>STALE PRICE DETECTED</b>\n"
                                    f"Quote is {staleness_seconds:.0f}s old (max allowed: "
                                    f"{MAX_PRICE_STALENESS_SECONDS}s).\n"
                                    f"Bot is skipping this price update -- not trading on stale data."
                                )
                                last_staleness_warning_time = now
                            await asyncio.sleep(0.5)
                            continue

                    mid_price = (price["bid"] + price["ask"]) / 2
                    epoch = datetime.now(timezone.utc).timestamp()
                    aggregator.add_tick(epoch, mid_price)

                    current_open_time = (
                        state.candles_5min[-1].open_time if state.candles_5min else None
                    )

                    if current_open_time is not None and current_open_time != last_seen_open_time:
                        last_seen_open_time = current_open_time

                        total_candles = len(state.candles_5min)
                        if total_candles >= 12 and total_candles - last_h1_build_count >= 12:
                            chunk = state.candles_5min[-12:]
                            h1_candle = Candle(
                                timeframe="1h",
                                open_time=chunk[0].open_time,
                                open=chunk[0].open,
                                high=max(c.high for c in chunk),
                                low=min(c.low for c in chunk),
                                close=chunk[-1].close,
                                tick_count=0,
                            )
                            state.candles_1h.append(h1_candle)
                            state.candles_1h = state.candles_1h[-500:]
                            last_h1_build_count = total_candles

                        update_swing_levels(state)

                        trend_status, _ = compute_current_trend(state)

                        # PROACTIVE TREND-CHANGE NOTIFICATION: only
                        # fires when the trend genuinely CHANGES
                        # (e.g. FLAT -> UP), not on every heartbeat --
                        # avoids spamming while still keeping the
                        # person informed of real shifts.
                        if trend_status is not None and trend_status != state.last_known_trend:
                            if state.last_known_trend is not None:  # skip the very first reading on startup
                                trend_emoji = {"UP": "📈", "DOWN": "📉", "FLAT": "➖"}.get(trend_status, "🔔")
                                await send_telegram_notification(
                                    f"{trend_emoji} <b>TREND CHANGED</b>\n"
                                    f"{state.last_known_trend} → {trend_status}"
                                )
                            state.last_known_trend = trend_status

                        logger.info(
                            f"Heartbeat: 5min_candles={len(state.candles_5min)} "
                            f"1h_candles={len(state.candles_1h)} "
                            f"levels_tracked={len(state.swing_levels)} "
                            f"trend={trend_status} "
                            f"open_position={'YES' if state.open_position else 'no'} "
                            f"balance=${state.current_balance:.2f} "
                            f"trend_filter_rejections={state.trend_filter_rejections}"
                        )

                        account_info = terminal_state.account_information
                        if account_info:
                            state.current_balance = account_info.get("balance", state.current_balance)

                        write_status_file(state)

                        if state.open_position is not None:
                            await manage_open_position(connection, state)
                        else:
                            await check_for_entry(connection, state)

                await asyncio.sleep(PRICE_POLL_INTERVAL_SECONDS)

            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error in main loop: {exc!r}. Continuing in 5s ...")
                await asyncio.sleep(5)
    finally:
        # CRITICAL: always clean up the connection and background task on
        # exit, including on KeyboardInterrupt (Ctrl+C). Without this, the
        # MetaApi SDK's own background websocket/reconnection logic keeps
        # running even after Ctrl+C stops the foreground loop -- which is
        # exactly the "tries to reconnect instead of stopping" behavior
        # that was observed.
        logger.info("Shutting down: closing connection and background tasks ...")
        background_refresh_task.cancel()
        try:
            await background_refresh_task
        except asyncio.CancelledError:
            pass
        try:
            await connection.close()
        except Exception as close_exc:  # noqa: BLE001
            logger.warning(f"Error closing connection during shutdown: {close_exc!r}")
        logger.info("Shutdown complete.")


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Unexpected error: {exc!r}")


if __name__ == "__main__":
    main()