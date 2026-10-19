"""Steady HTTP load on autoimmune-app for detector demos.

    python tools/load.py --seconds 330 --rps 20

Request mix (weights): order list, totals, discounts, searches by name, and --no-customer-share of searches
without a customer (a buggy client). Healthy, those get 400; with F01 injected they get 500.
Prints a status summary every 30 s and at the end.
"""
import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from pipeline.observe import base_url, load_config  # noqa: E402

MIX = [("/api/orders", 3), ("/api/orders/1/total", 2), ("/api/orders/2/total", 2), ("/api/orders/3/total", 1),
       ("/api/orders/1/discount", 2), ("/api/orders/2/discount", 2), ("/api/orders/3/discount", 1),
       ("/api/orders/search?customer=alice", 2), ("/api/orders/search?customer=bob", 1)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--rps", type=float, default=20)
    ap.add_argument("--no-customer-share", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    url = base_url(load_config())
    paths, weights = zip(*MIX)
    session = requests.Session()
    counts: Counter = Counter()
    start = last_report = time.monotonic()
    i = 0
    while time.monotonic() - start < args.seconds:
        path = "/api/orders/search" if rng.random() < args.no_customer_share else rng.choices(paths, weights)[0]
        try:
            counts[session.get(url + path, timeout=5).status_code] += 1
        except requests.RequestException as e:
            counts[type(e).__name__] += 1
        i += 1
        time.sleep(max(0.0, start + i / args.rps - time.monotonic()))
        if time.monotonic() - last_report >= 30:
            last_report = time.monotonic()
            print(f"load t={last_report - start:.0f}s sent={i} {dict(sorted(counts.items(), key=str))}", flush=True)
    print(f"load done: {i} requests in {time.monotonic() - start:.0f}s {dict(sorted(counts.items(), key=str))}",
          flush=True)


if __name__ == "__main__":
    main()
