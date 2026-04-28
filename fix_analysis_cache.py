"""
Patch script — replaces the broken analysis cache with a simple working version.
Run from project root: python fix_analysis_cache.py
"""
import re

with open("research\\analyst.py", "r", encoding="utf-8") as f:
    content = f.read()

# Replace the entire cache infrastructure with a simpler version
old_cache_block = '''# Disk-backed cache for Claude analysis results
# Keyed by a hash of the source item accession numbers + symbol
# Avoids re-analysing the same SEC filings every cycle
_ANALYSIS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "analysis_cache.json"
)'''

new_cache_block = '''# Disk-backed cache for Claude analysis results
import pathlib as _pathlib
_ANALYSIS_CACHE_PATH = str(_pathlib.Path.cwd() / "logs" / "analysis_cache.json")'''

if old_cache_block in content:
    content = content.replace(old_cache_block, new_cache_block)
    print("Replaced cache path block")
else:
    # Try alternative — just patch the path line directly
    content = re.sub(
        r'_ANALYSIS_CACHE_PATH\s*=.*?\n',
        'import pathlib as _pathlib\n_ANALYSIS_CACHE_PATH = str(_pathlib.Path.cwd() / "logs" / "analysis_cache.json")\n',
        content,
        count=1
    )
    print("Patched cache path via regex")

# Also fix _get_cache_path if it exists
content = re.sub(
    r'def _get_cache_path\(\).*?_ANALYSIS_CACHE_PATH = _get_cache_path\(\)\n',
    'import pathlib as _pathlib\n_ANALYSIS_CACHE_PATH = str(_pathlib.Path.cwd() / "logs" / "analysis_cache.json")\n',
    content,
    flags=re.DOTALL
)

# Make _save_analysis_cache print errors instead of swallowing them
old_save = '''def _save_analysis_cache(cache: dict):
    try:
        os.makedirs(os.path.dirname(_ANALYSIS_CACHE_PATH), exist_ok=True)
        with open(_ANALYSIS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        logger.debug("Analysis cache saved: %d entries to %s", len(cache), _ANALYSIS_CACHE_PATH)
    except Exception as e:
        logger.warning("Could not save analysis cache at %s: %s", _ANALYSIS_CACHE_PATH, e)'''

new_save = '''def _save_analysis_cache(cache: dict):
    try:
        import pathlib
        pathlib.Path(_ANALYSIS_CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(_ANALYSIS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        logger.info("Analysis cache saved: %d entries -> %s", len(cache), _ANALYSIS_CACHE_PATH)
    except Exception as e:
        logger.error("CACHE SAVE FAILED at %s: %s", _ANALYSIS_CACHE_PATH, e)'''

if old_save in content:
    content = content.replace(old_save, new_save)
    print("Updated _save_analysis_cache")
else:
    # Generic replacement
    content = re.sub(
        r'def _save_analysis_cache\(cache: dict\):.*?(?=\ndef |\n_ANALYSIS_CACHE)',
        new_save + '\n\n',
        content,
        flags=re.DOTALL
    )
    print("Updated _save_analysis_cache via regex")

with open("research\\analyst.py", "w", encoding="utf-8") as f:
    f.write(content)

print(f"Cache will be saved to: logs/analysis_cache.json (relative to {__import__('os').getcwd()})")
print("Restart start_research.bat and the cache should appear after the first cycle")
