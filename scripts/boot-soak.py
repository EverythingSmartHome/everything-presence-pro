#!/usr/bin/env python3
"""Boot-mode soak tester for the Everything Presence Pro 1.9 / 1.9a prototypes.

Both prototype revisions put something on GPIO0, which is the ESP32 strapping
pin that selects flash boot vs UART download boot:

  1.9   GPIO0 = LAN8720A nRST. ESP-IDF drives it LOW for 150us while
        resetting the PHY, then leaves it driven HIGH. A reset landing in
        that 150us window straps the chip into download mode.
  1.9a  GPIO0 = 50MHz RMII clock IN from an external oscillator. If the
        oscillator is live when the chip leaves reset, the strapping sample
        is effectively random.

Either way the symptom is the same: the ROM prints DOWNLOAD_BOOT instead of
SPI_FAST_FLASH_BOOT and the device sits in "waiting for download" forever.
This script drives resets and classifies every ROM boot banner it sees.

Modes
  monitor    Passive. Pair with a test-boot-soak-*.yaml build, which reboots
             itself at a randomised delay. Best coverage of SOFTWARE resets,
             which is the case where the ESP32 can still be driving GPIO0 low
             as the chip re-straps.
  hardreset  Active. Pulses EN via RTS N times, like a power cycle. Covers
             the POR / external-reset path. Note this cannot reproduce a
             1.9 ESP-held-low fault (pads go hi-Z while EN is low), so run
             monitor mode too.

Usage
  python boot-soak.py monitor   --port COM15 --boots 300
  python boot-soak.py hardreset --port COM15 --cycles 200
"""

from __future__ import annotations

import argparse
import datetime as _dt
import random
import re
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

# ROM banner, e.g.
#   rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
#   rst:0x1 (POWERON_RESET),boot:0x3 (DOWNLOAD_BOOT(UART0/UART1/SDIO_FEI_REO_V2))
ROM_BANNER = re.compile(
    r"rst:(0x[0-9a-fA-F]+)\s*\(([^)]*)\),\s*boot:(0x[0-9a-fA-F]+)\s*\((.+)$"
)
DOWNLOAD_HINT = re.compile(r"waiting for download|DOWNLOAD_BOOT", re.I)
APP_ALIVE = re.compile(r"SOAK-BOOT n=(\d+)|ESPHome version")
ETH_UP = re.compile(r"IP Address|link up|Connected to", re.I)


def _now() -> str:
    return _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class Tally:
    def __init__(self) -> None:
        self.banners = []  # list of (rst_reason, boot_mode)
        self.download_boots = 0
        self.normal_boots = 0
        self.app_starts = 0
        self.eth_up = 0
        self.other_boots = []

    @property
    def total(self) -> int:
        return len(self.banners)

    def add_banner(self, rst_reason: str, boot_mode: str) -> str:
        self.banners.append((rst_reason, boot_mode))
        upper = boot_mode.upper()
        if "DOWNLOAD_BOOT" in upper:
            self.download_boots += 1
            return "DOWNLOAD"
        if "FLASH_BOOT" in upper:
            self.normal_boots += 1
            return "OK"
        self.other_boots.append(boot_mode)
        return "OTHER"


def hard_reset(port) -> None:
    """esptool-style EN pulse.

    DTR stays deasserted so GPIO0 is never pulled low by the auto-reset
    circuit; we want the board wiring alone to decide the strapping.
    """

    def set_rts(state: bool) -> None:
        port.rts = state
        # usbser.sys workaround: setting one line can clobber the other
        port.dtr = port.dtr

    def set_dtr(state: bool) -> None:
        port.dtr = state
        port.rts = port.rts

    set_dtr(False)  # GPIO0 released -> normal boot requested
    set_rts(True)   # EN low, chip held in reset
    time.sleep(0.1)
    set_rts(False)  # EN high, chip boots


