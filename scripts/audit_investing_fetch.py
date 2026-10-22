"""Audit: how is each Investing.com-backed symbol actually fetched?
Find any remaining python-API / httpx / requests path to investing.com."""
import glob
import os
import re
import sys

ROOT = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"

PATTERNS = {
    "api.investing.com (JSON API)": r"api\.investing\.com",
    "www.investing.com": r"www\.investing\.com",
    "au/uk/us.investing.com": r"(au|uk|us)\.investing\.com",
    "httpx": r"\bhttpx\b",
    "curl_cffi": r"curl_cffi",
    "requests.get": r"requests\.(get|post)",
    "investing_html_scraper": r"investing_html_scraper",
    "impersonate": r"impersonate",
    "INVESTING_URLS": r"INVESTING_URLS",
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages"}

print("=" * 90)
print("A) every file that mentions investing.com")
print("=" * 90)

hits = {}
for path in glob.glob(os.path.join(ROOT, "**", "*"), recursive=True):
    if not os.path.isfile(path):
        continue
    if any(s in path.replace("\\", "/") for s in SKIP_DIRS):
        continue
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".py", ".md", ".json", ".sql", ".yaml", ".yml", ".toml", ".txt", ".ps1",
                   ".bat", ".env", ".example", ".ini", ".cfg"):
        continue
    try:
        src = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    if "investing" not in src.lower():
        continue
    rel = path.replace(ROOT + "\\", "").replace("\\", "/")
    hits[rel] = src

print("  files found: {}".format(len(hits)))
print()
for rel in sorted(hits):
    src = hits[rel]
    found = [k for k, p in PATTERNS.items() if re.search(p, src)]
    print("  {:<70} {}".format(rel, ", ".join(found)))

print()
print("=" * 90)
print("B) production scripts only - full fetch-path detail")
print("=" * 90)

PROD_DIRS = [r"src\db_builder", r"scripts"]
for rel in sorted(hits):
    if not any(rel.startswith(d.replace("\\", "/")) for d in PROD_DIRS):
        continue
    if rel.startswith("scripts/") and not rel.endswith(".py"):
        continue
    src = hits[rel]
    if "api.investing.com" in src or "INVESTING_URLS" in src:
        print()
        print("-" * 90)
        print("  FILE:", rel)
        print("-" * 90)
        for i, line in enumerate(src.splitlines(), 1):
            if re.search(r"investing|httpx|curl_cffi|impersonate|INVESTING_URLS", line, re.I):
                print("    {:>4}| {}".format(i, line.strip()[:150]))

print()
print("=" * 90)
print("C) is the macro fetch pipeline using the HTML scraper?")
print("=" * 90)
for rel in sorted(hits):
    if not rel.startswith("src/db_builder/"):
        continue
    src = hits[rel]
    if "investing" in src.lower():
        print("  {:<46} len={}".format(rel, len(src)))
