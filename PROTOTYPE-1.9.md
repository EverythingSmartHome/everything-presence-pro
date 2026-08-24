# Prototype firmware: board revisions 1.9 and 1.9a

Prototype-only builds. **Do not merge to `main` and do not publish to the
update channel.** Both revisions change the ethernet PHY wiring in ways that
touch GPIO0, the ESP32 strapping pin that selects flash boot vs UART download
boot, so both need a boot-mode soak before they can be trusted.

## What changed

| | 1.8 (production) | 1.9 | 1.9a |
|---|---|---|---|
| RMII 50MHz clock | ESP32 generates, out on **GPIO17** | ESP32 generates, out on **GPIO17** | **external oscillator, in on GPIO0** |
| LAN8720A reset | tied to board reset network | **GPIO0** | **GPIO17** |
| MDC / MDIO | GPIO23 / GPIO18 | unchanged | unchanged |
| PHY address | 0 | unchanged | unchanged |

Config files:

- `common/ethernet-base-1.9.yaml`, `common/ethernet-base-1.9a.yaml`
- `everything-presence-pro-ethernet-1.9.yaml`, `everything-presence-pro-ethernet-1.9a.yaml`

Each prototype build uses its own device name (`ep-pro-proto-19`,
`ep-pro-proto-19a`) and a deliberately dead prototype manifest URL, so a
prototype cannot collide with production devices on the network and cannot
pull a production image. A 1.8 image on a 1.9 board loses ethernet (no PHY
reset); a 1.8 image on a 1.9a board drives GPIO17 as a clock output straight
into the oscillator gate. Never press **Apply Update** on a prototype.

## Why boot mode is the risk

GPIO0 must read HIGH when the chip leaves reset. If it reads LOW, the ROM
enters UART download mode and prints:

```
rst:0xc (SW_CPU_RESET),boot:0x3 (DOWNLOAD_BOOT(UART0/UART1/SDIO_FEI_REO_V2))
waiting for download
```

instead of the normal:

```
rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
```

The device then sits there forever and looks bricked.

- **1.9** — ESP-IDF drives GPIO0 LOW while resetting the PHY (measured at
  150us, see below). Any reset landing inside that window re-straps the chip
  while the pin is low. The exposure is a very narrow timing window, so it
  needs many resets at randomised offsets to find, not a handful.
- **1.9a** — GPIO0 carries a live 50MHz square wave whenever the external
  oscillator is enabled. If the oscillator is running as the chip leaves
  reset, the strapping sample is effectively a coin flip. The design relies on
  GPIO17 sitting low (oscillator gated off) until firmware asserts it, so what
  matters is whether GPIO17 is genuinely low through every reset path.

An EN-pin reset cannot reproduce the 1.9 fault on its own: while EN is low all
pads go high-impedance, so the board pull-up decides the strapping and the ESP
cannot hold GPIO0 down. Only resets where the ESP32 keeps driving its pins —
software restart, watchdog, panic, brownout — can. **Both reset classes must be
tested.**

## What the firmware actually does with these pins

Read out of ESP-IDF 5.5.4 (`components/esp_eth`), because it decides how big
the risk really is.

**The PHY reset is deasserted before the MAC is initialised.**
`esp_eth_driver_install()` runs, in order:

```c
phy->reset_hw(phy);   // drive reset GPIO low, wait, drive high
mac->init(mac);       // reset/init the EMAC - this is what needs the RMII clock
phy->init(phy);
```

with an IDF comment stating this ordering exists specifically "for PHY whose
internal PLL has been configured to generate RMII clock, but is put in reset
state during power up". **This is what makes 1.9a viable**: if GPIO17 gates the
oscillator, the oscillator is already running by the time the MAC needs a
clock. Had the order been reversed, 1.9a could not have worked at all without
firmware changes.

**The 1.9 GPIO0-low window is 150 microseconds.**
`LAN87XX_PHY_RESET_ASSERTION_TIME_US` is 150 (`esp_eth_phy_lan87xx.c:18`), and
`esp_eth_phy_802_3_reset_hw()` drives the pin low, waits exactly that long,
then drives it high and leaves it driven high for the rest of the run. So on
1.9 GPIO0 is low for ~150us out of a ~2s startup - about 0.008% of the boot -
and sits at the safe HIGH level the entire rest of the time. That is a real
exposure but a very small one, and it means a 1.9 unit is only vulnerable to a
reset that happens to land inside that 150us window.

This also tells us what would make 1.9 dangerous: anything that leaves GPIO0
low for longer. Raising `hw_reset_assert_time_us`, or a board-level pull-down
on GPIO0, would widen the window proportionally.

## Test harness

`scripts/boot-soak.py` drives resets over the USB-serial adapter and
classifies every ROM boot banner. Exit code 0 = pass, 1 = a download-mode boot
was seen, 2 = inconclusive (no banners captured).

### Hard-reset soak — covers power-on / external reset

Runs against the normal prototype firmware. Pulses EN via RTS with a
randomised gap so resets land at every point in startup.

```
esphome upload everything-presence-pro-ethernet-1.9.yaml --device COM15
python scripts/boot-soak.py hardreset --port COM15 --cycles 250 \
    --settle 0.35 --settle-max 5.0 --log soak-hardreset.log
```

### Software / watchdog soak — covers the reset paths that matter most

Uses `test-boot-soak-1.9.yaml`, which layers `common/boot-soak-test.yaml` on
top of the identical prototype config. The device reboots itself at a random
delay in `[soak_min_ms, soak_max_ms]`, which spans PHY reset, ethernet
bring-up and steady-state running. The restart is a raw `arch_restart()` with
no component teardown,
because a graceful shutdown would deinit the ethernet driver and release
GPIO0 — hiding the exact fault being hunted.

Every `soak_wdt_every`-th boot it instead blocks the main loop to starve the
task watchdog, producing an abrupt reset with no shutdown path at all.

```
esphome upload test-boot-soak-1.9.yaml --device COM15
python scripts/boot-soak.py monitor --port COM15 --boots 300 --log soak-monitor.log
```

Soak knobs live in the substitutions block of `test-boot-soak-*.yaml`:

| Substitution | Default | Meaning |
|---|---|---|
| `soak_min_ms` / `soak_max_ms` | 40 / 6000 | randomised delay before self-restart |
| `soak_wdt_every` | 4 | force a watchdog reset every Nth boot; 0 disables |
| `soak_toggle_relay` | false | drive the relay high on odd boots |

`soak_toggle_relay` exists because GPIO12, the relay output, is the ESP32
`VDD_SDIO` strapping pin: it must be low at reset or the chip straps its flash
rail to 1.8V and fails to boot. That hazard is inherited from 1.8 rather than
introduced by 1.9, but it is worth a pass with the relay energised.

**Remember to reflash the normal prototype firmware afterwards** — a soak
build reboots forever by design.

## Results

See the PR description for measured results.
