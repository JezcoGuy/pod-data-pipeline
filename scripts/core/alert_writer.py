"""
alert_writer.py
===============
Shared module for writing alert sections to the nightly scratch file.
Import this in every sync script instead of sending emails directly.

Usage in each script:
    from alert_writer import write_alert_section

    # Build your alert lines
    lines = [
        f"  #CW1112 | £49.93 | 2025-02-10",
        f"  #CW1116 | £26.48 | 2025-02-11",
    ]

    write_alert_section(
        script_name = "SHOPIFY SYNC",
        alerts      = lines,
        summary     = f"⚠️  {len(lines)} unmatched orders > 48h"
    )

The scratch file is appended to — never overwritten by individual scripts.
nightly_alert.py reads the full file at 4am, sends one email, clears it.
"""

import os
import logging
from datetime import datetime

ALERT_FILE = os.getenv('ALERT_FILE', '/opt/your_brand_id/logs/nightly_alert.txt')

logger = logging.getLogger(__name__)


def write_alert_section(script_name, alerts, summary=None):
    """
    Append a formatted section to the nightly alert scratch file.

    Args:
        script_name: e.g. "SHOPIFY SYNC", "GELATO SYNC", "META SYNC"
        alerts:      list of strings, one per alert line
        summary:     optional one-line summary e.g. "⚠️  3 unmatched orders"
                     If None, uses f"{len(alerts)} alert(s)"
    """
    if not alerts:
        return

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    summary   = summary or f"{len(alerts)} alert(s)"

    lines = [
        f"\n{'=' * 60}",
        f"=== {script_name} — {timestamp} ===",
        f"{'=' * 60}",
        summary,
        "",
    ]
    lines.extend(alerts)
    lines.append("")

    content = "\n".join(lines) + "\n"

    try:
        os.makedirs(os.path.dirname(ALERT_FILE), exist_ok=True)
        with open(ALERT_FILE, 'a', encoding='utf-8') as f:
            f.write(content)
        logger.info(f'Alert section written: {script_name} — {len(alerts)} item(s)')
    except Exception as e:
        logger.error(f'Failed to write alert section: {e}')


def write_alert_line(script_name, message):
    """
    Write a single alert line — convenience wrapper for simple alerts.

    Args:
        script_name: e.g. "PRINTIFY SYNC"
        message:     single alert message string
    """
    write_alert_section(
        script_name=script_name,
        alerts=[f"  {message}"],
        summary=message
    )
