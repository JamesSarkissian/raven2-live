#!/usr/bin/env python3
"""Download chosen country folders of the RDD2022 road-damage dataset.

RDD2022 is published on Figshare as one 13 GB zip that holds one zip per
country. The server allows ranged downloads, so this reads the outer zip's
index and copies out only the country zips you ask for.

Usage: ./fetch_rdd2022.py China_Drone United_States [--out ~/Downloads/RDD2022]

Dataset: RDD2022, Arya et al. (https://arxiv.org/abs/2209.08538),
https://figshare.com/articles/dataset/21431547 (CC BY 4.0 on Figshare;
CC BY-SA 4.0 per github.com/sekilab/RoadDamageDetector).
"""
import argparse
import io
import os
import shutil
import sys
import time
import urllib.request
import zipfile

URL = "https://ndownloader.figshare.com/files/38030910"


class HttpFile(io.RawIOBase):
    """A remote file read with HTTP range requests, so zipfile can seek in it."""

    def __init__(self, url):
        # Figshare redirects to a short-lived signed link, so every request goes
        # through the Figshare URL to get a fresh one
        head = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60)
        self.url = url
        self.size = int(head.headers["Content-Length"])
        self.pos = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        for attempt in range(5):
            try:
                req = urllib.request.Request(self.url, headers={"Range": "bytes=%d-%d" % (self.pos, self.pos + n - 1)})
                data = urllib.request.urlopen(req, timeout=60).read()
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("countries", nargs="+", help="e.g. China_Drone United_States Czech")
    ap.add_argument("--out", default="~/Downloads/RDD2022")
    args = ap.parse_args()
    out = os.path.expanduser(args.out)
    os.makedirs(out, exist_ok=True)

    outer = zipfile.ZipFile(io.BufferedReader(HttpFile(URL), buffer_size=8 << 20))
    members = {os.path.basename(i.filename)[:-4]: i for i in outer.infolist() if i.filename.endswith(".zip")}
    for country in args.countries:
        if country not in members:
            sys.exit("unknown folder %s; available: %s" % (country, ", ".join(sorted(members))))
    for country in args.countries:
        info = members[country]
        zpath = os.path.join(out, country + ".zip")
        if not (os.path.exists(zpath) and os.path.getsize(zpath) == info.file_size):
            print("downloading %s (%.0f MB)" % (country, info.compress_size / 1e6), flush=True)
            tmp = zpath + ".part"
            with outer.open(info) as src, open(tmp, "wb") as dst:
                done, t0 = 0, time.time()
                while True:
                    chunk = src.read(8 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
                    done += len(chunk)
                    print("  %5.0f / %.0f MB  %.1f MB/s" % (done / 1e6, info.file_size / 1e6,
                                                       done / 1e6 / max(time.time() - t0, 1e-3)), flush=True)
            os.replace(tmp, zpath)
        print("unpacking %s" % country, flush=True)
        with zipfile.ZipFile(zpath) as z:
            for m in z.infolist():
                # images and labels only; never write outside the output folder
                name = os.path.normpath(m.filename)
                if m.is_dir() or name.startswith("..") or os.path.isabs(name):
                    continue
                if not name.lower().endswith((".jpg", ".jpeg", ".png", ".xml", ".txt")):
                    continue
                dest = os.path.join(out, name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        os.remove(zpath)
    print("done:", out)


if __name__ == "__main__":
    main()
