"""
Debug script — shows what's in the analysis cache.
Run from project root: python debug_cache.py
"""
import json, os, hashlib

cache_path = None
for root, dirs, files in os.walk("."):
    for f in files:
        if f == "analysis_cache.json":
            cache_path = os.path.join(root, f)
            break

if not cache_path:
    print("analysis_cache.json NOT FOUND anywhere in project")
    print("Cache is never being saved to disk")
else:
    print(f"Found cache at: {cache_path}")
    with open(cache_path) as f:
        data = json.load(f)
    print(f"Cache entries: {len(data)}")
    for k, v in list(data.items())[:5]:
        print(f"  key={k[:16]}... symbol={v.get('symbol','?')} conviction={v.get('conviction',0):.0%}")
