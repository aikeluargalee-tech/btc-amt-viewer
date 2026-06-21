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
SIGNAL_LOG  = os.path.expanduser("~/projects/amt-feed/state/amt_signal_log.jsonl")
MAX_LEVELS  = 15

OI_MIN_PCT = 0.05  # Minimum |OI change| before labeling direction (filters noise)

def _oi_direction(feed):
    oi = feed.get("funding", {}).get("oi_change_1h")
    nd = feed.get("footprint", {}).get("net_delta", 0)
    if oi is None or abs(oi) < OI_MIN_PCT:
        return None
    if oi > 0 and nd < 0: return "new shorts entering"
    if oi > 0 and nd > 0: return "new longs entering"
    if oi < 0 and nd < 0: return "shorts covering"
    if oi < 0 and nd > 0: return "longs covering"
    return "flat"

BUCKET_SIZE = 5   # HVN zone grouping: $5 bands
MERGE_GAP   = 10  # Merge adjacent bucket centers within $10

def _hvn_buckets(levels):
    """Group footprint levels into $5 price buckets, merge nearby zones, return top 3."""
    if not levels:
        return []
    buckets = {}
    for l in levels:
        bucket_key = int(l["price"] // BUCKET_SIZE) * BUCKET_SIZE
        if bucket_key not in buckets:
            buckets[bucket_key] = {"total": 0, "center": bucket_key + BUCKET_SIZE // 2}
        buckets[bucket_key]["total"] += l.get("buy", 0) + l.get("sell", 0)

    # Sort by center for adjacency merging
    sorted_buckets = sorted(buckets.values(), key=lambda b: b["center"])

    # Merge adjacent buckets within MERGE_GAP
    merged = []
    for b in sorted_buckets:
        if merged and (b["center"] - merged[-1]["center"]) < MERGE_GAP:
            # Merge into previous: volume-weighted center
            prev = merged[-1]
            total_vol = prev["total"] + b["total"]
            if total_vol > 0:
                prev["center"] = round(
                    (prev["center"] * prev["total"] + b["center"] * b["total"]) / total_vol
                )
            prev["total"] = total_vol
        else:
            merged.append({"center": b["center"], "total": b["total"]})

    top = sorted(merged, key=lambda b: b["total"], reverse=True)[:3]
    return sorted([b["center"] for b in top], reverse=True)

def load_feed():
    if not os.path.exists(FEED_FILE):
        print(f"[deploy] feed not found: {FEED_FILE}")
        sys.exit(1)
    with open(FEED_FILE, "r") as f:
        return json.load(f)

def _last_pivot_from_log():
    """Read the last whale pivot from the AMT signal log (history)."""
    if not os.path.exists(SIGNAL_LOG):
        return None
    last_pivot = None
    try:
        with open(SIGNAL_LOG, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    sig = entry.get("whale_pivot_signal")
                    if sig:
                        last_pivot = {
                            "signal": sig,
                            "direction": sig,
                            "price": entry.get("price"),
                            "timestamp": entry.get("ts"),
                            "conviction": entry.get("whale_pivot_conviction"),
                            "checklist": entry.get("checklist", {}),
                            "balance_state": entry.get("balance_state"),
                            "source": "signal_log"
                        }
                except Exception:
                    pass
    except Exception as e:
        print(f"[deploy] Error reading signal log: {e}")
    return last_pivot

def trim_feed(feed):
    footprint = feed.get("footprint", {})
    balance   = feed.get("balance", {})
    taker     = feed.get("taker_volume", {})
    funding   = feed.get("funding", {})
    basis     = feed.get("basis", {})

    levels = sorted(
        footprint.get("levels", []),
        key=lambda l: l["buy"] + l["sell"],
        reverse=True
    )[:MAX_LEVELS]
    levels = sorted(levels, key=lambda l: l["price"], reverse=True)

    # HVN zones — bucket levels into $5 bands, take highest-volume bucket centers
    hvn_zones = _hvn_buckets(footprint.get("levels", []))

    agg = feed.get("aggression_score", {})

    # Status — uses aggression_score; falls back to net_delta when neutral
    score = agg.get("score", 0.5)
    direction = agg.get("direction", "neutral")
    net_delta = footprint.get("net_delta", 0) or 0

    if direction == "short" and score >= 0.65:
        pressure, side = "HIGH", "seller"
    elif direction == "long" and score <= 0.35:
        pressure, side = "HIGH", "buyer"
    elif direction != "neutral":
        pressure, side = "ELEVATED", ("buyer" if direction == "long" else "seller")
    elif abs(net_delta) > 5:
        pressure, side = "ELEVATED", ("buyer" if net_delta > 0 else "seller")
    else:
        pressure, side = "NEUTRAL", None

    large_prints = footprint.get("large_prints", [])[:3]

    # Taker ratio from candle_delta
    taker_buy = taker.get("buy_volume_24h", 0)
    taker_sell = taker.get("sell_volume_24h", 0)
    taker_total = taker_buy + taker_sell
    taker_ratio = round(taker_buy / taker_total, 3) if taker_total > 0 else None

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price":        feed.get("btc_spot", 0),
        "change_24h_pct": feed.get("change_24h_pct", 0),
        "balance_state": balance.get("state", "UNKNOWN"),

        "status": {
            "pressure": pressure,
            "side":     side,
        },

        "footprint": {
            "candle_start":     footprint.get("candle_start"),
            "trade_count":      footprint.get("trade_count"),
            "levels":           levels,
            "hvn_zones":        hvn_zones,
            "net_delta":        net_delta,
            "delta_flip":       footprint.get("delta_flip"),
            "aggression_price": footprint.get("aggression_price"),
            "absorption_level": footprint.get("absorption_level"),
            "full_candle":      footprint.get("full_candle"),
            "window_seconds":   footprint.get("window_seconds"),
            "large_prints":     large_prints,
        },

        "balance": {
            "poc":         balance.get("poc"),
            "floor_48h":   balance.get("tf_48h", {}).get("floor"),
            "ceiling_48h": balance.get("tf_48h", {}).get("ceiling"),
            "state":       balance.get("state"),
        },

        "cvd": taker.get("session_cvd"),
        "funding_rate": funding.get("rate"),
        "basis": round(basis.get("premium_pct", 0), 4) if basis else None,
        "taker_ratio": taker_ratio,
        "oi_change": funding.get("oi_change_1h"),
        "oi_change_dir": _oi_direction(feed),
        "aggression_score": {
            "score": agg.get("score"),
            "direction": agg.get("direction"),
            "threshold_met": agg.get("threshold_met"),
        },

        # 4-Layer MTF Alignment
        "4layer": {
            "layers": {
                "1D": feed.get("4layer", {}).get("layers", {}).get("1D"),
                "4H": feed.get("4layer", {}).get("layers", {}).get("4H"),
                "1H": feed.get("4layer", {}).get("layers", {}).get("1H"),
                "15m": feed.get("4layer", {}).get("layers", {}).get("15m"),
            },
            "regime": feed.get("4layer", {}).get("regime", {}),
            "alignment": feed.get("4layer", {}).get("alignment", {}),
            "warmup": feed.get("4layer", {}).get("warmup", {}),
            "updated_at": feed.get("4layer", {}).get("updated_at"),
        },

        "whale_pivot": feed.get("whale_pivot", {}),
        "last_whale_pivot": _last_pivot_from_log(),
        "getclaw_brief_ready": feed.get("getclaw_brief_ready", False),
    }

    data["bottomline"] = generate_bottomline(data)
    data["plain_english"] = generate_plain_english(data)
    return data


def generate_plain_english(d):
    """2-3 plain-English sentences for beginners. Zero jargon."""
    price = d.get("price", 0)
    change = d.get("change_24h_pct", 0)
    fp = d.get("footprint", {})
    net = fp.get("net_delta", 0) or 0
    bs = d.get("balance_state", "UNKNOWN")
    st = d.get("status", {})

    # Price direction
    if change > 0.5:
        price_line = f"Bitcoin is at ${price:,.0f}, up {change:.2f}% today."
    elif change < -0.5:
        price_line = f"Bitcoin is at ${price:,.0f}, down {abs(change):.2f}% today."
    else:
        price_line = f"Bitcoin is at ${price:,.0f}, mostly flat today."

    # Who's in control
    abs_net = abs(net)
    oi_dir = d.get("oi_change_dir")
    if st.get("pressure") == "HIGH":
        side_word = "Buyers" if st.get("side") == "buyer" else "Sellers"
        opp_word = "sellers" if st.get("side") == "buyer" else "buyers"
        if abs_net < 50:
            control = f"{side_word} are leading this session — net flow of {abs_net:.1f} BTC toward {side_word.lower()}"
            if oi_dir and "covering" in oi_dir:
                control += f", though {oi_dir}"
            control += "."
        else:
            if st.get("side") == "buyer":
                control = "Buyers are firmly in control this session — large buy orders are moving the price up."
            else:
                control = "Sellers are firmly in control this session — large sell orders are pushing the price down."
    elif st.get("pressure") == "ELEVATED":
        if st.get("side") == "buyer":
            control = f"Buyers are slightly ahead this session, with {abs_net:.1f} more BTC bought than sold in the last 15 minutes."
        else:
            control = f"Sellers are slightly ahead this session, with {abs_net:.1f} more BTC sold than bought in the last 15 minutes."
    elif abs_net > 5:
        if net > 0:
            control = f"Buyers are slightly ahead this session, with {abs_net:.1f} more BTC bought than sold."
        else:
            control = f"Sellers are slightly ahead this session, with {abs_net:.1f} more BTC sold than bought."
    else:
        control = "Buying and selling are roughly balanced — no clear advantage either way."

    # Market state — driven by aggression score when conviction is present
    agg_score = d.get("aggression_score", {}).get("score", 0.5)
    if agg_score >= 0.55 and bs == "TIGHT_BALANCE":
        state_line = "The market is active — sellers are pressing with conviction despite the tight range. Watch whether buyers step in to absorb or sellers continue to push through."
    elif agg_score <= 0.45 and bs == "TIGHT_BALANCE":
        state_line = "The market is active — buyers are pressing with conviction despite the tight range. Watch whether sellers step in to absorb or buyers continue to push through."
    else:
        state_map = {
            "TIGHT_BALANCE": "The market is coiling in a tight range — price is compressing between clear boundaries.",
            "DEVELOPING_BALANCE": "The market is settling into a new range. The boundaries aren't clear yet — still early in the process.",
            "BREAKOUT_UP": "The market is pushing above its recent range. This is a period of price discovery to the upside.",
            "BREAKDOWN": "The market has broken below its recent range. Price is in a period of discovery to the downside.",
            "BREAKOUT": "The market is moving outside its recent range — price is in a period of discovery.",
        }
        state_line = state_map.get(bs, "The market is in a period of price discovery.")

    return f"{price_line} {control} {state_line}"


def _tight_balance_clause(d):
    """Auction clause for TIGHT_BALANCE — driven by aggression when present."""
    agg = d.get("aggression_score", {})
    score = agg.get("score", 0.5)
    if score >= 0.55:
        return " Auction is active — sellers pressing despite the tight range."
    elif score <= 0.45:
        return " Auction is active — buyers pressing despite the tight range."
    return " Auction is tight — price coiling, no clear direction yet."


def generate_bottomline(d):
    fp = d.get("footprint", {})
    st = d.get("status", {})
    net = fp.get("net_delta", 0) or 0
    flip = fp.get("delta_flip")
    levels = fp.get("levels", [])
    funding = d.get("funding_rate")
    bs = d.get("balance_state", "UNKNOWN")
    oi_dir = d.get("oi_change_dir")

    side = "Buyers" if net > 0 else "Sellers"
    opp  = "sellers" if net > 0 else "buyers"

    bs_clause = {
        "BREAKOUT":   " Auction is in breakout — directional move underway.",
        "BREAKDOWN":  " Auction is in breakdown — price has dropped below recent support.",
        "TIGHT_BALANCE": _tight_balance_clause(d),
        "DEVELOPING_BALANCE": " Auction is still developing — range not yet established.",
    }.get(bs, "")

    s1 = f"{side} dominated this candle.{bs_clause}"

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

    if oi_dir and oi_dir not in ("flat", None):
        s2 += f" OI shows {oi_dir}."

    # CVD caveat — flag when candle direction contradicts session flow
    cvd = d.get("cvd")
    if cvd is not None and abs(cvd) > 500:
        if net > 0 and cvd < 0:
            s2 += f" Note: session CVD remains deeply negative ({cvd:,.0f} BTC) — candle buyers have not yet reversed the broader flow."
        elif net < 0 and cvd > 0:
            s2 += f" Note: session CVD remains positive ({cvd:,.0f} BTC) — candle sellers have not yet reversed the broader flow."

    if st.get("pressure") == "HIGH":
        s3 = f"Watch whether {opp} step in on the next candle, or whether {side.lower()} continue to press."
    elif net > 0:
        s3 = "Watch whether buyers can hold these levels on the next candle."
    else:
        s3 = "Watch whether buyers step in on the next candle, or whether selling continues."

    return f"{s1} {s2} {s3}"


def deploy():
    feed = load_feed()
    data = trim_feed(feed)

    if not data["price"]:
        print("[deploy] invalid price — skipping")
        sys.exit(1)

    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

    subprocess.run(["git", "add", "data.json", "deploy.py", "index.html"], check=True, capture_output=True, cwd=REPO_DIR)

    result = subprocess.run(
        ["git", "commit", "--amend", "-m", f"data: {data['generated_at']}"],
        capture_output=True, text=True, cwd=REPO_DIR
    )
    if result.returncode != 0:
        subprocess.run(["git", "commit", "-m", f"data: {data['generated_at']}"], check=True, capture_output=True, cwd=REPO_DIR)

    subprocess.run(["git", "push", "-f", "origin", "main"], check=True, capture_output=True, cwd=REPO_DIR)
    # NOTE: force-push is intentional for deploy. Risky if multiple people push — use caution.

    print(f"[deploy] pushed — ${data['price']:,.0f} | {data['status']['pressure']} | {len(data['footprint']['levels'])} levels | {data.get('balance_state','?')}")

if __name__ == "__main__":
    deploy()
