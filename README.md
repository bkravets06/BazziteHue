# BazziteHue

A desktop app for controlling Philips Hue bulbs over **Bluetooth LE** — no Hue Bridge, no
Wi-Fi, no Philips account, no cloud round-trip. Built for [Bazzite](https://bazzite.gg)
(and any other Fedora/Linux system with BlueZ).

It lives in the panel — the top bar on GNOME, the system tray on KDE — so bulbs are one
click away: toggle them from the menu, or open the control window for the colour wheel,
brightness, white temperature and scenes. Everything, including finding and pairing new
bulbs, is done from the app; the terminal is optional.

A full command line tool ships alongside it for scripting and keyboard shortcuts.

```console
$ ./install.sh          # then launch "BazziteHue" from your app menu
```

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [The app](#the-app)
- [Command line](#command-line)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Development](#development)

## Requirements

| | |
|---|---|
| OS | Bazzite, or any Linux with BlueZ 5.55+ |
| Python | 3.10 or newer (Bazzite ships one) |
| Bluetooth | Any BLE-capable adapter, including the Steam Deck's internal radio |
| Bulbs | Any Hue lamp with Bluetooth — essentially everything sold since 2019 (look for the Bluetooth logo on the box) |
| Desktop | KDE Plasma or GNOME (Bazzite ships either); the panel applet uses the standard StatusNotifierItem interface |

Nothing is layered onto the base image with `rpm-ostree`; the tool installs into a
virtualenv under `~/.local`, which survives Bazzite image updates.

## Install

```bash
git clone -b claude/philips-hue-bluetooth-controller-qw4idn \
    https://github.com/bkravets06/BazziteHue.git
cd BazziteHue
./install.sh
```

That creates a virtualenv under `~/.local/share/bazzitehue`, installs the app and the
CLI, adds the launcher and icon to your app menu, and starts the panel applet at login.
Nothing is layered onto the base image with `rpm-ostree`, so it survives Bazzite updates.

The app pulls in Qt (about 80 MB). If you only want the terminal tool:

```bash
./install.sh --cli-only
```

To remove everything it installed (your saved bulbs are left alone):

```bash
./install.sh --uninstall
```

## The app

Launch **BazziteHue** from the app menu, or run `bazzitehue-gui`. On first run the
control window opens and the bulb icon appears in the panel.

### Setting up a bulb

Press **Find bulbs…**, pick yours from the list, and press **Pair and add**. That saves
it and bonds with it — Hue bulbs only accept commands from a paired host. If pairing
fails, switch the bulb off and on at the wall and try again.

If a bulb does not appear, see [sharing a bulb with your phone or a
Bridge](#sharing-a-bulb-with-your-phone-or-a-bridge) below. Two escape hatches live in
the same dialog: **Show every Bluetooth device**, for a bulb that has been renamed or
does not advertise the Hue service, and **Add by address…**, for one whose address you
already know.

### Sharing a bulb with your phone or a Bridge

These are two different radios in the bulb, and they behave differently.

**A Hue Bridge talks Zigbee, not Bluetooth.** It does not occupy the Bluetooth
connection at all, so a bridged bulb can normally be driven from this app and the Bridge
at the same time. Neither side is told what the other did, so the app may show a stale
colour until it reconnects and re-reads — press the lamp picker again to refresh.

**A phone talking Bluetooth is exclusive.** A Hue bulb accepts exactly one Bluetooth
connection at a time, and while something holds it the bulb stops advertising entirely —
it cannot be scanned for or connected to. That is the bulb's firmware, and no setting
here can work around it. Whichever app got there first keeps it until it disconnects.

What this app *can* do is not be the one hogging it. It keeps a bulb connected while you
are adjusting it, then lets go after two minutes idle. If you switch between this and the
Hue phone app often, tick **Share bulbs with other apps** in the tray menu: the bulb is
released a few seconds after each change instead. Commands then take a second or two
longer, because each one reconnects first.

The command line tool always behaves this way — it connects, does the job, and
disconnects — so it never blocks a phone.

### The control window

| Control | What it does |
|---|---|
| Lamp picker | One bulb, or “all lamps” to drive them together |
| Power | Big toggle at the top; the label says what clicking will do |
| Brightness | 0–100 %, applied live as you drag |
| Colour | Hue around the wheel, saturation towards the edge, plus preset swatches and a hex box |
| White | Colour temperature from 2000 K (candle) to 6500 K (daylight), with a live preview |
| Scene | Save the current look under a name and recall it later |

Colour changes are sent as you drag, coalesced to about 16 updates a second — roughly
what a Hue bulb can absorb — so the light tracks the slider instead of lagging behind a
queue of stale values.

### The panel applet

Clicking the tray icon opens the control window. The menu has a checkbox per bulb, All
on / All off, quick brightness steps, your scenes, and **Start at login** (on by default;
untick it to stop the applet starting with your session). The icon takes on the colour of
whichever bulb is lit, and goes hollow and grey when everything is off.

Closing the control window leaves the applet running. **Quit BazziteHue** in the menu
exits for real.

The app holds a Bluetooth connection open only while you are using a bulb, and lets go
after two minutes idle — otherwise nothing else, including the Hue phone app, could
reach it.

## Command line

The CLI is installed alongside the app and shares the same saved bulbs and scenes.

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

### Interactive terminal UI

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
  "default": "desk",
  "share_mode": false
}
```

`share_mode` is the tray menu's **Share bulbs with other apps**: when true, the app
releases each bulb a few seconds after a change rather than holding it for two minutes.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more lamps failed (others may still have been updated) |
| 2 | Bad arguments, unknown lamp, or unparseable colour |
| 3 | The Bluetooth stack is unreachable (BlueZ down, no adapter) |
| 130 | Interrupted |

## Troubleshooting

**The tray icon does not appear on GNOME.** GNOME hides StatusNotifierItem icons unless
the AppIndicator extension is enabled:

```bash
gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
```

The app notices this at install time and says so. Until it is enabled, the control window
still works — the app just falls back to opening it directly.

**The launcher is missing from the app menu.** Some desktops cache the menu; log out and
back in, or run `update-desktop-database ~/.local/share/applications`.

**Nothing happens for several seconds after a click.** Connecting to a bulb takes a
moment, and the control window says `connecting…` while it does. Once connected, changes
are immediate until the app lets go of the bulb after two minutes idle.

**`scan` finds nothing.** The bulb is probably already connected to something over
Bluetooth — the Hue app on a phone in the room, most likely. A bulb with an open
connection stops advertising, so nothing can find it. Close that app, or power-cycle the
lamp, then scan again. (A Hue Bridge uses Zigbee and does not cause this.) `bazzitehue scan --all` shows every BLE device, which tells you whether
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

**The bulb is not reachable from the Hue phone app while this is running.** A BLE bulb
accepts one connection at a time. The app releases it after two minutes idle, immediately
when you quit from the tray menu, or a few seconds after each change if you tick **Share
bulbs with other apps**. See [sharing a bulb](#sharing-a-bulb-with-your-phone-or-a-bridge).

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

The test suite runs without any hardware or a display: the BLE client is replaced with a
fake that records every write, and Qt runs on its offscreen platform. Encoding, gamut
clamping, fades, CLI parsing, the curses input loop, the colour wheel geometry, the
window's controls and the worker's coalescing are all covered.

```
bazzitehue/
  color.py      colour maths — sRGB ↔ xy, gamuts, Kelvin, parsing user input
  protocol.py   the GATT layout and payload encoding
  light.py      async client for one bulb (connect, read, write, fade)
  config.py     saved lamps, aliases, scenes
  cli.py        argument parsing and output
  tui.py        curses interface
  gui/
    app.py      entry point: application, autostart, shutdown
    worker.py   asyncio loop in a thread; coalesced writes, idle disconnect
    window.py   the control window
    tray.py     panel applet and its menu
    dialogs.py  find-and-pair dialog
    widgets.py  colour wheel, temperature bar, preview
    icons.py    icons drawn with QPainter
packaging/      .desktop entry and app icon
```

## Licence

MIT. Not affiliated with, endorsed by, or supported by Signify/Philips. "Philips Hue" is
their trademark; this is an independent client.
