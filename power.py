#!/usr/bin/env python3
import subprocess
import re

out = subprocess.check_output(
    ["vcgencmd", "pmic_read_adc"],
    text=True
)

curr = {}
volt = {}

for line in out.splitlines():
    m = re.search(r'(\S+)_A.*=([0-9.]+)A', line)
    if m:
        curr[m.group(1)] = float(m.group(2))
        continue

    m = re.search(r'(\S+)_V.*=([0-9.]+)V', line)
    if m:
        volt[m.group(1)] = float(m.group(2))

total = 0.0
for rail in sorted(curr):
    if rail in volt:
        p = curr[rail] * volt[rail]
        total += p
        print(f"{rail:12} {p:6.3f} W")

print(f"\nTotal: {total:.3f} W")
