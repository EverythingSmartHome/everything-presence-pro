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

## USB flashing on 1.9 - was broken, now fixed in hardware

**Original board.** Once any firmware was running, esptool could not put the
board into UART download mode. Every attempt failed with:

    Failed to connect to ESP32: Wrong boot mode detected (0x13)!
    The chip needs to be in download mode.

Reproduced consistently, both idle and in a fast reboot loop - not a timing
race a retry would fix. Holding GPIO0 low for 1.5s, thirty times esptool's
~50ms window, still strapped high and booted the application. Recovery was over
the network (`esphome upload <config>.yaml --device <ip>`), which worked
reliably; ESPHome's safe mode brings OTA up even after a boot loop, so units
were never bricked.

**After the hardware fix, measured with `scripts/download-mode-test.py`:**

| Test | Trials | Misses |
|---|---|---|
| Download-mode entry, application running with ethernet up | 40 | 0 |
| Full flash at 2 Mbps | 10 | 0 |

Full flashes take ~7.6s at 2 Mbps. No boot-mode regression from the change: a
further 200-cycle hard-reset soak after the fix was 200/200 clean, and ethernet
still links at 100M full duplex with the PHY reset on GPIO0.

### Testing this correctly

Each trial must boot the application and wait for ethernet to claim GPIO0 and
drive it high **before** attempting download mode. Against a freshly-reset or
blank chip the test passes trivially even on a broken board, because GPIO0 is
not being driven yet. `download-mode-test.py` does this warm-up by default.

Note also that the auto-reset circuit is cross-coupled: asserting DTR and RTS
together leaves both EN and GPIO0 high. They must never overlap - assert RTS
alone to reset, then DTR alone for GPIO0. `scripts/enter-download.py` follows
esptool's classic sequence with longer holds, and was what established the
original fault was not a timing problem.

## Results

Measured on ESPHome 2026.6.4 / ESP-IDF 5.5.4, ESP32 rev3.1.

### 1.9

**Ethernet works.** Link comes up at 100Mbit full duplex and DHCP resolves
(10.4.12.160). `esp_eth_driver_install` succeeds with the PHY reset on GPIO0;
driver setup takes ~1.9s. 132 further link-ups were observed across the soak.

**Boot mode: 633 resets, zero failures.**

| Test | Resets | Normal flash boot | Download mode |
|---|---|---|---|
| Hard reset (EN pulse, randomised 0.35-5.0s) | 250 | 250 | 0 |
| Software + watchdog (randomised 40-6000ms) | 183 | 183 | 0 |
| Hard reset, re-run after the GPIO0 hardware fix | 200 | 200 | 0 |
| **Total** | **633** | **633** | **0** |

The last row is a regression check: the flashing fix changes the GPIO0 net,
which is the strapping pin, so making it pullable-low could in principle have
made it marginal at reset. It did not.

The software run included **44 watchdog aborts** (confirmed in the log as
`task_wdt: Task watchdog got triggered ... Aborting ... Rebooting`), which is
the abrupt no-shutdown path where GPIO0 is still being driven as the chip
re-straps. All 183 reported `rst:0xc (SW_CPU_RESET)`; the watchdog path lands
there too because the panic handler restarts the chip.

**Verdict for 1.9: no boot-mode problem found.** That is consistent with the
mechanism - GPIO0 is low for only 150us per boot and driven high the rest of
the time - rather than just being a lucky run. The remaining exposure is a
reset landing inside that 150us window, which at 433 samples this test would
not be expected to hit; the argument for 1.9 being safe rests on the mechanism,
with the soak confirming nothing else is going on.

### 1.9a

**Boot mode: 200/200 hard resets, zero download-mode boots.** Resets were timed
to land 2.5-7.0s after boot, deliberately later than the ~1.9s ethernet setup,
so every one of them happened with GPIO17 asserted and the oscillator running.
That is the case the revision was suspected to fail.

Two independent pieces of evidence say the oscillator is properly gated off
while the chip is in reset, which is why this passes:

- 200/200 clean boots with the oscillator running before each reset.
- USB flashing works reliably on 1.9a. esptool can only enter download mode by
  pulling GPIO0 low, which it could not do if the oscillator were driving that
  net at reset. (Contrast 1.9, where esptool cannot enter download mode at
  all.)

