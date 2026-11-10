"""Hold a fixed share of RAM so Oracle never sees an idle Always Free A1 instance.

Run as `python3 keepalive.py [fraction]`; fraction of MemTotal defaults to 0.25 and must be
in (0, 0.5]. It allocates and touches that many bytes once, then sleeps until stopped. It
uses no CPU and no network after start. See docs/deploy/oracle.md, "Idle reclamation".
"""

import signal
import sys
from pathlib import Path

FRACTION_DEFAULT = 0.25
FRACTION_MAX = 0.5


def mem_total_bytes(meminfo: Path = Path("/proc/meminfo")) -> int:
    """Return MemTotal from a /proc/meminfo style file, in bytes.

    Raises ValueError when the file has no well-formed MemTotal line.
    """
    for line in meminfo.read_text(encoding="ascii").splitlines():
        name, _, rest = line.partition(":")
        if name == "MemTotal":
            value, unit = rest.split()
            if unit != "kB" or int(value) <= 0:
                raise ValueError(f"unexpected MemTotal line: {line!r}")
            return int(value) * 1024
    raise ValueError(f"no MemTotal line in {meminfo}")


def ballast_bytes(total: int, fraction: float) -> int:
    """Return how many bytes to hold: total times fraction, for 0 < fraction <= 0.5.

    Raises ValueError for a fraction outside that range or a non-positive total.
    """
    if not 0.0 < fraction <= FRACTION_MAX:
        raise ValueError(f"fraction must be in (0, {FRACTION_MAX}], got {fraction}")
    if total <= 0:
        raise ValueError(f"total must be positive, got {total}")
    size = int(total * fraction)
    assert 0 < size <= total // 2
    return size


def main(argv: list[str]) -> int:
    """Allocate the ballast and sleep until a signal stops the process."""
    fraction = float(argv[1]) if len(argv) > 1 else FRACTION_DEFAULT
    size = ballast_bytes(mem_total_bytes(), fraction)
    ballast = b"\x01" * size
    print(f"keepalive: holding {len(ballast) // 2**20} MiB", flush=True)
    signal.pause()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
