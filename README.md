# radon-detector

Reverse-engineering a cheap consumer radon detector (AEG-Test HOUND-1011S)
to report live radon readings into Home Assistant without buying a $200+
"connected" radon monitor or commercial HVAC device.

No public docs existed for this device's internals. This repo documents what we
found to save others the effort (and tokens). Claude was used to decode RAM and flash, write demo scripts, and co-author this repo.

**TL;DR:** the MCU has exposed SWD pads with no read protection. You can
dump the firmware and read live sensor values (radon, temp,
humidity, battery) straight out of RAM over SWD while the device keeps
running normally. SWD memory reads don't halt a Cortex-M CPU, so this is a non-invasive integration.

## The device

- **Sold as:** AEG-Test HOUND-1011S radon detector (~$50)
- **PCB silkscreen:** `RADON_RM-61-V0.0`
- **MCU:** Fudan Micro **FM33LC043N** (FM33LC0xx family — Cortex-M0+, built-in display controller)
- **Sensing:** silicon PIN photodiode alpha detector with a high-voltage bias
  boost converter. The display's "Total Counts" is the number of
  alpha decay events observed by the detector. The device also has an onboard T/RH sensor.

## Getting in: SWD

Test pad pins on the PCB listed below. See image for locations:

- **SWDIO = MCU pin 1** SW debug input/output
- **SWCLK = MCU pin 32** SW clock input
- **VDD = MCU pin 28** 3.3v system power in (system power can come from here, battery, or USB-C
- **NRST = MCU pin 2** reset pin to reset device if needed (optional), default hi (3.3v) pull low via GPIO to reset
- **GND = PCB common ground** Common ground - must be shared with picoprobe and/or other connected device

Attach any CMSIS-DAP probe. Simplest (and what we did here for initial testing) is to flash a **Raspberry Pi Pico (any model, we used a 2W) with
[debugprobe](https://github.com/raspberrypi/debugprobe)** (GP2 = SWCLK,
GP3 = SWDIO, GND to Pico GND), then run pyocd with the Keil `FM33LC0xx` CMSIS device pack (available as a local `.pack` file here: [arm-keil pack downloads](https://www.keil.arm.com/subfamily/fmsh-fm33lc0xx-series-fm33lc0xx/)) in terminal on a PC connected to the pico via USB.

Photo of the hound's SWD pad pinout:

<img width="642" height="666" alt="image" src="https://github.com/user-attachments/assets/a0c9819b-b0ad-4d80-91c8-dcf14cc268c8" />



## High-level findings:

- No flash read protection. 256 KB flash dumps clean. Valid Cortex-M image: initial SP `0x20002700`,
  reset vector `0x000001C9`. Flash is nearly full; most of it is LCD
  glyph/bitmap tables (screen is a full-color ~200×400 panel).
- RAM is 24 KB (`0x20000000`–`0x20005FFF`; reads at `0x20008000` bus-fault).
- No ASLR (it's an M0+ bare-metal image) — RAM addresses are stable across
  power cycles. Confirmed by matching live reads against later dumps.

## How the firmware stores radon values

Values are stored as integers in Bq/m³. The pCi/L display value is derived
by dividing by 37 at display time. Observed readings confirm this:

- Alarm threshold "3.998 pCi/L" (the weird .998!) = exactly 148 Bq/m³ ÷ 37
- Display 0.405 pCi/L = 15 Bq/m³ ÷ 37
- Display 0.432 pCi/L = 16 Bq/m³ ÷ 37 (and the device's own Bq/m³ unit mode
  shows 16)

The device tracks 7 radon figures: **Latest 12hr** (the main-screen number),
**Accumulated AVG** and **Accumulated Peak** (since power-on), plus **Latest
24hr / 48hr / 72hr / 96hr** windows (each populates only after enough runtime).

## RAM map (FM33LC043N, HOUND-1011S firmware)

Method: repeated 24 KB RAM snapshots via SWD, diffed as 32-bit words, with
physical stimuli to confirm (breathing on the temp/RH sensor, watching the
display, battery drain over hours).

### Misc Values

| Address | Type | Meaning | Evidence |
|---|---|---|---|
| `0x2000108C` | float32 LE | Temperature °C | tracked display; rose 26.2→30.3 after breathing on sensor |
| `0x20001098` | float32 LE | Relative humidity % | tracked display; 49.7→69.6 after breathing |
| `0x20001120` | float32 LE | Temp °C, 2nd copy | init sentinel 65535.0 until first reading |
| `0x2000112C` | float32 LE | RH %, 2nd copy | init sentinel 255.0 until first reading |
| `0x20000008` | u32 | Battery/VDD in mV | ~3045, slow monotonic decline (3V lithium) |
| `0x20000230`, `0x20001068` | u16 | Alarm threshold, Bq/m³ | 148 = the default "3.998 pCi/L" setting |

### Radon values

All seven radon statistics live in one contiguous **u16 array** starting at
`0x2000118C` — every entry matched the on-device stats screen one-for-one:

| Address | Type | Meaning |
|---|---|---|
| `0x2000118C` | u16 | Accumulated AVG, Bq/m³ |
| `0x2000118E` | u16 | Accumulated Peak, Bq/m³ (monotonic non-decreasing) |
| `0x20001190`–`0x20001194` | u16 ×3 | padding / unidentified (usually 0) |
| `0x20001196` | u16 | Latest 12hr, Bq/m³ |
| `0x20001198` | u16 | Latest 24hr, Bq/m³ |
| `0x2000119A` | u16 | Latest 48hr, Bq/m³ |
| `0x2000119C` | u16 | Latest 72hr, Bq/m³ |
| `0x2000119E` | u16 | Latest 96hr, Bq/m³ |

**One 20-byte read at `0x2000118C` fetches every radon stat.** That's the whole
poller.

Other values of interest:

| Address | Type | Meaning | Evidence |
|---|---|---|---|
| `0x20001078` | u32 | **Latest 12hr, Bq/m³** — the main-screen value (u32 copy of the array entry) | tracked display 0→15→16; later diverged from the accumulated avg, confirming it's the 12hr window |
| `0x2000113C` | u32 | **Total decay count** (copy at `0x200010A8`) | tracked display 0→1→4→16 |
| `0x20001090` | u32 | Constant **37** — the Bq/m³→pCi/L divisor, sitting right next to the radon values | static |
| `0x2000107C` | u32 | Constant 600 (could be measurement interval in seconds?) | static |

**Amusing detail:** the conversion constant 37 living at `0x20001090` means the
firmware really does store integer Bq/m³ and divide by 37 for the North
American display.

**A gotcha that cost us days:** early in a run the main-screen value equals the
accumulated average (a 12hr window ≈ all data when uptime < 12h), so we
mislabeled `0x20001078` as "accumulated average" at first. Only a diff after
several days of uptime, when the windows diverged, exposed the truth. If you're
mapping RAM on a slow-integrating sensor, confirm labels across a multi-day
run before trusting them.

**Misc:** `0x20000014`, `0x20000058`, `0x20000060` are runtime tick counters
(~47/s).

**Recommended poll list:** one 20-byte block read at `0x2000118C` (all seven
radon stats), `0x2000113C` (counts), `0x2000108C` (temp), `0x20001098` (RH),
`0x20000008` (battery mV).

## Lock function & Admin menu

The device has a button lock and a PIN-protected Admin menu:

**"Locked" state** — The firmware string notes:
*"Please press both the left and right buttons simultaneously to unlock."*
Hold left + right together to lock/unlock the buttons.

**Admin menu password** (factory/service menu — Set Slope Factor cal coefficient, Quick Detection Mode, and Reset And Shutdown): 4-digit PIN. On this firmware (v2.0.03) the
default PIN is 0018.

How we found the PIN: typing digits on the entry screen lands them in an ASCII
buffer at RAM `0x200002F9` (with an "entry active" flag byte at `0x200002F8`).
Cross-referencing that address in the firmware leads to the check routine,
which `memcmp`s the four typed characters against a hardcoded string literal
`"0018"` sitting in flash — match branches to unlock, mismatch shows "ERROR".
No hashing, no per-device secret.

### What's in the Admin menu

**Slope Factor** — a two-segment piecewise calibration gain applied to the
output concentration (the menu title is literally "Set Slope Factor (Bq/m³)").
There's one gain for the **0-500 Bq/m³** band and another for **>500 Bq/m³**;
cheap alpha detectors go nonlinear at high radon, so this lets a calibration
lab trim each range against a reference chamber. Both default to **1.00**
(identity = factory-uncorrected). Stored in RAM as u16 fixed-point ×100:

| Address | Type | Meaning | Value |
|---|---|---|---|
| `0x200010A0` | u16 | Slope factor, 0-500 Bq/m³ band | 100 = 1.00 |
| `0x200010A2` | u16 | Slope factor, >500 Bq/m³ band | 100 = 1.00 |

(These match the `100,100` pair in the flash `menu_parameter` defaults blob.)

**Quick Detection Mode** — a boolean that overrides the device's normal ~12h
averaging window (the UI has "Testing" / "Wait" / "Latest 12h" states) for
faster factory/bench testing. Toggling it On set a flag at RAM `0x2000018C`
(0 → 1). Useful for quicker readings
at the cost of noisier averages. This appears to reduce the normal 12hr warmup time to 1 hour before publishing a reading on the main display.

**Reset And Shutdown** — self-explanatory.

## Replicating the basic RAM polling setup

1. Open the case (small philips screws), find the SWD test pads, see photo earlier in readme (SWDIO→pin 1, SWCLK→pin 32 on the FM33LC043N; verify continuity). Recommend disconnecting and removing the battery before soldering. Power can be supplied via the battery, USB connection, or both.
3. Flash debugprobe onto the Pi Pico; wire GP2→SWCLK, GP3→SWDIO, GND→GND. First power on the sensor by holding the middle button, then connect pico to PC with USB cable. (We tried powering on the Pico first but that locked us from powering on the device)
4. On the PC in terminal: `pip install pyocd` (requires python and having python in system PATH), download the Keil FM33LC0xx DFP pack to a project folder, cd to the project folder then:
   `pyocd cmd --pack FMSH.FM33LC0XX_DFP.3.0.4.pack -t fm33lc04x` and use `savemem 0x20000000 0x6000 C:\full\project\folder\path\ram.bin` for RAM snapshots, `read32 0x20001078` for spot reads, or `savemem 0x00000000 0x40000 C:\full\project\folder\path\fm33_dump.bin` for FW flash dumps
5. Diff snapshots against display changes to confirm addresses on your firmware version (they may differ across FW builds)

**⚠️ "SWD works but everything reads zero":** after any power loss the hound
does **not** resume measuring on its own — you must hold the middle button to
actually turn it on. Until then the MCU still runs housekeeping (battery mV
updates, SWD attaches fine) but the whole sensor block stays zeroed and the
temp/RH init (65535/255) never appear.

Notes: pyocd target name is fm33lc04x, and update the version number as needed for your pack file i.e. `FMSH.FM33LC0XX_DFP.version_here.pack`

## Home automation integration setup (with pico in the loop)

```
PCB SWD, GND & 3.3v pads →
PicoProbe GP2, GP3, GND & 3.3v →
Raspberry Pi USB (or other connected device, NRST optional) →
pyocd polls SWD RAM addresses →
Flask API (or whatever data polling/logging function) →
home automation gateway of choice (i.e. Home Assistant REST sensor)
```

Poll every few minutes. The MCU is never halted, functions
(display, alarm, on-device logging) are untouched, and calibration stays factory. Radon,
temperature, humidity, and battery voltage all come along for free.

**Headless mounting tips:** for a permanent enclosure install the display can
be removed (it's only an output — the firmware runs fine without it) and the
buzzer can be desoldered so the radon alarm doesn't sound from inside a box.
Neither affects measurement. Remember the middle-button power-on quirk above
before you seal the enclosure — after a battery swap you'll need to reach it.

### Sample: Pi Zero 2W poller → Home Assistant REST sensor

[`scripts/radon_ha_poller.py`](scripts/radon_ha_poller.py) is a minimal working
example (~80 lines, stdlib + pyocd): it polls the RAM addresses above every 5
minutes and serves the readings as JSON on port 8080. Runs fine on a Pi Zero 2W
with the Pico debugprobe on USB.

```
pip install pyocd
python3 radon_ha_poller.py   # then: curl http://<pi-ip>:8080/
```

Home Assistant YAML config (added to `configuration.yaml`):

```yaml
rest:
  - resource: http://<pi-ip>:8080/
    scan_interval: 300
    sensor:
      - name: "Radon latest 12hr"
        value_template: "{{ value_json.radon_pci_l['12hr'] }}"
        unit_of_measurement: "pCi/L"
      - name: "Radon accumulated average"
        value_template: "{{ value_json.radon_pci_l.accum_avg }}"
        unit_of_measurement: "pCi/L"
      - name: "Radon detector temperature"
        value_template: "{{ value_json.temperature_c }}"
        unit_of_measurement: "°C"
        device_class: temperature
      - name: "Radon detector battery"
        value_template: "{{ value_json.battery_mv }}"
        unit_of_measurement: "mV"
```

(Prefer Bq/m³? Use `value_json.radon_bq_m3[...]` instead — the JSON carries
both.)

### No-picoprobe option: SWD directly from Pi GPIOs

```
PCB SWD, GND & 3.3v pads →
Raspberry Pi GPIO, GND & 3.3v (or other connected device, NRST optional) →
OpenOCD on the Pi polls SWD RAM addresses →
Flask API (or whatever data polling/logging function) →
home automation gateway of choice (i.e. Home Assistant REST sensor)
```

To save some HW, skip the microcontroller: OpenOCD's `linuxgpiod` driver bitbangs SWD
directly on a Raspberry Pi's GPIO header (it's the same mechanism RPi's own
docs use to flash a Pico *from* a Pi — here just pointed at the hound instead).
Four wires: **GPIO24 → SWDIO, GPIO25 → SWCLK, GND → GND, VDD → 3.3v**. No pack file
needed.

One trap: OpenOCD's `cortex_m` target fails examination on this chip
(`Cortex-M CPUID: 0x410cc300 is unrecognized`) and then refuses all memory
reads. The fix is a `mem_ap` target — raw reads through the debug
port, no CPU examination at all. The cfg
in this repo already does this.

```
sudo apt install openocd                          # needs 0.12+
sudo openocd -f scripts/hound-openocd.cfg &       # leave running (systemd unit ideal)
python3 scripts/radon_ha_poller_gpio.py           # same JSON on :8080
```

[`scripts/hound-openocd.cfg`](scripts/hound-openocd.cfg) has the wiring and
adapter config; [`scripts/radon_ha_poller_gpio.py`](scripts/radon_ha_poller_gpio.py)
is the poller — it queries OpenOCD's Tcl port instead of pyocd and serves the
identical JSON, so the Home Assistant config above works the same way with or without the picoprobe.

Caveats:

- Verified on hardware (Pi Zero 2W, OpenOCD 0.12, Debian bookworm):
- Recommend tying 5V power from the Pi/sbc/microcontroller directly to the USB C port pads on the radon sensor, though 3.3V through the VDD pad should be OK.
- Keep leads short (<10 cm); bitbanged SWD is less forgiving than a real probe.
- `sudo` is needed for gpiochip access (or add your user to the `gpio` group).
- `linuxgpiod` works on any Pi. If you prefer the legacy
  `bcm2835gpio` driver, you must set the right peripheral base for your model
  (Zero 2W / Pi 3 = 0x3F000000, Pi 4 = 0xFE000000, Pi 1 / Zero = 0x20000000).

Alternatively (future work) — decompile the dumped FW with ghidra and write new
custom FW (flash is unprotected and writable). Pros: Device is fully mutable, potential custom display readout, repurpose device as both radon sensor and small air quality display. Risks: losing factory calibration, need to reimplement the analog front end handling. Our approach (RAM polling) gets home automation integration without risk of bricking the device (though re-flashing stock FW is possible).

---

*Reverse-engineered with a multimeter, a Pi Pico, Claude, and stubbornness. No vendor
docs were harmed (or consulted — there weren't any).*