def run(args) -> int:
    tally = Tally()
    logfile = open(args.log, "a", encoding="utf-8", errors="replace") if args.log else None

    def emit(line: str) -> None:
        if logfile:
            logfile.write("%s %s\n" % (_now(), line))
            logfile.flush()

    print("Opening %s @ %d in %s mode" % (args.port, args.baud, args.mode))
    with serial.Serial(args.port, args.baud, timeout=0.2) as port:
        port.dtr = False
        port.rts = False
        time.sleep(0.2)
        port.reset_input_buffer()

        def next_settle() -> float:
            """Seconds to wait before the next EN pulse.

            Randomised across the range so resets land at every point in
            startup rather than only once the device has settled.
            """
            hi = args.settle_max if args.settle_max is not None else args.settle
            if hi <= args.settle:
                return args.settle
            return random.uniform(args.settle, hi)

        deadline = time.time() + args.timeout
        cycles_done = 0
        cycle_deadline = 0.0
        if args.mode == "hardreset":
            hard_reset(port)
            cycles_done = 1
            cycle_deadline = time.time() + next_settle()

        target = args.boots if args.mode == "monitor" else args.cycles
        last_banner_at = time.time()
        buf = b""

        while time.time() < deadline:
            chunk = port.read(4096)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")
                    if not line:
                        continue
                    emit(line)
                    if args.verbose:
                        print("  | " + line)

                    m = ROM_BANNER.search(line)
                    if m:
                        verdict = tally.add_banner(m.group(2), m.group(4))
                        last_banner_at = time.time()
                        marker = "  <-- STUCK IN DOWNLOAD MODE" if verdict == "DOWNLOAD" else ""
                        print("[%s] boot #%4d  %-8s rst=%s boot=%s%s" % (
                            _now(), tally.total, verdict, m.group(2), m.group(4)[:40], marker))
                        if verdict == "DOWNLOAD" and args.stop_on_fail:
                            print("\nStopping on first failure (--stop-on-fail).")
                            deadline = 0
                            break
                    elif DOWNLOAD_HINT.search(line):
                        print("[%s] ROM download prompt: %s" % (_now(), line.strip()))
                    elif APP_ALIVE.search(line):
                        tally.app_starts += 1
                    elif ETH_UP.search(line):
                        tally.eth_up += 1

            if args.mode == "hardreset":
                if time.time() >= cycle_deadline:
                    if cycles_done >= target:
                        break
                    hard_reset(port)
                    cycles_done += 1
                    cycle_deadline = time.time() + next_settle()
            else:
                if tally.total >= target:
                    break
                # A soak build that has gone quiet is itself a failure signal.
                if time.time() - last_banner_at > args.stall:
                    print("\n[%s] No ROM banner for %ss - device is not rebooting. It is "
                          "either stuck in download mode, hung, or the soak firmware is "
                          "not running." % (_now(), args.stall))
                    break

    if logfile:
        logfile.close()

    print("\n" + "=" * 68)
    print("  mode                 %s" % args.mode)
    print("  boots observed       %d" % tally.total)
    print("  normal flash boots   %d" % tally.normal_boots)
    print("  DOWNLOAD-mode boots  %d" % tally.download_boots)
    if tally.other_boots:
        print("  unclassified boots   %d  %s" % (len(tally.other_boots), set(tally.other_boots)))
    print("  app start markers    %d" % tally.app_starts)
    print("  network-up markers   %d" % tally.eth_up)
    reasons = {}
    for rst, _mode in tally.banners:
        reasons[rst] = reasons.get(rst, 0) + 1
    print("  reset reasons        %s" % reasons)
    print("=" * 68)

    if tally.total == 0:
        print("RESULT: INCONCLUSIVE - no ROM boot banners captured at all.")
        return 2
    if tally.download_boots:
        pct = 100.0 * tally.download_boots / tally.total
        print("RESULT: FAIL - %d/%d boots (%.2f%%) strapped into UART download mode."
              % (tally.download_boots, tally.total, pct))
        return 1
    print("RESULT: PASS - %d/%d boots reached flash boot." % (tally.total, tally.total))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["monitor", "hardreset"])
    p.add_argument("--port", required=True, help="e.g. COM15")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--boots", type=int, default=200, help="monitor: boots to observe")
    p.add_argument("--cycles", type=int, default=200, help="hardreset: EN pulses to issue")
    p.add_argument("--settle", type=float, default=3.0,
                   help="hardreset: seconds between EN pulses (lower bound)")
    p.add_argument("--settle-max", type=float, default=None,
                   help="hardreset: if set, wait a random time in "
                        "[--settle, --settle-max] between EN pulses")
    p.add_argument("--stall", type=float, default=45.0,
                   help="monitor: seconds without a boot banner before giving up")
    p.add_argument("--timeout", type=float, default=7200.0, help="overall wall-clock cap")
    p.add_argument("--log", default=None, help="append raw serial lines to this file")
    p.add_argument("--stop-on-fail", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="echo every serial line")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
