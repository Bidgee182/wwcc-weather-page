"""
lake_utils.py - Shared lake calculation utilities for board email.

All constants loaded from data/lake_config.json. To change surface areas,
evaporation rates, zone thresholds, or pump rates, edit that file only.
"""

import json
from pathlib import Path


# ── Email audit log ───────────────────────────────────────────────────────────

def log_email(email_type, subject, recipients, status):
    """Append one row to data/email_log.csv - the audit trail of every send.

    Never raises: a logging failure must not break an email send.
    """
    import csv
    from datetime import datetime, timezone
    try:
        path = Path(__file__).parent.parent / 'data' / 'email_log.csv'
        new = not path.exists() or path.stat().st_size == 0
        recips = recipients if isinstance(recipients, (list, tuple)) else [recipients]
        recips = [str(r) for r in recips if r]
        with path.open('a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if new:
                w.writerow(['timestamp_utc', 'email_type', 'subject',
                            'recipient_count', 'recipients', 'status'])
            w.writerow([datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                        email_type, subject, len(recips), '; '.join(recips), status])
    except Exception:
        pass


# ── Encrypted email recipients ────────────────────────────────────────────────

def load_recipients():
    """Decrypt data/email_recipients.enc.json using the RECIPIENTS_KEY env var.

    Returns the streams dict ({'gk': [...], 'lake_to': [...], ...}) or None -
    callers fall back to the legacy GitHub-secret env vars, so the encrypted
    file is strictly additive and can never break sending.
    """
    import os, base64, hashlib
    key = os.environ.get('RECIPIENTS_KEY', '')
    path = Path(__file__).parent.parent / 'data' / 'email_recipients.enc.json'
    if not key or not path.exists():
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        blob = json.loads(path.read_text(encoding='utf-8'))
        kd = hashlib.pbkdf2_hmac('sha256', key.encode(), b'wwcc-recipients-v1', 100000, 32)
        pt = AESGCM(kd).decrypt(bytes.fromhex(blob['iv']),
                                base64.b64decode(blob['ct']), None)
        return (json.loads(pt.decode('utf-8')) or {}).get('streams') or None
    except Exception:
        return None


def recipients_for(stream, env_value=''):
    """Recipient list for a stream: encrypted file first, env-secret fallback."""
    data = load_recipients()
    if data and data.get(stream):
        return [a.strip() for a in data[stream] if a and a.strip()]
    return [a.strip() for a in (env_value or '').split(',') if a.strip()]


# ── Email plain-text part ─────────────────────────────────────────────────────

def html_to_text(html_str):
    """Rough HTML to plain-text conversion for the text/plain email part.

    Not a full renderer - just enough that a text-only client (or a spam
    filter checking for a text alternative) gets readable content.
    """
    import html as _html
    import re
    text = re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', html_str,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|tr|div|h[1-6]|table)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = _html.unescape(text)
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    text = re.sub(r' ?\n ?', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

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

    # Threshold is the BOTTOM of the current zone (where you exit it), not the
    # bottom of the next zone - those differ by one full zone's depth.
    threshold   = current_zone_info(ahd)['min_ahd']
    vol_ml      = vol_between_ml(threshold, ahd)
    pump_ml     = current_zone_info(ahd)['max_pump_ml_day']
    evap_ml     = evap_ml_day(ahd, month)
    total_loss  = pump_ml + evap_ml

    if total_loss <= 0:
        return float('inf'), nxt

    return vol_ml / total_loss, nxt


# ── Cease-to-pump projection ───────────────────────────────────────────────────

def project_to_cease(ahd, start_date):
    """Day-by-day simulation: date AHD hits the cease-to-pump threshold.

    No future rainfall assumed. Each day deducts:
      - Lake surface evaporation: BOM pan × pan_factor × lake_area (ML)
      - Irrigation pumping (ML): from daily_kl_by_month in lake_config.json,
        only in active_months (no irrigation Jun/Jul/Aug).

    The cease threshold is derived from the lowest numeric min_ahd in
    zone_thresholds (Level 4 boundary = 189.650 m AHD).

    Returns:
        datetime.date  projected cease date
        None           if ahd is already at or below cease level
    """
    from datetime import timedelta

    cfg       = get_config()
    pan_rates = cfg['evaporation']['monthly_pan_mm_day']
    pf        = cfg['evaporation']['pan_factor']
    irrig_kl  = cfg['town_water']['daily_kl_by_month']
    active_m  = set(cfg['irrigation_season']['active_months'])
    cease_ahd = min(z['min_ahd'] for z in cfg['zone_thresholds'] if z['min_ahd'] is not None)

    cur_ahd  = float(ahd)
    cur_date = start_date

    if cur_ahd <= cease_ahd:
        return None

    for _ in range(1095):  # 3-year cap
        m    = cur_date.month
        area = lake_area_m2(cur_ahd)

        # BOM open-water evaporation (ML/day)
        evap_ml  = float(pan_rates[str(m)]) * pf * area / 1_000_000

        # Irrigation pumped from lake (ML/day) - zero in off-season months
        irrig_ml = float(irrig_kl.get(str(m), 0)) / 1000.0 if m in active_m else 0.0

        # AHD drop: volume_m3 = ML × 1000; depth = volume_m3 / area_m2
        cur_ahd  -= (evap_ml + irrig_ml) * 1000.0 / area
        cur_date += timedelta(days=1)

        if cur_ahd <= cease_ahd:
            return cur_date

    return None


def town_water_cost_projection(cease_date, end_date=None):
    """Estimated town water cost from cease_date to end_date.

    Counts only days in active irrigation months (daily_kl_by_month from
    lake_config.json) at cost_per_kl. No fixed service charges included.

    Args:
        cease_date: datetime.date extraction ceases
        end_date:   datetime.date to stop counting
                    (defaults to 31 March of next calendar year)

    Returns:
        float  total estimated cost in dollars
    """
    from datetime import date, timedelta

    cfg      = get_config()
    cost_kl  = cfg['town_water']['cost_per_kl']
    irrig_kl = cfg['town_water']['daily_kl_by_month']
    active_m = set(cfg['irrigation_season']['active_months'])

    if end_date is None:
        yr       = cease_date.year + (1 if cease_date.month > 3 else 0)
        end_date = date(yr, 3, 31)

    total = 0.0
    cur   = cease_date
    while cur <= end_date:
        if cur.month in active_m:
            total += float(irrig_kl.get(str(cur.month), 0)) * cost_kl
        cur += timedelta(days=1)

    return total
