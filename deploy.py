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

    data = {
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

    data["bottomline"] = generate_bottomline(data)
    return data


def generate_bottomline(d):
    """Observational summary — describes what happened, never prescribes."""
    fp = d.get("footprint", {})
    st = d.get("status", {})
    net = fp.get("net_delta", 0) or 0
    flip = fp.get("delta_flip")
    levels = fp.get("levels", [])
    funding = d.get("funding_rate")

    # Sentence 1 — who controlled
    if st.get("pressure") in ("HIGH", "ELEVATED") and st.get("side"):
        who = "Sellers" if st["side"] == "seller" else "Buyers"
        s1 = f"{who} dominated this candle."
    elif net > 0:
        s1 = "Buyers had the edge this candle."
    elif net < 0:
        s1 = "Sellers had the edge this candle."
    else:
        s1 = "Buyers and sellers were evenly matched this candle."

    # Sentence 2 — evidence
    abs_net = abs(net)
    direction_word = "buyers" if net > 0 else "sellers"
    s2 = f"Net flow was {abs_net:.1f} BTC toward {direction_word}."

    if levels:
        buyer_levels = sum(1 for l in levels if l.get("buy", 0) > l.get("sell", 0))
        total = len(levels)
        if buyer_levels > total * 0.7:
            s2 += " Most price levels showed buyer dominance."
        elif buyer_levels < total * 0.3:
            s2 += " Most price levels showed seller dominance."
        else:
            s2 += " Price levels were mixed."

    if flip is True:
        s2 += " Delta flipped bullish at candle close."
    elif flip is False:
        s2 += " Delta flipped bearish at candle close."

    if funding is not None:
        if funding > 0.0005:
            s2 += f" Funding is elevated ({(funding*100):.4f}%), suggesting longs are crowded."
        elif funding < -0.0005:
            s2 += f" Funding is negative ({(funding*100):.4f}%), suggesting shorts are crowded."

    # Sentence 3 — what to watch
    if st.get("pressure") == "HIGH":
        if st.get("side") == "seller":
            s3 = "Watch whether buyers step in on the next candle to defend current levels, or whether sellers continue to press."
        else:
            s3 = "Watch whether buyers maintain control on the next candle, or whether sellers push back."
    elif net > 0:
        s3 = "Watch whether buyers can hold these levels on the next candle."
    else:
        s3 = "Watch whether buyers step in on the next candle, or whether selling continues."

    return f"{s1} {s2} {s3}"


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
