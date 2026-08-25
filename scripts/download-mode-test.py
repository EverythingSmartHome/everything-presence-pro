#!/usr/bin/env python3
"""Measure how reliably a board can be put into UART download mode.

Written for the 1.9 prototype, where GPIO0 doubles as the LAN8720A reset line.
The original 1.9 board could not be put into download mode at all once firmware
was running: esptool failed every time with

    Failed to connect to ESP32: Wrong boot mode detected (0x13)!

The failure only appears once the ethernet driver has taken GPIO0 and is
driving it high, so each trial deliberately boots the application and waits for
ethernet to come up BEFORE attempting to enter download mode. Testing against a
freshly-reset or blank chip would pass trivially and prove nothing.

Each trial:
  1. Pulse EN via RTS to boot the application.
  2. Wait --warmup seconds so ethernet setup completes and GPIO0 is driven high.
  3. Run esptool and see whether it can enter download mode.

Modes:
  entry   esptool read-mac - exercises exactly the reset-into-download-mode
          sequence that was failing, and is fast enough for a large sample.
  flash   a real write-flash of the built images - slower, end-to-end proof.

Usage
  python download-mode-test.py --port COM15 --trials 40
  python download-mode-test.py --port COM15 --trials 5 --mode flash \\
      --build-dir .esphome/build/ep-pro-proto-19/.pioenvs/ep-pro-proto-19 \\
      --baud 2000000
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")


def boot_application(port_name: str) -> None:
    """EN pulse only. DTR stays deasserted so GPIO0 is never pulled low here -
    we want the board to boot its application normally."""
    with serial.Serial(port_name, 115200, timeout=0.2) as port:
        def set_rts(state: bool) -> None:
            port.rts = state
            port.dtr = port.dtr  # usbser.sys: setting one line can clobber the other

        port.dtr = False
        set_rts(True)
        time.sleep(0.1)
        set_rts(False)


def esptool_cmd(args, build_dir: str | None) -> list[str]:
    base = ["esptool", "--port", args.port, "--chip", "esp32",
            "--before", "default-reset", "--baud", str(args.baud)]
    if args.mode == "entry":
        return base + ["--after", "no-reset", "read-mac"]

    images = [
        ("0x1000", "bootloader.bin"),
        ("0x8000", "partitions.bin"),
        ("0x9000", "ota_data_initial.bin"),
        ("0x10000", "firmware.bin"),
    ]
    cmd = base + ["--after", "hard-reset", "write-flash", "-z", "--flash-size", "detect"]
    for offset, name in images:
        path = os.path.join(build_dir, name)
        if not os.path.isfile(path):
            sys.exit("missing image: %s" % path)
        cmd += [offset, path]
    return cmd


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--trials", type=int, default=40)
    p.add_argument("--warmup", type=float, default=4.0,
                   help="seconds to let the app boot and ethernet claim GPIO0")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("--mode", choices=["entry", "flash"], default="entry")
    p.add_argument("--build-dir", default=None, help="required for --mode flash")
    p.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args()

    if args.mode == "flash" and not args.build_dir:
        sys.exit("--mode flash requires --build-dir")

    cmd = esptool_cmd(args, args.build_dir)
    print("mode=%s baud=%d warmup=%.1fs trials=%d" % (
        args.mode, args.baud, args.warmup, args.trials))
    print("cmd: %s\n" % " ".join(cmd))

    misses = 0
    wrong_boot_mode = 0
    durations = []
    failures = []

    for i in range(1, args.trials + 1):
        boot_application(args.port)
        time.sleep(args.warmup)

        started = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=args.timeout)
            out = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            out = "(esptool timed out)"
            ok = False
        elapsed = time.time() - started

        if ok:
            durations.append(elapsed)
            print("trial %3d/%d  OK    %.1fs" % (i, args.trials, elapsed))
        else:
            misses += 1
            if "Wrong boot mode detected" in out:
                wrong_boot_mode += 1
                reason = "Wrong boot mode detected"
            else:
                reason = next((ln.strip() for ln in out.splitlines()
                               if "rror" in ln or "ailed" in ln), "unknown")
            failures.append((i, reason))
            print("trial %3d/%d  MISS  %.1fs  %s" % (i, args.trials, elapsed, reason))

    print("\n" + "=" * 64)
    print("  mode                       %s" % args.mode)
    print("  baud                       %d" % args.baud)
    print("  trials                     %d" % args.trials)
    print("  successes                  %d" % (args.trials - misses))
    print("  MISSES                     %d" % misses)
    print("  of which wrong-boot-mode   %d" % wrong_boot_mode)
    if durations:
        print("  time  min/avg/max          %.1f / %.1f / %.1f s" % (
            min(durations), sum(durations) / len(durations), max(durations)))
    if failures:
        print("  failing trials:")
        for idx, reason in failures:
            print("    #%d  %s" % (idx, reason))
    print("=" * 64)

    if misses:
        print("RESULT: FAIL - %d/%d attempts (%.1f%%) could not enter download mode."
              % (misses, args.trials, 100.0 * misses / args.trials))
        return 1
    print("RESULT: PASS - %d/%d attempts entered download mode." % (args.trials, args.trials))
    return 0


if __name__ == "__main__":
    sys.exit(main())
