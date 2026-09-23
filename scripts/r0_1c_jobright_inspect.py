"""Temporary read-only inspection of current provider-rendered destination links."""
from __future__ import annotations
import re, shutil, subprocess
from urllib.parse import urlsplit, urlunsplit

URLS = {
    "lensa": "https://lensa.com/cgw/2f55ead837014d2ba9b3675e7e4f4ea1tpsjo1?jpsi=jobright.ai",
    "jobright": "https://jobright.ai/jobs/info/6ab2caff8254c44790e57626",
}

def safe(value: str) -> str:
    parts = urlsplit(value)
    path = re.sub(r"/(?:f/a|ls/click)/.*$", "/<tracking>", parts.path, flags=re.I)
    path = re.sub(r"/jobs/info/[^/]+", "/jobs/info/<id>", path, flags=re.I)
    path = re.sub(r"\d{4,}", "<id>", path)
    return urlunsplit((parts.scheme, parts.netloc.casefold(), path, "", ""))

def main() -> int:
    chrome = next((shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if shutil.which(name)), None)
    if not chrome:
        raise SystemExit("browser unavailable")
    for name, url in URLS.items():
        completed = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw", "--dump-dom", url], capture_output=True, text=True, timeout=30, check=False)
        body = completed.stdout or ""
        links = [safe(item) for item in re.findall(r"https?://[^\"'<>\\s]+", body, re.I) if "greenhouse" in item.casefold() or "lever.co" in item.casefold()]
        labels = re.findall(r".{0,100}Original Job Posting.{0,300}", body, re.I)
        print({"provider": name, "exit": completed.returncode, "body_bytes": len(body), "original_post_label": bool(labels), "label_context": [re.sub(r"https?://[^\"'<>\\s]+", "<url>", item)[:400] for item in labels[:3]], "terminal_urls": sorted(set(links))})
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
