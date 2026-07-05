"""
live_mt5_test_bot_v25.py

SIMPLIFIED TEST BOT for Volatility 25 (1s) Index.

Entry conditions (relaxed vs main bot):
  - Trend from MT5 1H EMA(14) must be UP or DOWN (no FLAT)
  - ANY candlestick pattern on 5min candle confirms entry
  - NO swing level retest required
  - NO trend strength filter

Exit logic (identical to main bot):
  - Stage 1: fixed stop loss (0.057%)
  - Stage 2: after 2 favorable candles -> move stop to breakeven
  - Stage 3: tight trailing stop (last 2 candles)
  - Trend reversal -> close early

Run alongside the main bot:
    python live_bots/1HZ25V/live_mt5_test_bot_v25.py
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
import ssl
import certifi
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Repo-root discovery
# ---------------------------------------------------------------------------

def _find_repo_root(start_path: str) -> str:
    current = os.path.abspath(start_path)
    for _ in range(10):
        if os.path.isdir(os.path.join(current, "src", "strategy")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise RuntimeError(f"Could not find repo root from {start_path}")

_REPO_ROOT     = _find_repo_root(os.path.dirname(os.path.abspath(__file__)))
STRATEGY_PATH  = os.path.join(_REPO_ROOT, "src", "strategy")
DATA_PATH      = os.path.join(_REPO_ROOT, "src", "data")
MARKET_DIR     = os.path.join(_REPO_ROOT, "markets", "1HZ75V")
HISTORICAL_DATA_DIR = os.path.join(MARKET_DIR, "data")

sys.path.insert(0, STRATEGY_PATH)
sys.path.insert(0, DATA_PATH)
sys.path.insert(0, MARKET_DIR)

from candle_builder import Candle, CandleAggregator
import indicators
from candlestick_patterns import (
    Candle as PatternCandle,
    is_bullish_engulfing,
    is_bearish_engulfing,
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

SYMBOL                        = "Volatility 75 (1s) Index"
HISTORICAL_DATA_SYMBOL_PREFIX = "1HZ75V"
BOT_NAME                      = "TEST-V75"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# Exit parameters (same as main bot)
FIXED_STAGE1_STOP_PCT          = 0.344
NUM_FAVORABLE_CANDLES_REQUIRED = 2
STAGE3_TRAILING_WINDOW         = 2

# Risk (same as main bot)
RISK_PER_TRADE_PCT = 1.0
MIN_VOLUME         = 0.001
MAX_VOLUME         = 2.0
VOLUME_STEP        = 0.001
MAGIC_NUMBER       = 234075  # different from main bot to avoid conflicts

MAX_PRICE_STALENESS_SECONDS = 30
PRICE_POLL_INTERVAL_SECONDS = 1.0

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(
    LOG_DIR,
    f"test_bot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger("test_bot_v25")
logger.setLevel(logging.INFO)

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    position_id: int
    direction: str
    entry_price: float
    volume: float
    trailing_stop: float
    entry_time: Optional[datetime] = None
    favorable_candle_count: int = 0
    stage3_active: bool = False
    matched_pattern: str = ""


@dataclass
class BotState:
    candles_5min: List[Candle] = field(default_factory=list)
    candles_1h: List[Candle] = field(default_factory=list)
    open_position: Optional[OpenPosition] = None
    current_balance: float = 0.0
    trades_completed: int = 0
    last_known_trend: Optional[str] = None
    recent_trades_log: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# MT5 helpers
# ---------------------------------------------------------------------------

def mt5_initialize() -> bool:
    if not mt5.initialize():
        logger.error(f"MT5 initialize failed: {mt5.last_error()}")
        return False
    account = mt5.account_info()
    if account is None:
        logger.error("Could not get MT5 account info.")
        return False
    logger.info(f"MT5 connected: account={account.login} balance={account.balance:.2f}")
    return True


def get_balance() -> float:
    info = mt5.account_info()
    return info.balance if info else 0.0


def get_current_tick() -> Optional[dict]:
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
    return round(max(MIN_VOLUME, min(MAX_VOLUME, result)), 3)


def calculate_volume(balance: float, current_price: float) -> float:
    risk_dollars           = balance * (RISK_PER_TRADE_PCT / 100)
    stop_distance_fraction = FIXED_STAGE1_STOP_PCT / 100
    raw_volume             = risk_dollars / (current_price * stop_distance_fraction)
    return round_volume(raw_volume)


def get_open_position() -> Optional[mt5.TradePosition]:
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        return None
    for p in positions:
        if p.magic == MAGIC_NUMBER:
            return p
    return None


def update_sl_on_broker(ticket: int, new_sl: float) -> bool:
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol":   SYMBOL,
        "sl":       new_sl,
        "tp":       0.0,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.warning(f"SL update failed for ticket {ticket}")
        return False
    return True


# ---------------------------------------------------------------------------
# Trend detection (direct from MT5 1H candles)
# ---------------------------------------------------------------------------

def fetch_1h_candles_from_mt5(count: int = 100) -> List[Candle]:
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, count)
    if rates is None or len(rates) == 0:
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
    return candles


def compute_trend(state: BotState):
    if len(state.candles_1h) < 20:
        return None
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
        return None
    last_close  = state.candles_5min[-1].close if state.candles_5min else None
    if last_close is None:
        return None
    ema_rising  = last_ema > prev_ema
    price_above = last_close > last_ema
    if price_above and ema_rising:
        return "UP"
    if not price_above and not ema_rising:
        return "DOWN"
    return "FLAT"


# ---------------------------------------------------------------------------
# Candlestick pattern detection (any pattern = signal)
# ---------------------------------------------------------------------------

def _to_pc(c: Candle) -> PatternCandle:
    return PatternCandle(open=c.open, high=c.high, low=c.low, close=c.close)


def detect_buy_pattern(candles: list) -> Optional[str]:
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    if is_bullish_engulfing(_to_pc(prev), _to_pc(cur)):
        return "Bullish Engulfing"
    if is_piercing_line(_to_pc(prev), _to_pc(cur)):
        return "Piercing Line"
    if len(candles) >= 3:
        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        if is_morning_star(_to_pc(c1), _to_pc(c2), _to_pc(c3)):
            return "Morning Star"
        if is_three_white_soldiers(_to_pc(c1), _to_pc(c2), _to_pc(c3)):
            return "Three White Soldiers"
    return None


def detect_sell_pattern(candles: list) -> Optional[str]:
    if len(candles) < 1:
        return None
    cur = candles[-1]
    if is_shooting_star(_to_pc(cur)):
        return "Shooting Star"
    if len(candles) >= 2:
        prev = candles[-2]
        if is_bearish_engulfing(_to_pc(prev), _to_pc(cur)):
            return "Bearish Engulfing"
        if is_dark_cloud_cover(_to_pc(prev), _to_pc(cur)):
            return "Dark Cloud Cover"
    if len(candles) >= 3:
        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        if is_three_black_crows(_to_pc(c1), _to_pc(c2), _to_pc(c3)):
            return "Three Black Crows"
        if is_evening_star(_to_pc(c1), _to_pc(c2), _to_pc(c3)):
            return "Evening Star"
    if len(candles) >= 5:
        if is_falling_three_methods([_to_pc(c) for c in candles[-5:]]):
            return "Falling Three Methods"
    return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    labeled = f"<b>[{BOT_NAME}]</b>\n{message}"
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": labeled, "parse_mode": "HTML"}
    try:
        connector = aiohttp.TCPConnector(ssl=SSL_CONTEXT)
        timeout   = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning(f"Telegram failed ({resp.status}): {await resp.text()}")
    except Exception as exc:
        logger.warning(f"Telegram failed: {exc!r}")


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

async def open_trade(state: BotState, direction: str, pattern: str) -> None:
    tick = get_current_tick()
    if tick is None:
        return

    current_price = (tick["bid"] + tick["ask"]) / 2
    volume        = calculate_volume(state.current_balance, current_price)

    if direction == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick["ask"]
        stop_loss  = price * (1 - FIXED_STAGE1_STOP_PCT / 100)
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick["bid"]
        stop_loss  = price * (1 + FIXED_STAGE1_STOP_PCT / 100)

    symbol_info  = mt5.symbol_info(SYMBOL)
    filling_type = mt5.ORDER_FILLING_IOC
    if symbol_info and symbol_info.filling_mode == 1:
        filling_type = mt5.ORDER_FILLING_FOK

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       volume,
        "type":         order_type,
        "price":        price,
        "sl":           stop_loss,
        "tp":           0.0,
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "comment":      f"test:{pattern[:20]}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }

    logger.info(f"Opening {direction} [{pattern}]: vol={volume} price={price:.5f} sl={stop_loss:.5f}")
    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        error = result.comment if result else str(mt5.last_error())
        logger.error(f"Order failed: {error}")
        await send_telegram(f"❌ <b>ORDER FAILED</b>\n{direction} [{pattern}]\nError: {error}")
        return

    ticket      = result.order
    entry_price = result.price

    state.open_position = OpenPosition(
        position_id=ticket,
        direction=direction,
        entry_price=entry_price,
        volume=volume,
        trailing_stop=stop_loss,
        entry_time=datetime.now(timezone.utc),
        matched_pattern=pattern,
    )

    logger.info(f"✅ TRADE OPENED: {direction} [{pattern}] ticket={ticket} entry={entry_price:.5f}")
    await send_telegram(
        f"🟢 <b>TRADE OPENED</b>\n"
        f"Direction: {direction}\n"
        f"Pattern: {pattern}\n"
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
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       position.volume,
        "type":         order_type,
        "position":     position.position_id,
        "price":        price,
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "comment":      f"close:{reason}",
        "type_time":    mt5.ORDER_TIME_GTC,
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
            await send_telegram(
                f"⚠️ <b>CLOSE FAILED</b>\n"
                f"Ticket: {position.position_id}\n"
                f"Retcode: {result.retcode if result else 'None'}\n"
                f"Error: {error}"
            )
            return
        else:
            # Position closed despite error retcode (e.g. broker SL hit simultaneously)
            logger.warning(f"Close returned retcode={result.retcode if result else 'None'} but position is gone -- treating as closed.")

    elif position_still_open:
        # Success retcode but position still open -- retry once
        logger.warning(f"Close retcode={result.retcode} but position still open -- retrying...")
        await asyncio.sleep(2)
        position_still_open = get_open_position() is not None
        if position_still_open:
            logger.error(f"Position still open after retry -- giving up.")
            await send_telegram(
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
    outcome_emoji = "✅" if pct_change > 0 else "❌"

    state.recent_trades_log.append({
        "direction":   position.direction,
        "pattern":     position.matched_pattern,
        "entry_price": position.entry_price,
        "exit_price":  exit_price,
        "pct_change":  pct_change,
        "dollar_pnl":  dollar_pnl,
        "reason":      reason,
        "closed_at":   datetime.now(timezone.utc).isoformat(),
    })

    logger.info(f"🔚 CLOSED ({reason}): P&L {pct_change:+.4f}% (≈${dollar_pnl:+.2f})")
    await send_telegram(
        f"{outcome_emoji} <b>TRADE CLOSED</b>\n"
        f"Direction: {position.direction}\n"
        f"Pattern: {position.matched_pattern}\n"
        f"Reason: {reason}\n"
        f"Entry: {position.entry_price:.5f} → Exit: {exit_price:.5f}\n"
        f"Result: {pct_change:+.4f}% (≈${dollar_pnl:+.2f})\n"
        f"Balance: ${state.current_balance:.2f}\n"
        f"Trades: {state.trades_completed}"
    )
    state.open_position = None


# ---------------------------------------------------------------------------
# Position management (identical to main bot)
# ---------------------------------------------------------------------------

async def manage_open_position(state: BotState) -> None:
    position = state.open_position
    if position is None:
        return

    # Check if broker closed it
    mt5_pos = get_open_position()
    if mt5_pos is None:
        logger.warning(f"Position {position.position_id} closed by broker.")
        state.trades_completed += 1
        state.open_position = None
        await send_telegram(
            f"⚠️ <b>POSITION CLOSED BY BROKER</b>\n"
            f"Ticket: {position.position_id}\nLikely hit stop loss."
        )
        return

    # Trend reversal check
    trend = compute_trend(state)
    position_expects = "UP" if position.direction == "BUY" else "DOWN"
    if trend is not None and trend != position_expects and trend != "FLAT":
        await close_trade(state, reason=f"trend reversed to {trend}")
        return

    current_candle = state.candles_5min[-1]

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
        await close_trade(state, reason="stop hit")
        return

    is_favorable = (
        current_candle.close > current_candle.open if position.direction == "BUY"
        else current_candle.close < current_candle.open
    )
    if is_favorable:
        position.favorable_candle_count += 1
        if position.favorable_candle_count >= NUM_FAVORABLE_CANDLES_REQUIRED:
            position.trailing_stop = position.entry_price
            position.stage3_active = True
            update_sl_on_broker(position.position_id, position.trailing_stop)
            logger.info(f"Breakeven reached! SL moved to entry {position.entry_price:.5f}")
    else:
        position.favorable_candle_count = 0


async def check_for_entry(state: BotState) -> None:
    if state.open_position is not None:
        return
    if len(state.candles_5min) < 3:
        return

    trend = compute_trend(state)
    if trend not in ("UP", "DOWN"):
        return

    # Notify trend change
    if trend != state.last_known_trend:
        if state.last_known_trend is not None:
            emoji = "📈" if trend == "UP" else "📉"
            await send_telegram(f"{emoji} <b>TREND CHANGED</b>\n{state.last_known_trend} → {trend}")
        state.last_known_trend = trend

    # Check for pattern — NO swing level required, NO strength filter
    if trend == "UP":
        pattern = detect_buy_pattern(state.candles_5min)
        if pattern:
            logger.info(f"BUY signal: {pattern} | trend={trend}")
            await open_trade(state, "BUY", pattern)
    else:
        pattern = detect_sell_pattern(state.candles_5min)
        if pattern:
            logger.info(f"SELL signal: {pattern} | trend={trend}")
            await open_trade(state, "SELL", pattern)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    logger.info(f"Starting TEST BOT for '{SYMBOL}' ...")
    logger.info("Entry: trend (UP/DOWN) + any candlestick pattern (NO level filter)")
    logger.info(f"Exit: fixed {FIXED_STAGE1_STOP_PCT}% stop -> breakeven -> Stage3 trail")
    logger.info(f"Risk: {RISK_PER_TRADE_PCT}% per trade | Magic: {MAGIC_NUMBER}")

    if not mt5_initialize():
        logger.error("Failed to initialize MT5.")
        sys.exit(1)

    state = BotState()
    state.current_balance = get_balance()

    # Load 1H candles from MT5
    state.candles_1h = fetch_1h_candles_from_mt5(count=100)
    logger.info(f"Loaded {len(state.candles_1h)} 1H candles from MT5.")

    # Load historical 5min candles
    csv_path = os.path.join(HISTORICAL_DATA_DIR, "1HZ75V_5min.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path, index_col="open_time", parse_dates=True).sort_index()
        for open_time, row in df.tail(500).iterrows():
            state.candles_5min.append(Candle(
                timeframe="5min",
                open_time=open_time.to_pydatetime(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                tick_count=0,
            ))
        logger.info(f"Preloaded {len(state.candles_5min)} historical 5min candles.")

    initial_trend = compute_trend(state)
    state.last_known_trend = initial_trend
    logger.info(f"Initial trend: {initial_trend}")

    await send_telegram(
        f"🟢 <b>TEST BOT STARTED</b>\n"
        f"Symbol: {SYMBOL}\n"
        f"Balance: ${state.current_balance:.2f}\n"
        f"Trend: {initial_trend}\n"
        f"Entry: pattern + trend only (no level filter)\n"
        f"Magic: {MAGIC_NUMBER}"
    )

    aggregator = CandleAggregator()

    def on_candle_close(candle: Candle) -> None:
        if candle.timeframe == "5min":
            state.candles_5min.append(candle)
            state.candles_5min = state.candles_5min[-500:]

    aggregator.on_candle_close = on_candle_close

    last_seen_open_time = None
    last_1h_refresh     = datetime.now(timezone.utc)
    last_staleness_warn = None

    logger.info("Polling live prices ... (Ctrl+C to stop)\n")

    try:
        while True:
            try:
                # MT5 connection check
                if not mt5.terminal_info():
                    logger.error("MT5 connection lost. Reconnecting ...")
                    mt5.shutdown()
                    await asyncio.sleep(5)
                    if not mt5_initialize():
                        await asyncio.sleep(30)
                        continue

                tick = get_current_tick()
                if tick is None:
                    await asyncio.sleep(1)
                    continue

                now       = datetime.now(timezone.utc)
                staleness = (now - tick["time"]).total_seconds()
                if staleness > MAX_PRICE_STALENESS_SECONDS:
                    if last_staleness_warn is None or (now - last_staleness_warn).total_seconds() > 30:
                        logger.warning(f"STALE TICK: {staleness:.0f}s old")
                        last_staleness_warn = now
                    await asyncio.sleep(0.5)
                    continue

                mid_price = (tick["bid"] + tick["ask"]) / 2
                aggregator.add_tick(now.timestamp(), mid_price)

                # Refresh 1H candles every 60 minutes
                if (now - last_1h_refresh).total_seconds() >= 3600:
                    new_1h = fetch_1h_candles_from_mt5(count=100)
                    if new_1h:
                        state.candles_1h = new_1h
                    last_1h_refresh = now

                live_candle       = aggregator.get_current("5min")
                current_open_time = live_candle.open_time if live_candle else None

                if current_open_time is not None and current_open_time != last_seen_open_time:
                    last_seen_open_time   = current_open_time
                    state.current_balance = get_balance()

                    logger.info(
                        f"Heartbeat: 5min={len(state.candles_5min)} "
                        f"trend={compute_trend(state)} "
                        f"position={'YES' if state.open_position else 'no'} "
                        f"balance=${state.current_balance:.2f} "
                        f"trades={state.trades_completed}"
                    )

                    if state.open_position is not None:
                        await manage_open_position(state)
                    else:
                        await check_for_entry(state)

                await asyncio.sleep(PRICE_POLL_INTERVAL_SECONDS)

            except Exception as exc:
                logger.exception(f"Main loop error: {exc!r}. Retrying in 5s ...")
                await asyncio.sleep(5)

    finally:
        mt5.shutdown()
        logger.info("Test bot shutdown complete.")


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Test bot stopped by user.")
    except Exception as exc:
        logger.exception(f"Unexpected error: {exc!r}")


if __name__ == "__main__":
    main()
