"""Runs the engine loops on background threads inside the web process."""
import threading
import time
import traceback

import engine
from db import log_event

JOBS = [  # (name, function, interval seconds)
    ("verify", engine.verify_tick, 5),
    ("send", engine.send_tick, 20),
    ("sync", engine.sync_tick, 120),
    ("warmup", engine.warmup_tick, 300),
    ("autopilot", engine.autopilot_tick, 600),
]

_stop = threading.Event()
status = {}


def _loop(name, fn, interval):
    while not _stop.is_set():
        started = time.time()
        try:
            n = fn()
            status[name] = {"last_run": time.strftime("%H:%M:%S"), "last_count": n, "error": ""}
        except Exception as e:
            status[name] = {"last_run": time.strftime("%H:%M:%S"), "last_count": 0, "error": str(e)}
            log_event(name, traceback.format_exc(limit=3), "error")
        _stop.wait(max(1, interval - (time.time() - started)))


def start():
    _stop.clear()
    for name, fn, interval in JOBS:
        threading.Thread(target=_loop, args=(name, fn, interval), daemon=True, name=f"coldstack-{name}").start()


def stop():
    _stop.set()
