"""Approximate proxies for proprietary data feeds.

These replace Dipsea Capital's Flight Check, SAM, NYSE TICK, HIRO, and Vol Trace
with public-data approximations. Designed to be swapped for real feeds later.

Ported unchanged from services/alert-engine/src/evaluators/proxies.py.
"""

import logging

logger = logging.getLogger(__name__)


def proxy_flight_check(candles: list[dict]) -> float:
    """Proxy for Flight Check (-12 to +12).
    Composite of RSI position, MACD histogram, price vs SMA(20), ADX trend."""
    if not candles:
        return 0.0
    latest = candles[-1]
    score = 0.0

    # RSI position vs 50 (+/-3)
    rsi = latest.get("rsi_14", 50)
    if rsi is not None:
        score += max(-3, min(3, (rsi - 50) / 10))

    # MACD histogram (+/-3)
    macd_h = latest.get("macd_hist", 0)
    if macd_h is not None:
        score += max(-3, min(3, macd_h * 3))

    # Price vs SMA(20) (+/-3)
    close = latest.get("close")
    sma20 = latest.get("sma_20")
    if close and sma20 and sma20 > 0:
        pct = (close - sma20) / sma20 * 100
        score += max(-3, min(3, pct))

    # ADX trend direction (+/-3)
    adx = latest.get("adx_14")
    dmp = latest.get("dmp_14")
    dmn = latest.get("dmn_14")
    if adx and dmp and dmn and adx > 20:
        direction = 1 if dmp > dmn else -1
        score += direction * min(3, adx / 15)

    return round(max(-12, min(12, score)), 1)


def proxy_sam(market_state: dict | None, candles: list[dict]) -> float:
    """Proxy for SAM (-12 to +12).
    Composite of VIX level, market regime, OBV trend."""
    score = 0.0

    # VIX level (+/-4)
    vix = market_state.get("vix", 20) if market_state else 20
    if vix is not None:
        if vix < 15:
            score += 4
        elif vix < 20:
            score += 2
        elif vix < 25:
            score -= 1
        elif vix < 30:
            score -= 3
        else:
            score -= 4

    # Market regime (+/-4)
    regime = market_state.get("market_regime", "normal") if market_state else "normal"
    regime_map = {"low_vol": 4, "normal": 2, "elevated": -2, "high_vol": -4}
    score += regime_map.get(regime, 0)

    # OBV trend (+/-4)
    obv_vals = [c.get("obv") for c in candles[-20:] if c.get("obv") is not None]
    if len(obv_vals) >= 10:
        old = obv_vals[-10]
        new = obv_vals[-1]
        if old and old != 0:
            obv_slope = (new - old) / abs(old)
            score += max(-4, min(4, obv_slope * 100))

    return round(max(-12, min(12, score)), 1)


def proxy_tick_structure(spy_1m_candles: list[dict]) -> dict | None:
    """Proxy for NYSE TICK test structure using SPY 1-min volume-weighted momentum.
    Finds two extreme readings 4-12 bars apart forming HL (bull) or LH (bear)."""
    if not spy_1m_candles or len(spy_1m_candles) < 15:
        return None

    # Calculate volume-weighted momentum for each bar
    volumes = [c.get("volume", 0) or 0 for c in spy_1m_candles]
    avg_vol = sum(volumes[-20:]) / max(len(volumes[-20:]), 1) if volumes else 1
    if avg_vol == 0:
        avg_vol = 1

    extremes = []
    for i, c in enumerate(spy_1m_candles):
        if c.get("close") is None or c.get("open") is None:
            continue
        mom = c["close"] - c["open"]
        vol_weight = (c.get("volume", 0) or 0) / avg_vol
        weighted_mom = mom * max(vol_weight, 0.1)
        atr = c.get("atr_14")
        threshold = atr * 1.5 if atr and atr > 0 else abs(mom) * 2
        if abs(weighted_mom) > threshold and threshold > 0:
            extremes.append({"idx": i, "value": weighted_mom, "time": c.get("time")})

    # Look for patterns
    for i in range(len(extremes)):
        for j in range(i + 1, len(extremes)):
            gap = extremes[j]["idx"] - extremes[i]["idx"]
            if not (4 <= gap <= 12):
                continue
            v1, v2 = extremes[i]["value"], extremes[j]["value"]
            if v1 < 0 and v2 < 0 and v2 > v1:
                return {"direction": "BULL", "pattern": "HL", "gap": gap,
                        "test1": v1, "test2": v2,
                        "test1_idx": extremes[i]["idx"], "test2_idx": extremes[j]["idx"]}
            if v1 > 0 and v2 > 0 and v2 < v1:
                return {"direction": "BEAR", "pattern": "LH", "gap": gap,
                        "test1": v1, "test2": v2,
                        "test1_idx": extremes[i]["idx"], "test2_idx": extremes[j]["idx"]}
    return None


def proxy_tick_sma_slope(spy_5m_candles: list[dict]) -> float | None:
    """Proxy for TICK SMA(20) slope on 5-min. Uses close-to-close momentum."""
    if not spy_5m_candles or len(spy_5m_candles) < 25:
        return None
    mom_values = []
    for c in spy_5m_candles:
        if c.get("close") is not None and c.get("open") is not None:
            mom_values.append(c["close"] - c["open"])
        else:
            mom_values.append(0)
    if len(mom_values) < 25:
        return None
    # SMA(20) of momentum
    sma20 = [sum(mom_values[i - 20:i]) / 20 for i in range(20, len(mom_values))]
    if len(sma20) < 2:
        return None
    return sma20[-1] - sma20[-2]  # slope = change in SMA


def proxy_hiro_slope(spy_1m_candles: list[dict]) -> float | None:
    """Proxy for HIRO SMA(20) slope on 1-min. Uses OBV as flow proxy."""
    obv_vals = [c.get("obv") for c in spy_1m_candles if c.get("obv") is not None]
    if len(obv_vals) < 25:
        return None
    sma20 = [sum(obv_vals[i - 20:i]) / 20 for i in range(20, len(obv_vals))]
    if len(sma20) < 2:
        return None
    return sma20[-1] - sma20[-2]


def proxy_vol_trace(candles_5m: list[dict]) -> str | None:
    """Proxy for Vol Trace direction. Uses ATR/Close ratio trend."""
    ratios = []
    for c in candles_5m:
        if c.get("atr_14") and c.get("close") and c["close"] > 0:
            ratios.append(c["atr_14"] / c["close"])
    if len(ratios) < 10:
        return None
    # Compare recent vs 10 bars ago
    recent = sum(ratios[-3:]) / 3
    older = sum(ratios[-10:-7]) / 3
    if older == 0:
        return None
    if recent < older:
        return "BULL"  # Declining vol = bullish
    return "BEAR"  # Rising vol = bearish
