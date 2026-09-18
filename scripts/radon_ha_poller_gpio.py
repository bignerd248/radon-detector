#!/usr/bin/env python3
"""
radon_ha_poller_gpio.py — no-probe variant of radon_ha_poller.py.

Instead of a Pi Pico debugprobe + pyocd, this reads the HOUND-1011S RAM
through OpenOCD bitbanging SWD directly on the Pi's GPIOs (bcm2835gpio
driver). Wiring and OpenOCD setup are in hound-openocd.cfg.

Run OpenOCD first (leave it running, e.g. as a systemd unit):
    sudo openocd -f hound-openocd.cfg

Then:
    python3 radon_ha_poller_gpio.py
    curl http://<pi-ip>:8080/

Serves the exact same JSON as the pyocd version, so the Home Assistant
REST sensor config in the README works unchanged. Memory reads go through
the debug access port while the CPU runs — the detector is never halted.

RAM addresses are for firmware v2.0.03 — verify against your own dumps
first; they may shift between firmware builds.
"""
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OPENOCD_HOST = "127.0.0.1"
OPENOCD_TCL_PORT = 6666   # OpenOCD's Tcl RPC port (default)
POLL_SECONDS = 300        # radon moves slowly; be gentle
HTTP_PORT = 8080

STATS_ADDR = 0x2000118C  # u16 x10: accum avg, accum peak, pad x3, 12h, 24h, 48h, 72h, 96h
COUNTS_ADDR = 0x2000113C  # u32 total alpha decay count
TEMP_ADDR = 0x2000108C    # float32 deg C
RH_ADDR = 0x20001098      # float32 %
BATT_ADDR = 0x20000008    # u32 millivolts

latest = {"status": "starting"}


class OpenOCD:
    """Minimal OpenOCD Tcl RPC client (commands terminated by 0x1a)."""

    def __init__(self, host=OPENOCD_HOST, port=OPENOCD_TCL_PORT):
        self.sock = socket.create_connection((host, port), timeout=10)

    def cmd(self, command):
        self.sock.sendall(command.encode() + b"\x1a")
        buf = b""
        while not buf.endswith(b"\x1a"):
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("OpenOCD closed the connection")
            buf += chunk
        return buf[:-1].decode().strip()

    def read_memory(self, addr, width, count):
        """OpenOCD 0.12+ `read_memory` returns space-separated hex values."""
        out = self.cmd(f"read_memory 0x{addr:08X} {width} {count}")
        return [int(v, 16) for v in out.split()]

    def close(self):
        self.sock.close()


def read_device():
    ocd = OpenOCD()
    try:
        stats = ocd.read_memory(STATS_ADDR, 16, 10)
        counts = ocd.read_memory(COUNTS_ADDR, 32, 1)[0]
        temp_raw = ocd.read_memory(TEMP_ADDR, 32, 1)[0]
        rh_raw = ocd.read_memory(RH_ADDR, 32, 1)[0]
        batt = ocd.read_memory(BATT_ADDR, 32, 1)[0]
    finally:
        ocd.close()

    def f32(raw):
        return struct.unpack("<f", struct.pack("<I", raw))[0]

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
        "total_counts": counts,
        "temperature_c": round(f32(temp_raw), 1),
        "humidity_pct": round(f32(rh_raw), 1),
        "battery_mv": batt,
        "updated": int(time.time()),
    }


def poll_loop():
    global latest
    while True:
        try:
            latest = read_device()
        except Exception as exc:  # openocd down, wiring loose, etc.
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
