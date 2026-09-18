#!/usr/bin/env python3
"""
radon_ha_poller.py — poll a HOUND-1011S radon detector's RAM over SWD and
serve the readings as JSON for a Home Assistant REST sensor.

Hardware: detector SWD pads -> Pi Pico running debugprobe -> USB -> Pi Zero 2W
(or any Linux box). The MCU is never halted or reset; the detector keeps
running normally while we read.

Usage:
    pip install pyocd
    python3 radon_ha_poller.py
    curl http://<pi-ip>:8080/

Requires the FM33LC0xx DFP pack (see README) next to this script.
RAM addresses below are for firmware v2.0.03 — verify against your own
dumps first; they may shift between firmware builds.
"""
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from pyocd.core.helpers import ConnectHelper

PACK = "FMSH.FM33LC0XX_DFP.3.0.4.pack"  # adjust to your pack version
TARGET = "fm33lc04x"
POLL_SECONDS = 300                       # radon moves slowly; be gentle
HTTP_PORT = 8080

STATS_ADDR = 0x2000118C  # u16 x10: accum avg, accum peak, pad x3, 12h, 24h, 48h, 72h, 96h
COUNTS_ADDR = 0x2000113C  # u32 total alpha decay count
TEMP_ADDR = 0x2000108C    # float32 deg C
RH_ADDR = 0x20001098      # float32 %
BATT_ADDR = 0x20000008    # u32 millivolts

latest = {"status": "starting"}


def read_device():
    # connect_mode "attach" = no reset, no halt: the detector never notices us
    with ConnectHelper.session_with_chosen_probe(
        pack=PACK,
        target_override=TARGET,
        options={"connect_mode": "attach"},
    ) as session:
        t = session.target

        def f32(addr):
            return struct.unpack("<f", struct.pack("<I", t.read32(addr)))[0]

        stats = struct.unpack("<10H", bytes(t.read_memory_block8(STATS_ADDR, 20)))
        bq = {
            "accum_avg": stats[0],
            "accum_peak": stats[1],
            "12hr": stats[5],
            "24hr": stats[6],
            "48hr": stats[7],
            "72hr": stats[8],
            "96hr": stats[9],
        }
        return {
            "status": "ok",
            "radon_bq_m3": bq,
            "radon_pci_l": {k: round(v / 37.0, 3) for k, v in bq.items()},
            "total_counts": t.read32(COUNTS_ADDR),
            "temperature_c": round(f32(TEMP_ADDR), 1),
            "humidity_pct": round(f32(RH_ADDR), 1),
            "battery_mv": t.read32(BATT_ADDR),
            "updated": int(time.time()),
        }


def poll_loop():
    global latest
    while True:
        try:
            latest = read_device()
        except Exception as exc:  # probe unplugged, device off, etc.
            latest = {**latest, "status": f"error: {exc}"}
        time.sleep(POLL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(latest).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the Zero 2W's journal quiet


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"Serving radon readings on port {HTTP_PORT}, polling every {POLL_SECONDS}s")
    HTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()
