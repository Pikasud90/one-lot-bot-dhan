"""The desk's trade-management rule (owner, 2026-11-23): cut the loss when wrong, ride the
trend when right. Pure functions on prices: no I/O, no pandas, importable on its own file.

A plan is fixed at entry: a side, an entry, a first stop and a first target. The distance
from the entry to the first target is the PLANNED MOVE. As the best price since entry covers
shares of that move the stop is raised (it never goes back) and, late in the move, the target
is pushed out:

    progress >= 25%   stop above breakeven: entry + costs + 5% of the planned move
    progress >= 50%   stop locks 35% of the planned move
    progress >= 80%   stop follows the best price at 20% of the planned move, and the target
                      moves out by 50% of the planned move each time price comes within 20%
                      of it (`extend=False`: the target stays where it is)

25%, 35% at 50% and 80% are the owner's numbers. The 5% above breakeven, the trail distance,
the extension and `min_gap` are the lead's choices; docs/research/RATCHET_STUDY_2026-11-23.md
tests them.

For an index option the managed price is the INDEX in the trade's direction (a call is long
the index, a put is short it); the fill and the profit are in premium. `cost` is then the
round-trip cost per unit of premium divided by the option's delta.

How a caller uses it, bar by bar (one-minute bars where it has them):

    state = start(plan)
    for bar in bars_after_the_fill:
        hit = exit_on_bar(plan, state, bar.open, bar.high, bar.low)
        if hit: ...close at hit[1] for the reason hit[0]...
        state, events = advance(plan, state, bar.high, bar.low, params)

So a level raised by a bar holds from the NEXT bar on, and a bar that touches both the stop
and the target counts as the stop.
"""

from __future__ import annotations

from dataclasses import dataclass

#: a price exactly on a level counts as reaching it, float noise or not
_EPS = 1e-9
STAGES = ("FIRST_STOP", "BREAKEVEN", "LOCKED", "TRAILING")
#: what a stop-out is called at each stage
STOP_LABELS = ("STOP", "BREAKEVEN_STOP", "LOCK_STOP", "TRAIL_STOP")


@dataclass(frozen=True)
class RatchetParams:
    """Shares of the planned move. See the module text for who chose which."""

    be_at: float = 0.25
    be_lock: float = 0.05
    lock_at: float = 0.50
    lock: float = 0.35
    extend_at: float = 0.80
    trail_gap: float = 0.20
    extend: bool = True
    extend_by: float = 0.50
    #: a raised stop is never set nearer to the best price than this share of the move
    min_gap: float = 0.10

    def __post_init__(self) -> None:
        if not 0 < self.be_at < self.lock_at < self.extend_at < 1:
            raise ValueError("need 0 < be_at < lock_at < extend_at < 1")
        if not 0 <= self.be_lock <= self.lock < self.lock_at:
            raise ValueError("need 0 <= be_lock <= lock < lock_at")
        if not 0 < self.min_gap <= self.trail_gap < self.extend_at:
            raise ValueError("need 0 < min_gap <= trail_gap < extend_at")
        if self.extend_by <= 0:
            raise ValueError("extend_by must be positive")


@dataclass(frozen=True)
class Plan:
    """What is fixed at entry. `side` is +1 when a rising managed price is a gain, -1 when a
    falling one is. `cost` is the round-trip cost per unit in the managed price's units."""

    side: int
    entry: float
    stop: float
    target: float
    cost: float = 0.0

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError("side must be +1 or -1")
        if not self.side * (self.entry - self.stop) > 0:
            raise ValueError("the first stop must be on the losing side of the entry")
        if not self.side * (self.target - self.entry) > 0:
            raise ValueError("the first target must be on the winning side of the entry")
        if self.cost < 0:
            raise ValueError("cost cannot be negative")

    @property
    def move(self) -> float:
        """The planned move: entry to the first target."""
        return abs(self.target - self.entry)

    @property
    def risk(self) -> float:
        """Entry to the first stop."""
        return abs(self.entry - self.stop)


def plan_from_risk(
    side: int, entry: float, risk: float, target_r: float, cost: float = 0.0
) -> Plan:
    """A plan whose first stop is `risk` away and whose first target is `target_r` risks away."""
    if risk <= 0 or target_r <= 0:
        raise ValueError("risk and target_r must be positive")
    return Plan(side, entry, entry - side * risk, entry + side * target_r * risk, cost)


@dataclass(frozen=True)
class State:
    """The levels in force for the coming bar, the best price so far and the stage reached."""

    stop: float
    target: float
    best: float
    stage: int = 0


def start(plan: Plan) -> State:
    return State(plan.stop, plan.target, plan.entry, 0)


def progress(plan: Plan, best: float) -> float:
    """The share of the planned move the best price has covered (negative: none yet)."""
    return plan.side * (best - plan.entry) / plan.move


def stop_label(state: State) -> str:
    return STOP_LABELS[state.stage]


def exit_on_bar(
    plan: Plan, state: State, open_: float, high: float, low: float
) -> tuple[str, float] | None:
    """The exit a bar forces with the levels in `state`: (reason, price before slippage), or
    None. A bar that opens beyond a level fills at its open (a gap). The stop is checked
    first, so a bar touching both levels is a stop."""
    s = plan.side
    adverse, favourable = (low, high) if s == 1 else (high, low)
    if s * (open_ - state.stop) <= 0:
        return f"GAP_{stop_label(state)}", open_
    if s * (adverse - state.stop) <= 0:
        return stop_label(state), state.stop
    if s * (open_ - state.target) >= 0:
        return "GAP_TARGET", open_
    if s * (favourable - state.target) >= 0:
        return "TARGET", state.target
    return None


def advance(
    plan: Plan, state: State, high: float, low: float, p: RatchetParams | None = None
) -> tuple[State, tuple[str, ...]]:
    """Fold one finished bar into the state. Returns the state for the NEXT bar and what
    changed: BREAKEVEN, LOCKED, TRAILING (a stage reached), STOP_MOVED, TARGET_MOVED."""
    p = p or RatchetParams()
    s, move = plan.side, plan.move
    best = max(state.best, high) if s == 1 else min(state.best, low)
    prog = s * (best - plan.entry) / move

    # the stop, as a gain over the entry in the trade's favour (negative: still a loss)
    held = s * (state.stop - plan.entry)
    want: float | None = None
    if prog >= p.be_at - _EPS:
        want = plan.cost + p.be_lock * move
    if prog >= p.lock_at - _EPS:
        want = max(want or 0.0, p.lock * move)
    if prog >= p.extend_at - _EPS:
        want = max(want or 0.0, (prog - p.trail_gap) * move)
    if want is not None:
        want = min(want, (prog - p.min_gap) * move)
        held = max(held, want)
    stop = plan.entry + s * held

    reach = s * (state.target - plan.entry) / move  # the target as a share of the move
    if p.extend:
        near = 1.0 - p.extend_at
        while prog >= reach - near - _EPS:
            reach += p.extend_by
    target = (
        plan.entry + s * reach * move
        if reach * move != s * (state.target - plan.entry)
        else state.target
    )

    stage = (
        3
        if prog >= p.extend_at - _EPS
        else 2
        if prog >= p.lock_at - _EPS
        else 1
        if prog >= p.be_at - _EPS
        else 0
    )
    stage = max(state.stage, stage)
    events = [("BREAKEVEN", "LOCKED", "TRAILING")[k - 1] for k in range(state.stage + 1, stage + 1)]
    if stop != state.stop:
        events.append("STOP_MOVED")
    if target != state.target:
        events.append("TARGET_MOVED")
    return State(stop, target, best, stage), tuple(events)
