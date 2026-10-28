"""Run the swarm and the baseline on the same seed and compare scores.

    python runner.py --seed 42 --ticks 90
    python runner.py --seeds 20          # average over many seeds
"""
from __future__ import annotations

import argparse
from typing import Callable

from agents import Baseline, Swarm
from events import EventLog
from sim import Facility


def make_run(mode: str, seed: int, log: EventLog | None = None):
    log = log or EventLog()
    run_id = f"{mode}-seed{seed}"
    fac = Facility(seed=seed, log=log, run_id=run_id)
    brain = Swarm(log, run_id) if mode == "swarm" else Baseline(log, run_id)
    return fac, brain, log


def run(mode: str, seed: int, ticks: int = 90,
        on_tick: Callable[[Facility], None] | None = None) -> dict:
    fac, brain, _ = make_run(mode, seed)
    for _ in range(ticks):
        fac.step()
        brain.act(fac)
        if on_tick:
            on_tick(fac)
    return fac.score()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seeds", type=int, default=0, help="average over N seeds")
    ap.add_argument("--ticks", type=int, default=90)
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1)) if args.seeds else [args.seed]
    keys = ["containment_speed", "false_alarm_rate", "civilians_saved",
            "civilians_lost", "anomalies", "contained", "actions"]
    totals = {m: dict.fromkeys(keys, 0.0) for m in ("swarm", "baseline")}
    for s in seeds:
        for m in totals:
            sc = run(m, s, args.ticks)
            for k in keys:
                totals[m][k] += sc[k] / len(seeds)
    label = f"seed {seeds[0]}" if len(seeds) == 1 else f"average of {len(seeds)} seeds"
    print(f"\n{'metric':<20}{'swarm':>10}{'baseline':>10}   ({label}, {args.ticks} ticks)")
    for k in keys:
        print(f"{k:<20}{totals['swarm'][k]:>10.2f}{totals['baseline'][k]:>10.2f}")


if __name__ == "__main__":
    main()