**Ethernet does NOT work on 1.9a: the link glitches constantly and DHCP never
completes.**

This is a real hardware fault, unrelated to boot mode. The board never obtains
an IP; the switch shows no lease for it.

The decisive measurement is ESPHome's ethernet event log, which sits at VERBOSE
and so is invisible at the normal DEBUG level. With it enabled, over a ~105s
window on otherwise stock firmware:

    22 x [Ethernet event] ETH connected
    21 x [Ethernet event] ETH disconnected

Link up for roughly 1.5-4s, down for roughly 1.5-2s, continuously. Every
disconnect restarts DHCP, so DHCP never gets to finish, which is why ESPHome
logs `Connecting failed; reconnecting` every 15s forever.

What the PHY registers say, and what each fact rules out:

| Register | Value | Meaning |
|---|---|---|
| PHYID1/2 (2,3) | `0x0007` / `0xC0F1` | genuine LAN8720A, OUI 0x1F0 - MDIO fully working |
| SPMODE (18) | `0x60E0` | PHYAD = 0 (matches config), MODE = 7 (all-capable, auto-neg) - strapping correct |
| MCSR (17) | `0x0002` | ENERGYON = 1 - energy present on the line, cable is live |
| ANLPAR (5) | `0xD141` | link partner ability received - the PHY *hears* the switch |
| BMSR (1) | `0x782D` when up | link up, auto-negotiation complete |
| SCSR (31) | `0x1058` | auto-neg done, speed indication 6 = **100M full duplex** |

So the following are cleared of suspicion: cable and switch port (the same ones
the 1.9 board used successfully), magnetics and differential pairs (energy
detected, partner ability received), PHY identity and address, PHY mode
strapping, MDIO, and the ESP32 side (MAC init succeeds, which requires a live
50MHz on GPIO0). Auto-negotiation completes correctly at 100M full duplex.

### Note on measuring this: BMSR link status latches low

BMSR bit 2 is latching-low per 802.3 - it goes low on *any* link failure and
stays low until read, then reports live state. This matters two ways:

- ESP-IDF's `lan87xx_update_link_duplex_speed()` reads BMSR **once** per poll
  (~2s timer) and uses the bit directly, so a brief glitch anywhere in that
  window is reported as a full disconnect.
- A diagnostic that reads BMSR twice and takes the second value sees *live*
  state, and so reports the link as healthy. Doing that at 250ms intervals also
  clears the latch constantly, which masks the very glitches being hunted.

An earlier version of this document quoted flap dwell times taken while the
diagnostic was also writing BMCR (forcing link modes, soft-resetting the PHY).
Those numbers were contaminated and have been replaced by the event-log figures
above, which come from stock firmware with no PHY writes at all.

Likewise, "forced 100M and forced 10M do not link" should not be read as
evidence about the clock: forcing a fixed mode against an auto-negotiating
switch is unreliable by design (the partner falls back to parallel detection),
so that test was inconclusive.

**Leading hypothesis: marginal physical layer on the 1.9a-specific change, the
external 50MHz clock.** Auto-negotiation uses slow FLP pulses and tolerates a
poor clock, which is why it completes and reports 100M full duplex; sustaining
a 100BASE-TX link needs an accurate, low-jitter reference, which is where brief
errors would show up. This is a hypothesis, not a proven cause - the evidence
establishes that the fault is physical-layer and 1.9a-specific, not that the
clock is definitely at fault. To confirm or eliminate it:

1. Frequency, amplitude and jitter of the oscillator **measured at the PHY's
   XTAL1/CLKIN pin**, not just at the oscillator output.
2. Termination on the clock net. One oscillator driving two loads (ESP32 GPIO0
   and the PHY) needs proper series termination; reflections show up as jitter.
3. The PHY's REF_CLK mode strap (nINTSEL). If the PHY is strapped for REF_CLK
   **Out** mode it will try to drive 50MHz onto the same net the external
   oscillator drives - contention would produce exactly this symptom.
4. PHY supply rails under load.

Until that is resolved, 1.9a is not usable as an ethernet board, even though
its boot-mode behaviour is sound.
