#!/usr/bin/env python3
import re, urllib.request, json

html = urllib.request.urlopen('http://localhost:7127/').read().decode()
m = re.search(r'try\{D=(.*?)\}catch', html, re.DOTALL)
raw = m.group(1)

issues = []
if '</script' in raw.lower():
    issues.append('Contains </script> - FATAL')

pos = 0
while True:
    p = raw.find('</', pos)
    if p < 0:
        break
    ctx = repr(raw[max(0,p-10):p+20])
    issues.append(f'</ at pos {p}: {ctx}')
    pos = p + 1
    if len(issues) > 10:
        break

if '<!--' in raw:
    issues.append('Contains HTML comment <!-- ')

# Check for characters that break JS string context
for bad in ['\n', '\r', '\x00']:
    if bad in raw:
        issues.append(f'Contains problematic char: {repr(bad)}')

if not issues:
    print('No HTML/JS-breaking sequences found')
else:
    for i in issues:
        print(i)

# Also verify the full HTML parses as expected
idx_script = html.rfind('<script>')
if idx_script > 0:
    script_content = html[idx_script+8:]
    end = script_content.find('</script>')
    if end > 0:
        script_content = script_content[:end]
        print(f'\nScript block: {len(script_content)} chars')
        print(f'Starts with: {repr(script_content[:60])}')
        print(f'Ends with: {repr(script_content[-60:])}')
    else:
        print('\nERROR: No closing </script> after main script block!')
