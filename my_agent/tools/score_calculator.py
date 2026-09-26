"""ArcBench score calculator.

The leaderboard formula, transcribed from the platform's "Leaderboard scoring" modal
and verified against three live leaderboard rows (see SCORING_AND_OPTIMIZATION.md):

    E     = 0.4 * N * p / 100          expected cost, CNY
    r     = clamp(b / E, 0.01, 100)    cost ratio, b floored at CNY 0.10
    gamma = 0.1 if b <= E else 0.2
    S     = p / r ** gamma

Ranking tie-breaks: score, then pass rate, then lower cost, then earlier submission.
A submission must complete EVERY task with valid CNY cost data to be ranked.

Usage:
    python tools/score_calculator.py --pass-rate 50.5 --cost 16.39 --tests 200
    python tools/score_calculator.py --tables
    python tools/score_calculator.py --target-score 60 --tests 200 --pass-rate 55
"""

from __future__ import annotations

import argparse
import sys

COST_FLOOR = 0.10
RATIO_MIN = 0.01
RATIO_MAX = 100.0
GAMMA_REWARD = 0.1
GAMMA_PENALTY = 0.2


def evaluate(pass_rate: float, cost: float, tests: float) -> dict:
    """Score one (or one aggregated) run."""

    if tests <= 0:
        raise ValueError("tests must be positive")
    expected = 0.4 * tests * pass_rate / 100.0
    floored = max(cost, COST_FLOOR)
    raw_ratio = floored / expected if expected > 0 else RATIO_MAX
    ratio = max(RATIO_MIN, min(RATIO_MAX, raw_ratio))
    reward = cost <= expected
    gamma = GAMMA_REWARD if reward else GAMMA_PENALTY
    return {
        "pass_rate": pass_rate,
        "cost": cost,
        "tests": tests,
        "expected_cost": expected,
        "raw_ratio": raw_ratio,
        "ratio": ratio,
        "clamped": raw_ratio < RATIO_MIN or raw_ratio > RATIO_MAX,
        "regime": "reward" if reward else "penalty",
        "gamma": gamma,
        "score": pass_rate / (ratio ** gamma),
    }


def show(result: dict) -> None:
    print(f"  pass rate      : {result['pass_rate']:.2f}%")
    print(f"  cost           : CNY {result['cost']:.4f}")
    print(f"  tests (N)      : {result['tests']:.0f}")
    print(f"  expected cost  : CNY {result['expected_cost']:.4f}")
    print(f"  cost ratio     : {result['ratio']:.6f}"
          + ("  (clamped)" if result["clamped"] else ""))
    print(f"  regime         : {result['regime']}  (gamma={result['gamma']})")
    print(f"  SCORE          : {result['score']:.4f}")


def tables() -> None:
    n = 200
    p = 50.0
    base = evaluate(p, 0.4 * n * p / 100.0, n)["score"]
    print(f"=== cost sensitivity (N={n}, pass rate {p}%) ===")
    print(f"  expected cost = CNY {0.4 * n * p / 100:.2f}\n")
    print("  cost(CNY)   ratio     regime     score     vs ratio=1")
    for cost in (0.4, 4, 16, 24, 40, 60, 80, 200, 400):
        r = evaluate(p, cost, n)
        print(f"  {cost:<11} {r['ratio']:<9.4f} {r['regime']:<10} {r['score']:<9.2f} "
              f"{(r['score'] / base - 1) * 100:+.1f}%")

    print()
    print(f"=== pass-rate sensitivity (N={n}, cost CNY 16) ===")
    anchor = evaluate(50.0, 16.0, n)["score"]
    print("  pass%    expected cost   ratio     score     vs p=50")
    for p in (30, 40, 50, 60, 80, 100):
        r = evaluate(p, 16.0, n)
        print(f"  {p:<8} {r['expected_cost']:<15.2f} {r['ratio']:<9.4f} {r['score']:<9.2f} "
              f"{(r['score'] / anchor - 1) * 100:+.1f}%")

    print()
    print("=== expected-cost bar per scenario ===")
    print("  scenario                          N     E(p=50%)   E(p=100%)")
    for label, tests in (
        ("hackathon overall (both tasks)", 200),
        ("hackathon single task", 100),
        ("ticketbooking (playground)", 30),
        ("arc-bench-lite bookstack", 34),
    ):
        print(f"  {label:<33} {tests:<6} {0.4 * tests * 0.5:<10.2f} {0.4 * tests * 1.0:.2f}")

    print()
    print(f"  theoretical maximum (p=100, ratio at the {RATIO_MIN} floor): "
          f"{evaluate(100.0, 0.0, n)['score']:.4f}")


def target(goal: float, tests: float, pass_rate: float) -> None:
    """What is the most you may spend and still hit `goal`?"""
    expected = 0.4 * tests * pass_rate / 100.0
    print(f"=== budget for score >= {goal} (N={tests}, p={pass_rate}%) ===")
    print(f"  expected cost = CNY {expected:.4f}")

    reward_max = expected
    reward_score = evaluate(pass_rate, reward_max, tests)["score"]
    if reward_score >= goal:
        print(f"  at the expected-cost line the score is {reward_score:.2f}, already >= {goal}")
    else:
        # in the penalty regime: S = p / r^0.2  =>  r = (p/S)^(1/0.2)
        r = (pass_rate / goal) ** (1 / GAMMA_PENALTY)
        print(f"  reward regime cannot reach {goal} (needs p >= {goal} for that),")
        print(f"  so you must land in the penalty regime with ratio <= {r:.4f}")
        print(f"  -> max spend CNY {r * expected:.4f}")

    ratio_at_goal = (pass_rate / goal) ** (1 / GAMMA_REWARD)
    if ratio_at_goal >= RATIO_MIN:
        print(f"  hitting {goal} inside the reward regime needs ratio <= {ratio_at_goal:.4f}"
              f"  -> CNY {ratio_at_goal * expected:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pass-rate", type=float)
    parser.add_argument("--cost", type=float)
    parser.add_argument("--tests", type=float)
    parser.add_argument("--tables", action="store_true")
    parser.add_argument("--target-score", type=float)
    args = parser.parse_args()

    if args.tables:
        tables()
        return 0

    if args.pass_rate is not None and args.cost is not None and args.tests is not None:
        print("=== score ===")
        show(evaluate(args.pass_rate, args.cost, args.tests))
        return 0

    if args.target_score is not None:
        tests = args.tests if args.tests is not None else 200.0
        pass_rate = args.pass_rate if args.pass_rate is not None else 50.0
        target(args.target_score, tests, pass_rate)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
