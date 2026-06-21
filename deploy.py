"""
deploy.py
Reads /tmp/amt_feed.json, writes data.json into btc-amt-viewer repo,
commits (amend), and force-pushes to GitHub Pages.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

FEED_FILE   = "/tmp/amt_feed.json"
REPO_DIR    = os.path.expanduser("~/projects/btc-amt-viewer")
DATA_FILE   = os.path.join(REPO_DIR, "data.json")
MAX_LEVELS  = 15

def load_feed():
    if not os.path.exists(FEED_FILE):
        print(f"[deploy] feed not found: {FEED_FILE}")
        sys.exit(1)
    with open(FEED_FILE, "r") as f:
        return json.load(f)

def trim_feed(feed):
    footprint = feed.get("footprint", {})
    balance   = feed.get("balance", {})

    levels = sorted(
        footprint.get("levels", []),
        key=lambda l: l["buy"] + l["sell"],
        reverse=True
    )[:MAX_LEVELS]
    levels = sorted(levels, key=lambda l: l["price"], reverse=True)

    agg = feed.get("aggression_score", {})

    # Status
    score = agg.get("score", 0.5)
    direction = agg.get("direction", "neutral")
    if direction == "short" and score >= 0.65:
        pressure, side = "HIGH", "seller"
    elif direction == "long" and score <= 0.35:
        pressure, side = "HIGH", "buyer"
    elif direction != "neutral":
        pressure, side = "ELEVATED", ("buyer" if direction == "long" else "seller")
    else:
        pressure, side = "NEUTRAL", None

    large_prints = footprint.get("large_prints", [])[:3]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price":        feed.get("btc_spot", 0),
        "change_24h_pct": feed.get("change_24h_pct", 0),

        "status": {
            "pressure": pressure,
            "side":     side,
        },

        "footprint": {
            "candle_start":     footprint.get("candle_start"),
            "trade_count":      footprint.get("trade_count"),
            "levels":           levels,
            "net_delta":        footprint.get("net_delta"),
            "delta_flip":       footprint.get("delta_flip"),
            "aggression_price": footprint.get("aggression_price"),
            "absorption_level": footprint.get("absorption_level"),
            "full_candle":      footprint.get("full_candle"),
            "window_seconds":   footprint.get("window_seconds"),
            "large_prints":     large_prints,
        },

        "balance": {
            "poc":        balance.get("poc"),
            "floor_48h":  balance.get("tf_48h", {}).get("floor"),
            "ceiling_48h": balance.get("tf_48h", {}).get("ceiling"),
            "state":      balance.get("state"),
        },

        "cvd": feed.get("taker_volume", {}).get("session_cvd"),
        "funding_rate": feed.get("funding", {}).get("rate"),
    }

def deploy():
    feed = load_feed()
    data = trim_feed(feed)

    # Validate minimum
    if not data["price"]:
        print("[deploy] invalid price — skipping")
        sys.exit(1)

    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

    os.chdir(REPO_DIR)

    subprocess.run(["git", "add", "data.json"], check=True, capture_output=True)

    # Amend commit if there's one, or create initial
    result = subprocess.run(
        ["git", "commit", "--amend", "-m", f"data: {data['generated_at']}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # No prior commit — create one
        subprocess.run(
            ["git", "commit", "-m", f"data: {data['generated_at']}"],
            check=True, capture_output=True
        )

    # Force push (amend requires force)
    subprocess.run(
        ["git", "push", "-f", "origin", "main"],
        check=True, capture_output=True
    )

    price = data["price"]
    status = data["status"]["pressure"]
    print(f"[deploy] pushed — ${price:,.0f} | {status} | {len(data['footprint']['levels'])} levels")

if __name__ == "__main__":
    deploy()
