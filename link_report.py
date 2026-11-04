#!/usr/bin/env python3
"""Show how the video and flight-data link held up during a recording.

Usage: ./link_report.py recordings/<name> [--step 5]

For each time window it prints frames per second, the longest video gap,
the longest flight-data gap, distance from home, altitude and (when
recorded) the Mac's Wi-Fi signal strength, so you can see at what distance
the link starts to fail.
"""
import argparse
import csv
import os

from recorder import _parse, distance_m


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--step", type=float, default=5.0, help="window length in seconds")
    args = ap.parse_args()

    with open(os.path.join(args.folder, "frames.csv")) as f:
        frames = [float(r["t"]) for r in csv.DictReader(f)]
    with open(os.path.join(args.folder, "telemetry.csv")) as f:
        tel = [{k: _parse(v) for k, v in r.items()} for r in csv.DictReader(f)]
    if not tel:
        raise SystemExit("no flight data in this recording")
    t0 = min([tel[0]["t"]] + frames[:1])
    t1 = max([tel[-1]["t"]] + frames[-1:])
    home = next(((r["lat"], r["lon"]) for r in tel if r.get("lat")), None)

    print("%6s %6s %8s %8s %7s %6s %7s" % ("time", "fps", "vid gap", "data gap", "dist m", "alt m", "rssi"))
    total_gap = 0.0
    t = t0
    while t < t1:
        end = t + args.step
        fr = [x for x in frames if t <= x < end]
        tr = [r for r in tel if t <= r["t"] < end]
        # longest stretch without a frame / flight data inside this window
        def longest(ts):
            pts = [t] + ts + [min(end, t1)]
            return max(b - a for a, b in zip(pts, pts[1:]))
        vgap = longest(fr)
        dgap = longest([r["t"] for r in tr])
        total_gap += sum(g - 1.0 for g in (b - a for a, b in zip([t] + fr, fr + [end])) if g > 1.0)
        last = tr[-1] if tr else {}
        dist = distance_m(home, (last["lat"], last["lon"])) if home and last.get("lat") else None
        rssi = last.get("rssi_dbm")
        print("%5.0fs %6.1f %7.1fs %7.1fs %7s %6s %7s" % (
            t - t0, len(fr) / args.step, vgap, dgap,
            "%.1f" % dist if dist is not None else "-",
            "%.1f" % last["alt_m"] if last.get("alt_m") is not None else "-",
            "%d dBm" % rssi if rssi is not None else "-"))
        t = end
    dur = t1 - t0
    print("\n%d frames in %.0f s = %.1f fps average; about %.0f s (%.0f%%) of the recording had no video"
          % (len(frames), dur, len(frames) / dur, total_gap, 100 * total_gap / dur))


if __name__ == "__main__":
    main()
