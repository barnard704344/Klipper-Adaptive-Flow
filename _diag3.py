#!/usr/bin/env python3
import sys, urllib.request
d = urllib.request.urlopen('http://localhost:7127/api/data').read().decode()
print('Has Infinity:', 'Infinity' in d)
print('Has NaN:', 'NaN' in d)
print('Has undefined:', 'undefined' in d)
print('Has null:', 'null' in d)

# Try actually eval-ing it in a JS-like way
import json
obj = json.loads(d)
print('JSON loads OK')

# Check for odd types
def scan(obj, path=''):
    if isinstance(obj, str) and len(obj) > 200:
        print(f'Long string at {path}: {len(obj)} chars')
    if isinstance(obj, dict):
        for k, v in obj.items():
            scan(v, f'{path}.{k}')
    if isinstance(obj, list) and len(obj) > 0:
        for i, v in enumerate(obj[:3]):
            scan(v, f'{path}[{i}]')
scan(obj)
