"""Parse `tcpdump -tt -x` text of UDP packets into (time, src, dst, payload)."""
import re
import sys


def packets(path):
    cur = None
    for line in open(path):
        if not line.startswith("\t"):
            if cur:
                yield finish(cur)
            m = re.search(r"^(\d+\.\d+) .*?(\d+\.\d+\.\d+\.\d+)\.(\d+) > (\d+\.\d+\.\d+\.\d+)\.(\d+):", line)
            cur = [float(m.group(1)), m.group(2) + ":" + m.group(3), m.group(4) + ":" + m.group(5), ""] if m else None
        elif cur:
            cur[3] += "".join(line.split(":", 1)[1].split())
    if cur:
        yield finish(cur)


def finish(cur):
    raw = bytes.fromhex(cur[3])
    ihl = (raw[0] & 0x0F) * 4
    return cur[0], cur[1], cur[2], raw[ihl + 8:]


if __name__ == "__main__":
    for t, s, d, p in packets(sys.argv[1]):
        print("%.3f %s>%s %3d %s" % (t, s, d, len(p), p.hex()))
