"""
lake_utils.py — Shared lake calculation utilities for board email.

All constants loaded from data/lake_config.json. To change surface areas,
evaporation rates, zone thresholds, or pump rates, edit that file only.
"""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent / 'data' / 'lake_config.json'
_config = None


def get_config():
    global _config
    if _config is None:
        _config = json.loads(_CONFIG_PATH.read_text(encoding='utf-8'))
    return _config


# ── Surface area ──────────────────────────────────────────────────────────────

def lake_area_m2(ahd):
    """Estimated lake surface area (m²) at given AHD.

    Linear model fitted between the calibration measurement and full supply level.
    Extrapolates linearly below the calibration point.
    Update calibration_ahd / calibration_area_m2 in lake_config.json if a new
    survey is conducted.
    """
    cfg  = get_config()['lake_geometry']
    cal_ahd  = cfg['calibration_ahd']
    cal_area = cfg['calibration_area_m2']
    full_ahd = cfg['full_supply_ahd']
    full_area = cfg['full_area_m2']
    slope = (full_area - cal_area) / (full_ahd - cal_ahd)
    return max(0.0, cal_area + slope * (ahd - cal_ahd))


# ── Volume ────────────────────────────────────────────────────────────────────

def vol_between_ml(lo_ahd, hi_ahd, steps=500):
    """Volume of water (ML) between two AHD levels.

    Uses trapezoidal integration over the linear area model, anchored to the
    official full-supply capacity in lake_config.json.
    """
    if lo_ahd >= hi_ahd:
        return 0.0
    dh    = (hi_ahd - lo_ahd) / steps
    total = 0.0
    for i in range(steps):
        h1 = lo_ahd + i * dh
        h2 = lo_ahd + (i + 1) * dh
        total += (lake_area_m2(h1) + lake_area_m2(h2)) / 2.0 * dh
    return total / 1000.0  # m³ → ML


# ── Zone information ──────────────────────────────────────────────────────────

def current_zone_info(ahd):
    """Return the zone dict for the current lake AHD level."""
    zones = get_config()['zone_thresholds']
    for z in zones:
        if z['min_ahd'] is None or ahd >= z['min_ahd']:
            return z
    return zones[-1]


def next_zone_below(ahd):
    """Return the zone dict for the next zone below the current level, or None."""
    zones = get_config()['zone_thresholds']
    current = current_zone_info(ahd)
    current_num = current['zone']
    for z in zones:
        if z['zone'] == current_num + 1:
            return z
    return None


# ── Evaporation ───────────────────────────────────────────────────────────────

def evap_ml_day(ahd, month):
    """Open-water evaporation loss from the lake in ML/day.

    Uses BOM Class A pan evaporation monthly averages × pan_factor (0.70)
    from lake_config.json. Update monthly_pan_mm_day there to change rates.
    Surface area at the given AHD is calculated from the linear model, so
    evaporation correctly reduces as the lake level (and area) drops.
    """
    cfg     = get_config()['evaporation']
    pan_mm  = cfg['monthly_pan_mm_day'][str(month)]
    lake_mm = pan_mm * cfg['pan_factor']          # open-water mm/day
    area    = lake_area_m2(ahd)                   # m² at this level
    return lake_mm * area / 1_000_000.0           # mm × m² → ML (÷1e6)


# ── Projection ────────────────────────────────────────────────────────────────

def days_to_next_zone(ahd, month):
    """Estimated days until lake drops to the next lower zone threshold.

    Assumes pumping at current zone's maximum allowable rate with no rainfall
    (conservative planning figure). Evaporation uses current month's BOM pan
    average × 0.70 lake factor. Surface area shrinkage as level drops is
    accounted for via the linear area model.

    Returns:
        (days: float | None, next_zone: dict | None)
        days is None  → already at lowest zone (cease to pump)
        days is inf   → net balance is positive (lake rising)
    """
    nxt = next_zone_below(ahd)
    if nxt is None:
        return None, None

    threshold   = nxt['min_ahd']
    vol_ml      = vol_between_ml(threshold, ahd)
    pump_ml     = current_zone_info(ahd)['max_pump_ml_day']
    evap_ml     = evap_ml_day(ahd, month)
    total_loss  = pump_ml + evap_ml

    if total_loss <= 0:
        return float('inf'), nxt

    return vol_ml / total_loss, nxt
