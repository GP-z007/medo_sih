"""Measured cgroup resources; never label VM-wide psutil totals as container usage."""
import time
from pathlib import Path

import psutil


class ResourceMonitor:
    def __init__(self, root=Path("/sys/fs/cgroup")):
        self.root = root
        self.previous = None

    def sample(self):
        result = {
            "scope": "container cgroup; filesystem disk; API process RSS",
            "cpu_percent": None,
            "cpu_basis": "one CPU core (may exceed 100%)",
            "memory_percent": None,
            "memory_used_bytes": None,
            "memory_limit_bytes": None,
            "disk_percent": psutil.disk_usage(".").percent,
            "process_rss_bytes": psutil.Process().memory_info().rss,
        }
        try:
            values = dict(line.split() for line in (self.root / "cpu.stat").read_text().splitlines())
            current = (time.monotonic(), int(values["usage_usec"]))
            if self.previous and current[0] > self.previous[0]:
                result["cpu_percent"] = max(0, (current[1] - self.previous[1]) / 1e6 /
                                            (current[0] - self.previous[0]) * 100)
            self.previous = current
        except (OSError, ValueError, KeyError):
            result["cpu_basis"] = "cgroup CPU unavailable"
        try:
            result["memory_used_bytes"] = int((self.root / "memory.current").read_text())
            limit = (self.root / "memory.max").read_text().strip()
            if limit != "max":
                result["memory_limit_bytes"] = int(limit)
                if int(limit) > 0:
                    result["memory_percent"] = result["memory_used_bytes"] / int(limit) * 100
        except (OSError, ValueError):
            pass
        return result
