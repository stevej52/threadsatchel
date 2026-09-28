"""Small, fail-closed resource guard for optional background memory work.

No monitoring service, dependency, shell command, network call, or process list.
Windows counters are machine-wide CPU time and currently available physical RAM.
"""
import ctypes
import math
import os
import time


def _windows_snapshot():
    if os.name != 'nt':
        raise OSError('Native resource counters unavailable')
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD),
                    ('total_physical', ctypes.c_ulonglong), ('available_physical', ctypes.c_ulonglong),
                    ('total_pagefile', ctypes.c_ulonglong), ('available_pagefile', ctypes.c_ulonglong),
                    ('total_virtual', ctypes.c_ulonglong), ('available_virtual', ctypes.c_ulonglong),
                    ('available_extended_virtual', ctypes.c_ulonglong)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetSystemTimes.argtypes = [ctypes.POINTER(FileTime)] * 3
    api.GetSystemTimes.restype = wintypes.BOOL
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = wintypes.BOOL
    idle, kernel, user = FileTime(), FileTime(), FileTime()
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not api.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    def ticks(value):
        return (int(value.high) << 32) | int(value.low)
    return ticks(idle), ticks(kernel) + ticks(user), memory.available_physical / (1024 * 1024)


class ResourceMonitor:
    """CPU is a measured delta, never the misleading total since boot.

    The initial reading has no CPU delta and intentionally denies idle work.
    CPU samples less than one second apart reuse the last measured delta; RAM
    is read each time. Missing/reset counters always fail closed.
    """
    def __init__(self, reader=None, clock=None):
        self._reader = reader or _windows_snapshot
        self._clock = clock or time.monotonic
        self._previous = None
        self._cpu = None

    def sample(self):
        try:
            idle, total, available_mb = self._reader()
            now = self._clock()
            if (not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and v >= 0 for v in (idle, total, available_mb))
                    or idle > total):
                raise ValueError('Invalid native resource sample')
            previous = self._previous
            if previous is None:
                self._previous = (now, idle, total)
            elif now - previous[0] >= 1:
                delta_idle, delta_total = idle - previous[1], total - previous[2]
                self._previous = (now, idle, total)
                if delta_total <= 0 or not 0 <= delta_idle <= delta_total:
                    self._cpu = None
                else:
                    self._cpu = 100 * (delta_total - delta_idle) / delta_total
            return dict(available=self._cpu is not None, cpu_percent=self._cpu,
                        available_mb=available_mb)
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            self._previous = None
            self._cpu = None
            return dict(available=False, cpu_percent=None, available_mb=None)


def resource_reason(config, sample):
    """Return a fixed diagnostic code, or None when background work is safe."""
    cpu, ram = sample.get('cpu_percent'), sample.get('available_mb')
    if (sample.get('available') is not True or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (cpu, ram)) or not 0 <= cpu <= 100 or ram < 0):
        return 'resource_unavailable'
    if cpu > config.get('idle_max_cpu_percent', 25):
        return 'cpu_busy'
    if ram < config.get('idle_min_available_mb', 8192):
        return 'low_memory'
    return None


def resource_guard(config, sample):
    return resource_reason(config, sample) is None


def lower_process_priority():
    """Best-effort reduction for this optional worker only, never elevation."""
    try:
        if os.name == 'nt':
            from ctypes import wintypes
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.GetCurrentProcess.restype = wintypes.HANDLE
            api.GetPriorityClass.argtypes = [wintypes.HANDLE]
            api.GetPriorityClass.restype = wintypes.DWORD
            api.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.SetPriorityClass.restype = wintypes.BOOL
            handle = api.GetCurrentProcess()
            current = api.GetPriorityClass(handle)
            if current in (0x40, 0x4000):  # IDLE or BELOW_NORMAL: do not raise it.
                return True
            return bool(current and api.SetPriorityClass(handle, 0x4000))
        os.nice(5)
        return True
    except (OSError, AttributeError):
        return False
