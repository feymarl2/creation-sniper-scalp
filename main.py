#!/usr/bin/env python3
"""Entry for make.fun bounty u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.

A creation sniper for the pump.fun replay. It buys a small fixed stake
in a coin's first slots when the curve already holds at least 0.3 SOL
of real reserves, few distinct buyers have traded, and the creator
has not sold. It exits the whole position at 15% profit, at a 12%
loss, after ten slots, or when the creator sells, whichever comes
first. Sizes respect the per-coin cap and free cash after tx fees.
No clock, no randomness: identical events give identical orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot

STAKE = makefun_bot.LAMPORTS_PER_SOL // 10
MIN_REAL = 300_000_000
MAX_AGE = 3
MAX_BUYERS = 3
TAKE = 1.15
STOP = 0.88
MAX_HOLD = 10
BUY_SLIPPAGE_BPS = 10_000
SELL_SLIPPAGE_BPS = 10_000


class Coin:
    __slots__ = ("created", "creator", "buyers", "dev_sold", "bought_slot", "done")

    def __init__(self, slot, creator):
        self.created = slot
        self.creator = creator
        self.buyers = set()
        self.dev_sold = False
        self.bought_slot = None
        self.done = False


coins = {}


def price_of(curve):
    if curve.virtual_token_reserves <= 0:
        return None
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


def on_batch(state, batch):
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    for e in batch["events"]:
        mint = e["mint"]
        kind = e["event"]
        coin = coins.get(mint)
        if kind == "CreateEvent":
            if coin is None:
                coins[mint] = Coin(e["slot"], e.get("creator") or e.get("user"))
            continue
        if coin is None:
            curve = state.curves.get(mint)
            created = curve.created_slot if curve is not None and curve.created_slot is not None else e["slot"]
            coin = Coin(created, None)
            coins[mint] = coin
        if kind == "TradeEvent":
            user = e.get("user")
            if e.get("is_buy"):
                if user is not None:
                    coin.buyers.add(user)
                    if coin.creator is None:
                        coin.creator = user
            elif user is not None and coin.creator is not None and user == coin.creator:
                coin.dev_sold = True
    orders = []
    # exits: fast scalp out
    for mint in sorted(state.positions):
        coin = coins.get(mint)
        curve = state.curves.get(mint)
        if coin is None or curve is None or curve.complete:
            continue
        price = price_of(curve)
        if price is None:
            continue
        if coin.bought_slot is None:
            coin.bought_slot = state.slot
        held = state.positions[mint]
        ratio = held.token_amount * price / held.cost_lamports if held.cost_lamports > 0 else 1.0
        if ratio >= TAKE or ratio <= STOP or coin.dev_sold or state.slot - coin.bought_slot > MAX_HOLD:
            orders.append({"side": "sell", "mint": mint, "token_amount": held.token_amount,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            coin.done = True
    orders = orders[: caps["max_orders_per_batch"]]
    # entries: snipe creations
    free = state.free_cash_lamports()
    slots = caps["max_open_positions"] - state.committed_position_count()
    for e in batch["events"]:
        if len(orders) >= caps["max_orders_per_batch"] or slots <= 0:
            break
        mint = e["mint"]
        coin = coins.get(mint)
        curve = state.curves.get(mint)
        if coin is None or curve is None or curve.complete or coin.done:
            continue
        if mint in state.positions or coin.bought_slot is not None:
            continue
        if state.slot - coin.created > MAX_AGE:
            coin.done = True
            continue
        if coin.dev_sold or len(coin.buyers) > MAX_BUYERS:
            continue
        if curve.real_quote_reserves < MIN_REAL:
            continue
        amount = min(STAKE, state.max_buy_lamports(mint))
        if amount <= 0 or free < amount + tx:
            continue
        orders.append({"side": "buy", "mint": mint, "quote_lamports": amount,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        free -= amount + tx
        slots -= 1
        coin.bought_slot = state.slot
    return orders


if __name__ == "__main__":
    try:
        makefun_bot.run(on_batch)
    except makefun_bot.ProtocolError as exc:
        if "before hello" not in str(exc):
            raise
