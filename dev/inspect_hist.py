#!/usr/bin/env python3
"""Inspect hist series for STZ/MU/CCL to sanity-check the extended sample."""
import sys
sys.path.insert(0, '/opt/data/finance_work/earnings-radar')
import earnings_radar as er

for t in ['STZ', 'MU', 'CCL', 'NKE']:
    r = er.analyze('longbridge', t, 45, 3)
    hist = r.get('hist') or []
    print("==", t, "| ok:", r.get('ok'), "| n =", len(hist), "| med =", r.get('hist_med'))
    for h in hist:
        print("    %s  %+.2f%%" % (h['date'], h['move']))
