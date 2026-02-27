#!/usr/bin/env python3
"""Quick diagnostic: is the served dashboard HTML valid?"""
import re, urllib.request, json, sys

url = 'http://localhost:7127/'
html = urllib.request.urlopen(url).read().decode()
print("Total HTML bytes:", len(html))

scripts = re.findall(r'<script[^>]*>', html)
print("Script tags:", scripts)

if 'try{D=' in html:
    print("try{D= FOUND (new code)")
    idx = html.index('try{D=')
    snippet = html[idx:idx+100]
    # Check JSON start
    print("Snippet after try{D=:", repr(snippet[:100]))
elif 'var D=' in html:
    print("var D= FOUND (old code)")
else:
    print("NO D= found!")

if '__DASHBOARD_DATA__' in html:
    print("ERROR: placeholder NOT replaced!")
else:
    print("Placeholder replaced OK")

# Check closing tags
print("Has </script>:", '</script>' in html or '<\\/script>' in html)
print("Has </body>:", '</body>' in html)
print("Has </html>:", '</html>' in html)

# Check if the JSON data itself is valid
m = re.search(r'try\{D=(.*?)\}catch', html, re.DOTALL)
if m:
    raw = m.group(1)
    print("JSON data length:", len(raw))
    try:
        d = json.loads(raw)
        print("JSON parse: OK")
        print("  summary:", bool(d.get('summary')))
        print("  sessions:", len(d.get('sessions', [])))
        print("  timeline points:", len(d.get('timeline', [])))
        print("  recommendations:", len(d.get('recommendations', [])))
        print("  error:", d.get('error', 'none'))
        # Check for problematic values
        def check_vals(obj, path=""):
            if isinstance(obj, str):
                if '</script>' in obj.lower():
                    print(f"  WARNING: </script> in string at {path}")
                if '</' in obj:
                    print(f"  NOTE: </ found in string at {path}: {repr(obj[:80])}")
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    check_vals(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj[:5]):
                    check_vals(v, f"{path}[{i}]")
        check_vals(d)
    except json.JSONDecodeError as e:
        print("JSON parse FAILED:", e)
        print("First 200 chars:", repr(raw[:200]))
        print("Last 200 chars:", repr(raw[-200:]))
else:
    print("Could not extract JSON blob from try{D=...}catch pattern")
    # Try the old pattern
    m2 = re.search(r'var D=(.*?);\s*var isLive', html, re.DOTALL)
    if m2:
        print("Found old var D= pattern, length:", len(m2.group(1)))

# Check the API endpoint too
api = urllib.request.urlopen('http://localhost:7127/api/data').read().decode()
try:
    ad = json.loads(api)
    print("\nAPI /api/data: valid JSON, summary:", bool(ad.get('summary')))
except:
    print("\nAPI /api/data: INVALID JSON")
