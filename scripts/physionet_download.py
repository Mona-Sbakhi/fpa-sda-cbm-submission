#!/usr/bin/env python3
"""Download a PhysioNet project (same files as `wget -r -N -c -np --user USER --ask-password`).

    python scripts/physionet_download.py vindr-pcxr 1.0.0 --username USER --out ~/vindr-pcxr

The password is asked with a hidden prompt and is never printed or stored. Files that already exist with the
right size are skipped, interrupted files are resumed, and at the end every file is checked against the
project's SHA256SUMS.txt. Keep --out OUTSIDE any cloud-synced folder (OneDrive, iCloud, Dropbox): the data use
agreement does not allow copies on third-party servers.
"""
import argparse
import base64
import getpass
import hashlib
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

ap = argparse.ArgumentParser()
ap.add_argument("project"); ap.add_argument("version")
ap.add_argument("--username", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--workers", type=int, default=6)
a = ap.parse_args()

out = Path(a.out).expanduser().resolve()
if any(s in str(out) for s in ("CloudStorage", "OneDrive", "iCloud", "Mobile Documents", "Dropbox", "Google Drive")):
    sys.exit(f"{out} looks like a cloud-synced folder; choose a local folder such as ~/{a.project}.")
BASE = f"https://physionet.org/files/{a.project}/{a.version}/"
try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()
AUTH = "Basic " + base64.b64encode(f"{a.username}:{getpass.getpass(f'PhysioNet password for {a.username}: ')}".encode()).decode()


class AuthStaysOnHost(urllib.request.HTTPRedirectHandler):
    """Never forward the password to another host (PhysioNet redirects some files to S3, which also rejects it)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX), AuthStaysOnHost())


def request(url, headers=None):
    req = urllib.request.Request(url, headers={"Authorization": AUTH, "User-Agent": "Wget/1.21", **(headers or {})})
    return OPENER.open(req, timeout=120)


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs += [v for k, v in attrs if k == "href" and v]


def listing(url):
    p = Links(); p.feed(request(url).read().decode("utf-8", "replace"))
    for h in p.hrefs:
        u = urljoin(url, h).split("#")[0].split("?")[0]
        if u.startswith(url) and u != url:
            yield u


try:
    files, todo = [], [BASE]
    while todo:
        d = todo.pop()
        for u in listing(d):
            (todo if u.endswith("/") else files).append(u)
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        sys.exit(f"PhysioNet refused access (HTTP {e.code}): wrong username/password, or the data use agreement "
                 f"for {a.project} is not signed for this account.")
    raise
files = sorted(set(files))
print(f"{len(files)} files listed under {BASE}")

lock = threading.Lock(); done = [0, 0]   # files, bytes


def get(url):
    rel = unquote(urlparse(url).path[len(urlparse(BASE).path):])
    dst = out / rel; part = dst.with_name(dst.name + ".part"); dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        try:
            if dst.exists():
                size = int(request(url, {"Range": "bytes=0-0"}).headers.get("Content-Range", "/-1").split("/")[-1])
                if size in (-1, dst.stat().st_size):
                    return rel, 0
                dst.unlink()
            have = part.stat().st_size if part.exists() else 0
            r = request(url, {"Range": f"bytes={have}-"} if have else None)
            mode = "ab" if have and r.status == 206 else "wb"
            n = 0
            with open(part, mode) as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk); n += len(chunk)
            part.rename(dst)
            return rel, n
        except urllib.error.HTTPError as e:
            if e.code == 416 and part.exists():   # .part already complete
                part.rename(dst); return rel, 0
            if e.code in (401, 403) or attempt == 4:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 4:
                raise


failed = []
with ThreadPoolExecutor(a.workers) as ex:
    futs = {ex.submit(get, u): u for u in files}
    for f in as_completed(futs):
        try:
            _, n = f.result()
            with lock:
                done[0] += 1; done[1] += n
                print(f"  {done[0]}/{len(files)} files, {done[1] / 1e9:.2f} GB downloaded this session", end="\r", flush=True)
        except Exception as e:
            failed.append(futs[f]); print(f"\n  failed: {futs[f]} ({type(e).__name__}: {e})")
print()
if failed:
    sys.exit(f"{len(failed)} files failed; run the same command again to resume.")

sums = out / "SHA256SUMS.txt"
if sums.exists():
    bad, n = [], 0
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        h, name = line.split(maxsplit=1); name = name.lstrip("*"); p = out / name
        if name == "SHA256SUMS.txt":
            continue
        n += 1
        if not p.exists():
            bad.append(name); continue
        d = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                d.update(chunk)
        if d.hexdigest() != h:
            bad.append(name); p.unlink(missing_ok=True)
        print(f"  checksums {n}", end="\r", flush=True)
    print()
    if bad:
        sys.exit(f"{len(bad)} files had a wrong checksum and were deleted; run the same command again.")
    print(f"all {n} files match SHA256SUMS.txt")
else:
    print("no SHA256SUMS.txt in the project; checksums not verified")
print("done:", out)
