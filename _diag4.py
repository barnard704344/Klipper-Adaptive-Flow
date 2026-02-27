#!/usr/bin/env python3
import urllib.request, json

d = json.loads(urllib.request.urlopen('http://localhost:7127/api/data').read().decode())

for i, r in enumerate(d.get('recommendations', [])):
    print(f"\n--- Rec {i} ---")
    print(f"severity: {r.get('severity')}")
    print(f"title: {repr(r.get('title'))}")
    print(f"detail: {repr(r.get('detail'))}")
    print(f"action: {repr(r.get('action'))}")
    cc = r.get('config_changes', [])
    if cc:
        for j, c in enumerate(cc):
            print(f"  cfg[{j}]: var={c.get('variable')} suggested={c.get('suggested')} desc={repr(c.get('description'))}")
            # Check if 'suggested' is a valid JS literal
            sv = c.get('suggested')
            if isinstance(sv, str):
                print(f"    WARNING: suggested is string: {repr(sv)}")
            elif isinstance(sv, (int, float)):
                print(f"    OK: suggested is number: {sv}")
            else:
                print(f"    WARNING: suggested is {type(sv).__name__}: {repr(sv)}")
    else:
        print("  no config_changes")
