"""Before/after route comparison across two graph builds.

Usage:  ./.venv/bin/python tools/compare_graphs.py OLD.pkl NEW.pkl

Routes a fixed sample of Bangkok landmark pairs at three trade-off points
(fastest, balanced, safest) through both graphs and prints the deltas, so the
effect of a safety-model change is visible rather than assumed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from router import Router, load_graph  # noqa: E402

LAMBDAS = (0.0, 1.6, 6.0)

# (label, lat, lon) — spread across the city, deliberately including trips
# that cross big arterials where the junction model should bite.
PLACES = [
    ("Siam Square", 13.7455, 100.5331),
    ("Chatuchak", 13.7999, 100.5503),
    ("Khao San", 13.7590, 100.4975),
    ("Silom", 13.7246, 100.5339),
    ("Bang Na", 13.6680, 100.6047),
    ("Rama IX", 13.7580, 100.6430),
    ("Thonburi", 13.7250, 100.4860),
    ("Lat Phrao", 13.8160, 100.5610),
    ("On Nut", 13.7055, 100.6010),
    ("Nonthaburi", 13.8590, 100.5210),
]

# Origin/destination pairs by index into PLACES.
PAIRS = [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9), (1, 8), (0, 6), (3, 5)]


def metrics(router, o, d, lam):
    res = router.plan(o[1], o[2], d[1], d[2], lambdas=[lam])
    routes = res.get("routes") or []
    if not routes:
        return None
    r = routes[0]
    return {
        "time_min": r["time_s"] / 60.0,
        "dist_km": r["distance_m"] / 1000.0,
        "avg_lts": r["avg_lts"],
        "max_lts": r["max_lts"],
        "junc": r.get("junction_count", 0),
        "comfort": r["comfort_pct"],
    }


def main():
    old_path, new_path = sys.argv[1], sys.argv[2]
    old = Router(load_graph(old_path))
    new = Router(load_graph(new_path))

    header = (f"{'pair':<22} {'lam':>4} | {'time(min)':>14} {'dist(km)':>14} "
              f"{'avgLTS':>13} {'maxLTS':>11} {'junc':>11} {'comfort%':>15}")
    print(header)
    print("-" * len(header))

    fails = []
    for oi, di in PAIRS:
        o, d = PLACES[oi], PLACES[di]
        name = f"{o[0]}->{d[0]}"
        for lam in LAMBDAS:
            a = metrics(old, o, d, lam)
            b = metrics(new, o, d, lam)
            if a is None or b is None:
                fails.append(f"{name} lam={lam}: old={a is not None} new={b is not None}")
                print(f"{name:<22} {lam:>4} | UNROUTABLE old={a is not None} new={b is not None}")
                continue
            print(
                f"{name:<22} {lam:>4} | "
                f"{a['time_min']:6.1f}->{b['time_min']:<7.1f}"
                f"{a['dist_km']:6.2f}->{b['dist_km']:<7.2f}"
                f"{a['avg_lts']:6.2f}->{b['avg_lts']:<6.2f}"
                f"{a['max_lts']:4.1f}->{b['max_lts']:<6.1f}"
                f"{a['junc']:4d}->{b['junc']:<6d}"
                f"{a['comfort']:6.1f}->{b['comfort']:<6.1f}"
            )
    print("-" * len(header))
    print(f"unroutable: {len(fails)}")
    for f in fails:
        print("  !", f)


if __name__ == "__main__":
    main()
