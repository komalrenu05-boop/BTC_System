"""
BTC Liquidity Zone Monitor — Production Grade
============================================================
Monitors a user-defined price range and accumulates order flow
data (delta + OI) while price is inside the zone.

WORKFLOW:
  1. Set your zone:   echo "96200 96800" > zone.txt
  2. Run:             python btc_monitor.py
  3. Code arms itself, waits for price to enter range
  4. Price enters  → accumulation starts automatically
  5. Price leaves  → final summary printed, resets, waits
  6. Set new zone: echo "95000 95600" > zone.txt  (no restart needed)

STATES:
  WAITING   → zone.txt is empty or missing
  ARMED     → range loaded, watching for price entry
  INSIDE    → price in zone, accumulating
  COMPLETED → price left zone, summary shown, waiting for new zone

INSTALL:
  pip install websockets aiohttp
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import aiohttp
print("RUNNING btc_monitor_v3.py - latest version")

# ─────────────────────────── CONFIG ───────────────────────────

SYMBOL         = "btcusdt"
FUTURES_SYMBOL = "BTCUSDT"
WS_URL         = f"wss://fstream.binance.com/ws/{SYMBOL}@aggTrade"
OI_URL         = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={FUTURES_SYMBOL}"
PRICE_URL      = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={FUTURES_SYMBOL}"
ZONE_FILE      = "zone.txt"
PRINT_INTERVAL = 5          # seconds between live updates while inside zone
OI_INTERVAL    = 3          # seconds between OI polls
RECONNECT_BASE = 2          # base reconnect delay seconds
RECONNECT_MAX  = 30         # max reconnect delay seconds
LOG_FILE       = "btc_monitor.log"

# ─────────────────────────── LOGGING ──────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("btc_monitor")
logging.getLogger("aiohttp").setLevel(logging.WARNING)

# ─────────────────────────── STATE ────────────────────────────

class ZoneState(Enum):
    WAITING   = auto()
    ARMED     = auto()
    INSIDE    = auto()
    COMPLETED = auto()

@dataclass
class ZoneAccumulator:
    zone_low:    float = 0.0
    zone_high:   float = 0.0
    buy_volume:  float = 0.0
    sell_volume: float = 0.0
    entry_time:  float = 0.0
    entry_price: float = 0.0
    exit_reason: str   = ""   # "ABOVE" or "BELOW"

    def reset(self):
        self.buy_volume  = 0.0
        self.sell_volume = 0.0
        self.entry_time  = 0.0
        self.entry_price = 0.0
        self.exit_reason = ""

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    @property
    def buy_pct(self) -> float:
        return (self.buy_volume / self.total_volume * 100) if self.total_volume > 0 else 0.0

    @property
    def sell_pct(self) -> float:
        return (self.sell_volume / self.total_volume * 100) if self.total_volume > 0 else 0.0

    @property
    def time_in_zone(self) -> float:
        return time.time() - self.entry_time if self.entry_time > 0 else 0.0

@dataclass
class Snapshot:
    """One 5s update captured for the summary table."""
    time_str:  str
    price:     float
    delta:     float
    volume:    float
    buy_pct:   float
    sell_pct:  float
    oi_change: float

@dataclass
class AppState:
    zone_state:    ZoneState       = ZoneState.WAITING
    zone:          ZoneAccumulator = field(default_factory=ZoneAccumulator)
    current_price: float           = 0.0
    current_oi:    float           = 0.0
    zone_entry_oi: float           = 0.0
    lock:          asyncio.Lock    = field(default_factory=asyncio.Lock)
    running:       bool            = True
    last_zone_str: str             = ""
    snapshots:     list            = field(default_factory=list)  # stores last 10 Snapshots

state = AppState()

# ─────────────────────────── SIGNAL HANDLER ───────────────────

def shutdown(sig, frame):
    log.warning(f"Signal {sig} — shutting down.")
    state.running = False
    sys.exit(0)

signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ─────────────────────────── ZONE FILE ────────────────────────

def read_zone_file() -> Optional[tuple]:
    """
    Reads zone.txt. Format: 'LOW HIGH'  e.g. '96200 96800'
    Returns (low, high) tuple or None if missing/invalid.
    """
    try:
        if not os.path.exists(ZONE_FILE):
            return None
        content = open(ZONE_FILE).read().strip()
        if not content:
            return None
        parts = content.split()
        if len(parts) != 2:
            log.warning(f"zone.txt bad format. Expected: 'LOW HIGH' e.g. '96200 96800'")
            return None
        low, high = float(parts[0]), float(parts[1])
        if low >= high:
            log.warning(f"Invalid zone: {low} must be less than {high}")
            return None
        return low, high
    except Exception as e:
        log.warning(f"zone.txt read error: {e}")
        return None

# ─────────────────────────── ZONE WATCHER ─────────────────────

async def zone_watcher():
    """
    Polls zone.txt every 2 seconds for changes.
    Arms a new zone when WAITING or COMPLETED.
    Ignores changes while INSIDE (zone must complete first).
    """
    while state.running:
        await asyncio.sleep(2)

        try:
            content = open(ZONE_FILE).read().strip() if os.path.exists(ZONE_FILE) else ""
        except Exception:
            content = ""

        if content == state.last_zone_str:
            continue

        state.last_zone_str = content
        result = read_zone_file()

        async with state.lock:
            if result is None:
                if state.zone_state in (ZoneState.WAITING, ZoneState.COMPLETED):
                    state.zone_state = ZoneState.WAITING
                    log.info("zone.txt cleared — WAITING for new zone.")
            else:
                low, high = result
                if state.zone_state in (ZoneState.WAITING, ZoneState.COMPLETED):
                    state.zone.zone_low  = low
                    state.zone.zone_high = high
                    state.zone.reset()
                    state.zone_state = ZoneState.ARMED
                    log.info(f"Zone ARMED: ${low:,.0f} — ${high:,.0f}")
                    print_armed_banner(low, high)
                elif state.zone_state == ZoneState.INSIDE:
                    log.warning("Zone file changed while INSIDE zone — will apply after zone completes.")

# ─────────────────────────── OI LOOP ──────────────────────────

async def oi_loop(session: aiohttp.ClientSession):
    """Polls open interest every OI_INTERVAL seconds. Fully async."""
    while state.running:
        try:
            async with session.get(OI_URL, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                data = await resp.json()
                async with state.lock:
                    state.current_oi = float(data["openInterest"])
        except Exception as e:
            log.warning(f"OI fetch error: {e}")
        await asyncio.sleep(OI_INTERVAL)

# ─────────────────────────── TRADE STREAM ─────────────────────

async def trade_stream():
    """
    Binance aggTrade WebSocket.
    - m=True  → buyer is maker → SELL aggression
    - m=False → buyer is taker → BUY aggression
    Only accumulates when zone_state == INSIDE.
    Reconnects with exponential backoff on failure.
    """
    backoff = RECONNECT_BASE

    while state.running:
        try:
            import websockets
            log.info("Connecting to WebSocket...")

            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                log.info("WebSocket connected ✅")
                backoff = RECONNECT_BASE  # reset on success

                async for raw in ws:
                    if not state.running:
                        return

                    data           = json.loads(raw)
                    qty            = float(data["q"])
                    is_buyer_maker = data["m"]

                    async with state.lock:
                        if state.zone_state == ZoneState.INSIDE:
                            if is_buyer_maker:
                                state.zone.sell_volume += qty   # sell aggression
                            else:
                                state.zone.buy_volume  += qty   # buy aggression

        except Exception as e:
            log.error(f"WebSocket error: {e} — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

# ─────────────────────────── PRICE MONITOR ────────────────────

async def price_monitor(session: aiohttp.ClientSession):
    """
    Fetches mark price every 1 second.
    Drives zone state transitions:
      ARMED  + price enters range → INSIDE (start accumulating)
      INSIDE + price leaves range → COMPLETED (print summary, reset)
    """
    while state.running:
        await asyncio.sleep(1)

        try:
            async with session.get(PRICE_URL, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                data  = await resp.json()
                price = float(data["markPrice"])
        except Exception as e:
            log.warning(f"Price fetch error: {e}")
            continue

        async with state.lock:
            state.current_price = price

            if state.zone_state == ZoneState.ARMED:
                if state.zone.zone_low <= price <= state.zone.zone_high:
                    state.zone_state       = ZoneState.INSIDE
                    state.zone.entry_time  = time.time()
                    state.zone.entry_price = price
                    state.zone_entry_oi    = state.current_oi
                    log.info(f"Price ENTERED zone at ${price:,.2f} — accumulation started")
                    print_entry_banner(price)

            elif state.zone_state == ZoneState.INSIDE:
                if price > state.zone.zone_high:
                    state.zone.exit_reason = "ABOVE"
                    _finalize_zone(price)
                elif price < state.zone.zone_low:
                    state.zone.exit_reason = "BELOW"
                    _finalize_zone(price)

def _finalize_zone(exit_price: float):
    """Called inside lock. Prints summary and transitions to COMPLETED."""
    state.zone_state = ZoneState.COMPLETED

    print_zone_summary(
        exit_price  = exit_price,
        z           = state.zone,
        oi          = state.current_oi,
        entry_oi    = state.zone_entry_oi,
    )

    log.info(
        f"ZONE COMPLETE | delta={state.zone.delta:+.4f}BTC "
        f"vol={state.zone.total_volume:.4f}BTC "
        f"exit={state.zone.exit_reason} "
        f"at=${exit_price:,.2f}"
    )

    # Clear snapshots for next zone
    state.snapshots = []

    # Clear zone.txt so user must deliberately set a new one
    try:
        open(ZONE_FILE, "w").write("")
    except Exception:
        pass
    state.last_zone_str = ""

# ─────────────────────────── LIVE PRINTER ─────────────────────

async def live_printer():
    """
    While INSIDE:
      - Prints live update every PRINT_INTERVAL seconds.
      - Captures a snapshot each update into state.snapshots.
      - Every 10th update prints the 10-row summary table.
    While ARMED:  quiet heartbeat log every 30s.
    While WAITING: reminder log every 60s.
    """
    tick          = 0
    total_updates = 0  # counts updates fired while INSIDE, never resets mid-session

    while state.running:
        await asyncio.sleep(PRINT_INTERVAL)
        tick += 1

        async with state.lock:
            zs       = state.zone_state
            price    = state.current_price
            z        = state.zone
            oi       = state.current_oi
            e_oi     = state.zone_entry_oi
            delta    = z.delta
            volume   = z.total_volume
            buy_pct  = z.buy_pct
            sell_pct = z.sell_pct
            elapsed  = z.time_in_zone

        if zs == ZoneState.INSIDE:
            total_updates += 1
            oi_change = oi - e_oi if e_oi > 0 else 0.0

            # Capture snapshot with fully copied values (no z property access later)
            snap = Snapshot(
                time_str  = time.strftime("%H:%M:%S"),
                price     = price,
                delta     = delta,
                volume    = volume,
                buy_pct   = buy_pct,
                sell_pct  = sell_pct,
                oi_change = oi_change,
            )
            state.snapshots.append(snap)
            if len(state.snapshots) > 10:
                state.snapshots = state.snapshots[-10:]

            # Print live update using pre-copied values only
            _print_live(price, delta, volume, buy_pct, sell_pct, oi_change, elapsed,
                        z.zone_low, z.zone_high)

            # Print summary table on every 10th update
            if total_updates % 10 == 0:
                print_snapshot_table(list(state.snapshots))

        else:
            # Only reset when transitioning OUT of a completed zone
            # (i.e. was counting, now stopped) — not on every non-INSIDE tick
            if zs == ZoneState.ARMED and total_updates > 0:
                total_updates = 0
                state.snapshots = []

            if zs == ZoneState.ARMED and tick % 6 == 0:
                log.info(
                    f"ARMED | Watching ${z.zone_low:,.0f}–${z.zone_high:,.0f} | "
                    f"Current: ${price:,.2f}"
                )
            elif zs == ZoneState.WAITING and tick % 12 == 0:
                log.info("WAITING | Set zone with:  echo 'LOW HIGH' > zone.txt")

# ─────────────────────────── PRINT HELPERS ────────────────────

BAR_WIDTH = 24

def bar(pct: float, width: int = BAR_WIDTH) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)

def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def build_signal(oi_change: float, delta: float) -> str:
    if oi_change > 0 and delta > 0:
        return "🟢 LONG BUILDUP    — buyers opening positions"
    elif oi_change > 0 and delta < 0:
        return "🔴 SHORT BUILDUP   — sellers opening positions"
    elif oi_change < 0 and delta > 0:
        return "🟡 SHORT SQUEEZE   — shorts being forced out"
    elif oi_change < 0 and delta < 0:
        return "🔴 LONG LIQUIDATION — longs being forced out"
    else:
        return "⚪ INDECISION       — no clear pressure yet"

def print_snapshot_table(snaps: list):
    """
    Prints a compact 10-row summary table after every 10th update.
    Shows trend direction at the bottom so you can decide at a glance.
    """
    if not snaps:
        return

    W = 79  # total table width

    print("\n" + "┌" + "─"*W + "┐")
    print(f"│  📊 SUMMARY — Last {len(snaps)} Updates ({len(snaps)*5}s){'':<{W - 33 - len(str(len(snaps)))*2}}│")
    print("├" + "─"*7 + "┬" + "─"*10 + "┬" + "─"*12 + "┬" + "─"*10 + "┬" + "─"*9 + "┬" + "─"*8 + "┬" + "─"*9 + "┤")
    print(f"│{'  #':^7}│{'  Time':^10}│{'  Price':^12}│{'  Delta':^10}│{'  Vol':^9}│{'  Buy%':^8}│{'  OI Δ':^9}│")
    print("├" + "─"*7 + "┼" + "─"*10 + "┼" + "─"*12 + "┼" + "─"*10 + "┼" + "─"*9 + "┼" + "─"*8 + "┼" + "─"*9 + "┤")

    for i, s in enumerate(snaps, 1):
        buy_icon = "🟢" if s.buy_pct >= 55 else "🔴" if s.buy_pct <= 45 else "🟡"
        oi_icon  = "↑" if s.oi_change > 0 else "↓" if s.oi_change < 0 else "─"
        print(
            f"│{i:^7}│"
            f" {s.time_str:^9}│"
            f" ${s.price:>9,.0f} │"
            f" {s.delta:>+8.2f} │"
            f" {s.volume:>7.2f} │"
            f" {s.buy_pct:>4.1f}% {buy_icon}│"
            f" {s.oi_change:>+6.1f}{oi_icon} │"
        )

    print("├" + "─"*7 + "┴" + "─"*10 + "┴" + "─"*12 + "┼" + "─"*10 + "┼" + "─"*9 + "┼" + "─"*8 + "┼" + "─"*9 + "┤")

    # Compute trend for each column
    deltas    = [s.delta    for s in snaps]
    volumes   = [s.volume   for s in snaps]
    buy_pcts  = [s.buy_pct  for s in snaps]
    oi_chgs   = [s.oi_change for s in snaps]

    def trend(values: list) -> str:
        if len(values) < 2:
            return "  ─  "
        rising  = sum(1 for a, b in zip(values, values[1:]) if b > a)
        falling = sum(1 for a, b in zip(values, values[1:]) if b < a)
        total   = len(values) - 1
        if rising   >= total * 0.7: return " 📈 +"
        if falling  >= total * 0.7: return " 📉 -"
        return " ↕ MIX"

    avg_buy = sum(buy_pcts) / len(buy_pcts)
    dom     = "BUYERS 🟢" if avg_buy >= 55 else "SELLERS 🔴" if avg_buy <= 45 else "MIXED  🟡"

    print(
        f"│{'  TREND':<29}│"
        f"{trend(deltas):^10}│"
        f"{trend(volumes):^9}│"
        f"{dom:^8}│"
        f"{trend(oi_chgs):^9}│"
    )
    print("└" + "─"*29 + "┴" + "─"*10 + "┴" + "─"*9 + "┴" + "─"*8 + "┴" + "─"*9 + "┘\n")


def print_armed_banner(low: float, high: float):
    print("\n╔" + "═"*54 + "╗")
    print( "║  🎯 ZONE ARMED                                       ║")
    print(f"║  Range : ${low:>10,.2f}  —  ${high:<10,.2f}              ║")
    print( "║  Waiting for price to enter...                      ║")
    print( "╚" + "═"*54 + "╝\n")

def print_entry_banner(price: float):
    print("\n╔" + "═"*54 + "╗")
    print(f"║  ✅ PRICE ENTERED ZONE at ${price:>10,.2f}               ║")
    print( "║  Accumulation started — updates every 5s            ║")
    print( "╚" + "═"*54 + "╝\n")

def _print_live(price: float, delta: float, volume: float, buy_pct: float,
                sell_pct: float, oi_change: float, elapsed_secs: float,
                zone_low: float, zone_high: float):
    """Prints live update using only pre-copied plain values — no object access."""
    delta_usd = delta * price
    vol_usd   = volume * price
    signal    = build_signal(oi_change, delta)
    elapsed   = fmt_time(elapsed_secs)

    print("\n" + "═"*56)
    print(f"  ⏱  {time.strftime('%H:%M:%S')}   BTC ≈ ${price:>10,.2f}")
    print("═"*56)
    print(f"  📦 ZONE  ${zone_low:,.0f} — ${zone_high:,.0f}   ✅ INSIDE  ({elapsed})")
    print(f"  {'─'*52}")
    print(f"  Delta   : {delta:>+10.4f} BTC  /  ${delta_usd:>+14,.0f}")
    print(f"  Volume  : {volume:>10.4f} BTC  /  ${vol_usd:>14,.0f}")
    print(f"  Buyers  : {bar(buy_pct)}  {buy_pct:>5.1f}%")
    print(f"  Sellers : {bar(sell_pct)}  {sell_pct:>5.1f}%")
    print(f"  OI Δ    : {oi_change:>+10.2f} BTC since entry")
    print(f"  Signal  : {signal}")
    print("═"*56)

def print_zone_summary(exit_price: float, z: ZoneAccumulator, oi: float, entry_oi: float):
    oi_change = oi - entry_oi if entry_oi > 0 else 0.0
    delta_usd = z.delta * exit_price
    vol_usd   = z.total_volume * exit_price
    signal    = build_signal(oi_change, z.delta)
    elapsed   = fmt_time(z.time_in_zone)
    direction = "📈 BROKE ABOVE" if z.exit_reason == "ABOVE" else "📉 BROKE BELOW"

    print("\n╔" + "═"*54 + "╗")
    print(f"║  🏁 ZONE COMPLETE                                    ║")
    print(f"║  {direction:<54}║")
    print(f"║  Entry : ${z.entry_price:<10,.2f}   Exit : ${exit_price:<10,.2f}       ║")
    print(f"║  Time in Zone : {elapsed:<38}║")
    print("╠" + "═"*54 + "╣")
    print(f"║  Delta   : {z.delta:>+10.4f} BTC  /  ${delta_usd:>+13,.0f}      ║")
    print(f"║  Volume  : {z.total_volume:>10.4f} BTC  /  ${vol_usd:>13,.0f}      ║")
    print(f"║  Buyers  : {bar(z.buy_pct, 20)}  {z.buy_pct:>5.1f}%              ║")
    print(f"║  Sellers : {bar(z.sell_pct, 20)}  {z.sell_pct:>5.1f}%              ║")
    print(f"║  OI Δ    : {oi_change:>+10.2f} BTC since entry               ║")
    print("╠" + "═"*54 + "╣")
    print(f"║  {signal:<54}║")
    print("╠" + "═"*54 + "╣")
    print(f"║  ⏳ Set new zone:  echo 'LOW HIGH' > zone.txt        ║")
    print("╚" + "═"*54 + "╝\n")

# ─────────────────────────── MAIN ─────────────────────────────

async def main():
    print("\n" + "█"*56)
    print("█  BTC LIQUIDITY ZONE MONITOR                       █")
    print("█  Set zone : echo '96200 96800' > zone.txt         █")
    print("█  Log file : btc_monitor.log                       █")
    print("█"*56 + "\n")

    # Check for existing zone on startup
    result = read_zone_file()
    if result:
        low, high = result
        state.zone.zone_low  = low
        state.zone.zone_high = high
        state.zone_state     = ZoneState.ARMED
        state.last_zone_str  = f"{int(low)} {int(high)}"
        print_armed_banner(low, high)
    else:
        log.info("No zone set. Set with:  echo 'LOW HIGH' > zone.txt")

    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            trade_stream(),
            oi_loop(session),
            price_monitor(session),
            live_printer(),
            zone_watcher(),
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user.")
