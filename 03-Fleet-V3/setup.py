# -*- coding: utf-8 -*-
"""Generate local config files from the safe *.example.json templates.

Run once after cloning:  python setup.py
The generated files contain placeholders - fill in your own credentials.
They are listed in .gitignore and must NEVER be committed.
"""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
PAIRS = [
    ("server/ai_config.example.json", "server/ai_config.json"),
    ("server/cloud_config.example.json", "server/cloud_config.json"),
    ("server/devices.example.json", "server/devices.json"),
]
for src, dst in PAIRS:
    s, d = ROOT / src, ROOT / dst
    if d.exists():
        print(f"[skip] {dst} already exists")
        continue
    shutil.copy2(s, d)
    print(f"[created] {dst}")
print("\nNext: fill in your own AK/SK, device secrets and AI API key.")
print("Or use environment variables: HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK / V3_AI_API_KEY")
