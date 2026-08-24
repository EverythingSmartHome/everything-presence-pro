#!/usr/bin/env python3
"""Force an Everything Presence Pro 1.9 prototype into UART download mode.

On rev 1.9 the LAN8720A reset line shares GPIO0 with the ESP32 boot strap, so
GPIO0 is no longer a quiet pin owned by the USB-serial auto-reset circuit. Once
firmware is running, the ethernet driver holds GPIO0 driven HIGH, and whatever
else sits on that net (a PHY reset RC, a pull-up) has to be overcome by the
adapter's auto-reset transistor within esptool's short assertion window.

esptool's default reset holds GPIO0 low for only ~50ms around the EN release.
If that is not enough, esptool reports:

    Failed to connect to ESP32: Wrong boot mode detected (0x13)!
    The chip needs to be in download mode.

This script does the same reset but holds GPIO0 low far longer on both sides of
the EN pulse, then leaves the chip sitting in download mode so a subsequent
esptool/esphome invocation can use --before no-reset.

Usage
  python enter-download.py --port COM15 [--hold 1.5] [--en 0.3]
  esphome upload <config>.yaml --device COM15      # or esptool --before no-reset
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True, help="e.g. COM15")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--hold", type=float, default=1.5,
                   help="seconds to hold GPIO0 low either side of the EN pulse")
    p.add_argument("--en", type=float, default=0.3,
                   help="seconds to hold EN low")
    args = p.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.2) as port:
        def set_dtr(state: bool) -> None:
            port.dtr = state
            port.rts = port.rts  # usbser.sys: setting one line can clobber the other

        def set_rts(state: bool) -> None:
            port.rts = state
            port.dtr = port.dtr

        set_dtr(False)
        set_rts(False)
        time.sleep(0.1)
        port.reset_input_buffer()   # flush BEFORE the reset, so the ROM banner survives

        # The standard two-transistor auto-reset circuit is cross-coupled:
        # asserting DTR and RTS together leaves BOTH EN and GPIO0 high. So the
        # two must never overlap - assert RTS alone for reset, then DTR alone
        # for GPIO0. This is esptool's classic_reset, just with longer holds.
        set_rts(True)          # EN low - chip in reset, pads high-impedance
        time.sleep(args.en)

        set_dtr(True)          # GPIO0 low ...
        set_rts(False)         # ... and EN high, so the chip straps GPIO0 low

        time.sleep(args.hold)  # keep GPIO0 low through strapping and ROM start
        set_dtr(False)         # release GPIO0; ROM is already in download mode

        time.sleep(0.3)
        banner = port.read(2048).decode("utf-8", errors="replace")

    # Windows consoles are often cp1252; strip anything it cannot encode so a
    # noisy line never crashes the tool before it reports its verdict.
    safe = banner.encode("ascii", "replace").decode("ascii")
    print(safe.strip() or "(no output)")
    if "DOWNLOAD_BOOT" in banner or "waiting for download" in banner:
        print("\nOK: chip is in UART download mode.")
        return 0
    if "SPI_FAST_FLASH_BOOT" in banner or "SPI_FLASH_BOOT" in banner:
        print("\nFAILED: chip booted the application instead. GPIO0 did not read "
              "low at strapping - try a longer --hold.")
        return 1
    print("\nUNKNOWN: no recognisable ROM banner captured.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
