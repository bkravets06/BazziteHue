# BazziteHue

Control Philips Hue bulbs from the terminal over **Bluetooth LE** — no Hue Bridge, no
Wi-Fi, no Philips account, no cloud round-trip. Built for [Bazzite](https://bazzite.gg)
(and any other Fedora/Linux system with BlueZ).

Full colour control: hex, RGB, HSV/HSL, raw CIE xy, colour temperature in Kelvin or
mireds, brightness 0–100 %, software fades, saved scenes, and an interactive terminal
UI you can drive with the arrow keys.

```console
$ bazzitehue scan
AA:BB:CC:DD:EE:FF  Hue color lamp

$ bazzitehue add desk AA:BB:CC:DD:EE:FF --pair
Saved desk -> AA:BB:CC:DD:EE:FF (gamut C)
Paired.

$ bazzitehue color "#ff6a00" -b 45 --on
desk: xy(0.5843, 0.3878) ≈ #ff6a00, 45%
```

---

## Requirements

| | |
|---|---|
| OS | Bazzite, or any Linux with BlueZ 5.55+ |
| Python | 3.10 or newer (Bazzite ships one) |
| Bluetooth | Any BLE-capable adapter, including the Steam Deck's internal radio |
| Bulbs | Any Hue lamp with Bluetooth — essentially everything sold since 2019 (look for the Bluetooth logo on the box) |

Nothing is layered onto the base image with `rpm-ostree`; the tool installs into a
virtualenv under `~/.local`, which survives Bazzite image updates.

## Install

```bash
git clone https://github.com/bkravets06/BazziteHue.git
cd BazziteHue
./install.sh
```

This creates `~/.local/share/bazzitehue/venv`, links `~/.local/bin/bazzitehue`, and adds
a desktop entry so you can launch the TUI from the app grid (or from Steam's desktop
mode). Make sure `~/.local/bin` is on your `PATH`.

Alternatives, if you prefer them:

```bash
pipx install .          # or: pipx install git+https://github.com/bkravets06/BazziteHue
uv tool install .
```

## First run

1. **Put the bulb in range.** BLE is line-of-sight-ish; stay within a room.
2. **Make sure nothing else owns it.** A bulb that is currently connected to the Hue
   app or a Hue Bridge will not advertise. Power-cycle it (off/on at the switch) if the
   scan comes up empty.
3. **Scan and save:**
   ```bash
   bazzitehue scan
   bazzitehue add desk AA:BB:CC:DD:EE:FF
   ```
4. **Pair.** Hue bulbs only accept commands from a *bonded* host:
   ```bash
   bazzitehue pair desk
   ```
   If that fails, do it manually — `bluetoothctl` then `pair AA:BB:CC:DD:EE:FF`,
   `trust AA:BB:CC:DD:EE:FF`.
5. **Use it:**
   ```bash
   bazzitehue on
   bazzitehue brightness 70
   bazzitehue color teal
   ```

The first saved lamp becomes the default, so `--light` is optional from then on.

## Commands

Every command takes `-l/--light <alias|address|a,b|all>`; without it, the default lamp
is used. Flags work before or after the subcommand.

| Command | What it does |
|---|---|
| `scan [--all] [--timeout S]` | Find Hue lamps in range (`--all` lists every BLE device) |
| `add <alias> <address> [--pair] [--gamut A\|B\|C]` | Save a lamp under a short name |
| `lights` / `rm <alias>` / `use <alias>` | List, forget, or set the default lamp |
| `pair` / `unpair` | Manage the Bluetooth bond |
| `status [--full]` | Power, brightness, colour (add `--json` for scripting) |
| `info` | Model, firmware, manufacturer, and which features the bulb has |
| `on` / `off` / `toggle` | Power |
| `brightness <0-100\|+10\|-10> [--fade S] [--on]` | Absolute or relative brightness |
| `color <spec> [-b PCT] [--fade S] [--on]` | Set the colour |
| `temp <3000\|3000k\|250mired\|warm> [-b PCT]` | Set white colour temperature |
| `set [--on\|--off] [-c COLOR] [-b PCT] [--fade S]` | Change several things in one connection |
| `scene save\|apply\|list\|rm` | Store and recall looks |
| `loop [--speed S] [--saturation PCT] [--duration S]` | Cycle the colour wheel |
| `tui` | Interactive controller |
| `colors` | Print the built-in colour names with swatches |
| `gatt-dump` | Print the bulb's raw GATT table (diagnostics) |

### Colour formats

Anywhere a colour is accepted:

| Form | Example |
|---|---|
| Hex | `#ff8800`, `ff8800`, `f80` |
| RGB triple | `255,136,0` or `rgb:255,136,0` |
| HSV / HSB | `hsv:30,100,100` (hue °, sat %, value % — value sets brightness) |
| HSL | `hsl:30,100,50` |
| CIE xy | `xy:0.5,0.4` |
| Kelvin | `3000k` |
| Mireds | `250mired` |
| Named colour | `red`, `teal`, `lavender`, … (`bazzitehue colors`) |
| Named white | `candle`, `warm`, `reading`, `daylight`, … |

Colours outside what the bulb can physically produce are clamped to the nearest point
on its gamut triangle instead of being clipped per channel, so the hue is preserved.

### Examples

```bash
# A warm dim evening on every saved lamp
bazzitehue -l all set --on -c 2200k -b 20

# Slow fade to deep blue over four seconds
bazzitehue color "#0033ff" --fade 4 --on

# Nudge brightness with a hotkey binding
bazzitehue brightness +10
bazzitehue brightness -10

# Save the bulb's current look, then bring it back later
bazzitehue scene save cosy --from-light
bazzitehue scene apply cosy

# Party mode for 30 seconds
bazzitehue loop --speed 6 --duration 30

# Scripting
bazzitehue status --json | jq '.[0].brightness_percent'
```

### Interactive TUI

```bash
bazzitehue tui
```

| Key | Action |
|---|---|
| `←` `→` | Hue −/+ 5° (`H`/`L` for 20°) |
| `↑` `↓` | Brightness −/+ 5 % (`K`/`J` for 20 %) |
| `s` `S` | Saturation −/+ |
| `t` `T` | Warmer / cooler white |
| `w` / `c` | White mode / colour mode |
| `1`–`9` | Colour presets |
| `space` | Toggle power |
| `r` | Re-read state from the lamp |
| `q` | Quit |

Key presses update a local model and are pushed to the bulb ~16×/second, so holding a
key feels smooth instead of queueing hundreds of BLE writes.

## Configuration

`~/.config/bazzitehue/config.json` (override with `$BAZZITEHUE_CONFIG`):

```json
{
  "lights": {
    "desk": { "address": "AA:BB:CC:DD:EE:FF", "gamut": "C", "name": null, "model": null }
  },
  "scenes": {
    "cosy": { "color": "2200k", "brightness": 20.0, "power": true }
  },
  "default": "desk"
}
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more lamps failed (others may still have been updated) |
| 2 | Bad arguments, unknown lamp, or unparseable colour |
| 3 | The Bluetooth stack is unreachable (BlueZ down, no adapter) |
| 130 | Interrupted |

## Troubleshooting

**`scan` finds nothing.** The bulb is probably already connected to something — the Hue
app on a phone in the room, or a Hue Bridge. Close the app, or power-cycle the lamp,
then scan again. `bazzitehue scan --all` shows every BLE device, which tells you whether
the adapter is working at all.

**"refused a read/write" / insufficient authentication.** The host is not bonded. Run
`bazzitehue pair <alias>`. If BlueZ refuses, remove any stale bond first:

```bash
bluetoothctl remove AA:BB:CC:DD:EE:FF
bluetoothctl pair AA:BB:CC:DD:EE:FF
bluetoothctl trust AA:BB:CC:DD:EE:FF
```

**The Bluetooth stack is unreachable (exit code 3).**

```bash
systemctl status bluetooth
sudo systemctl enable --now bluetooth
bluetoothctl show          # should list an adapter, Powered: yes
```

**Connections drop, especially on a Steam Deck.** The internal radio shares an antenna
with Wi-Fi and with your controllers. Keeping the lamp within a few metres and avoiding
a full 2.4 GHz band helps; the tool retries connections twice by default and
`--timeout` raises the per-attempt patience.

**A command works but the bulb ignores part of it.** White-only and ambiance bulbs have
no colour characteristic — `bazzitehue info` shows what a given lamp supports, and
colour commands against a white-only bulb say so rather than failing silently.

**Something looks wrong at the protocol level.** `bazzitehue gatt-dump` prints the
lamp's real services and characteristics so you can compare them with what the tool
expects (below).

## How it works

Philips does not publish the BLE interface, so the layout below comes from community
reverse engineering and matches other open-source Hue BLE clients. It has been stable
across the Hue bulbs people have tested, but it is not an official contract — hence
`gatt-dump`.

| Characteristic | UUID | Payload |
|---|---|---|
| Power | `932c32bd-0002-47a2-835a-a8d455b859dd` | 1 byte, `0` / `1` |
| Brightness | `932c32bd-0003-47a2-835a-a8d455b859dd` | 1 byte, `1`–`254` |
| Colour temperature | `932c32bd-0004-47a2-835a-a8d455b859dd` | uint16 LE, mireds (153–500) |
| Colour | `932c32bd-0005-47a2-835a-a8d455b859dd` | 2 × uint16 LE, CIE x and y scaled by `0xFFFF` |

Lamps advertise service `0xFE0F`, which is how `scan` picks them out. Brightness `0` is
rejected by the firmware, so "off" is always the power characteristic's job. sRGB is
converted through Philips' Wide RGB D65 matrices and clamped to the bulb's gamut
triangle (gamut C by default; pass `--gamut A` or `B` when saving an older lamp).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The test suite runs without any hardware: the BLE client is replaced with a fake that
records every write, so encoding, gamut clamping, fades, ordering and error handling are
all covered offline.

```
bazzitehue/
  color.py      colour maths — sRGB ↔ xy, gamuts, Kelvin, parsing user input
  protocol.py   the GATT layout and payload encoding
  light.py      async client for one bulb (connect, read, write, fade)
  config.py     saved lamps, aliases, scenes
  cli.py        argument parsing and output
  tui.py        curses interface
```

## Licence

MIT. Not affiliated with, endorsed by, or supported by Signify/Philips. "Philips Hue" is
their trademark; this is an independent client.
