# standing-desk-automation

**English** | [한국어](README.ko.md)

An unofficial Python script that controls a LINAK DPI1C panel from a Mac over Bluetooth.
You can change the desk height with terminal commands or Siri Shortcuts.

## Reference desk

- Controller: LINAK DPI1C
- Control box: LINAK CBD6S
- Columns (motors): LINAK DL6
- Name: `DESK 0000`
- Range: 60–125 cm

## File locations

| What | Location |
| --- | --- |
| Script (the one you run) | `~/linak/desk.py` |
| Script copy (version confirmed to connect) | `desk.py` in this repo |
| Python virtual environment (with bleak installed) | `~/linak/.venv` |
| Settings (desk address, server key) | `~/.config/linak-dpi1c/config.json` |

## Requirements

- macOS
- Python 3.9 or later
- The built-in Terminal app (terminals inside other apps may crash because of Bluetooth permission issues)

## Installation

```
mkdir -p ~/linak && mv ~/Downloads/desk.py ~/linak/
python3 -m venv ~/linak/.venv && ~/linak/.venv/bin/pip install bleak
```

`bleak` is a Python library for talking to Bluetooth Low Energy (BLE) devices.

To keep using the `desk` command, add this line to `~/.zshrc`.

```
alias desk="~/linak/.venv/bin/python ~/linak/desk.py"
```

## Initial setup

1. Disconnect any device currently connected to the desk over Bluetooth.
2. Hold the button on the back or underside of the panel for 2–3 seconds. The desk ID, such as `DESK 0000`, appears on the display. The number is different for each desk.
3. While the ID is shown, run `desk scan`, then `desk status`.
4. If a Bluetooth permission or pairing pop-up appears, allow it.

## Commands

| Command | Action |
| --- | --- |
| `desk status` | Show the current height and presets |
| `desk to 100` | Move to 100 cm |
| `desk preset 1` | Move to preset 1–4 |
| `desk stop` | Stop |
| `desk scan` | Find the desk again |
| `desk serve` | Run the server for Siri/Shortcuts |

Press Ctrl+C while the desk is moving to stop it immediately.

The script's messages are in Korean.

## Siri setup

1. Run `desk serve`. Keep the Terminal window open and don't let the Mac go to sleep.
2. Copy one of the printed URLs. Example: `http://192.168.x.x:8765/preset/2?key=xxxxxxxx`
3. In the Shortcuts app, create a new shortcut and paste the URL into a "Get Contents of URL" action.
4. Name it something like "Raise desk", and "Hey Siri, raise desk" will run it.

- An iPhone on the same Wi-Fi can use the same URL.
- The `key` at the end of the URL stops other people on the same Wi-Fi from moving your desk.
- Available paths: `status`, `to/<cm>`, `preset/<1-4>`, `stop`

## Update / uninstall

### Update

Download the new `desk.py` and overwrite the old one. Your settings and virtual environment are kept.

```
mv -f ~/Downloads/desk.py ~/linak/
```

### Uninstall

```
rm -rf ~/linak ~/.config/linak-dpi1c
```

- Remove the `alias desk=...` line you added to `~/.zshrc`.
- If the desk is listed in System Settings > Bluetooth, click ⓘ and choose "Forget This Device".
- Delete any Siri shortcuts you made.

## How it works

- The target height is sent as an absolute position, not as a distance to move. The controller compares it with the current position and picks the direction.
- The target height is sent repeatedly every 50 ms. Longer intervals make the movement stutter.
- Height data is in 0.01 cm units and input is in 0.1 cm units. In practice the desk stops within ±0.5 cm of the target.
- A move ends when:
  - The desk arrives: a stop command is sent.
  - The panel is pressed in the opposite direction: the script stops sending the target height and leaves control to the panel.
  - It takes more than 45 seconds, or the desk doesn't move for 3 seconds: a stop (`FF 00`) is sent, followed by a heartbeat (`01 80`).
- If a new command arrives during `desk serve` while the desk is moving, the desk stops, waits 2 seconds, then moves to the new target.

## Limitations

- The maximum height isn't read from the desk. It is fixed at `minimum height + 65 cm`, so the range may be wrong on desks with a different travel distance.
- Moves smaller than 0.5 cm are skipped.
- The desk is found only if its name starts with `DESK`, or if its Bluetooth advertisement (BLE advertise) includes the DPI1C service UUID or the LINAK manufacturer ID. If you rename the desk in the app, it may not be found.
- Only the DPI1C panel is supported for now. DPG1C support is planned.

## Troubleshooting

- **`desk scan` can't find the desk**: Hold the panel button for 2–3 seconds and run it again while the ID is shown.
- **`연결/통신 실패` (connection/communication failed)**: Disconnect the desk from your phone and try again as above. The error message shows which step failed.
- **Crashes right after launch**: Terminals inside other apps, such as VS Code, may crash because of Bluetooth permissions. Run it in the built-in Terminal app.
- **E16 error**: Happens when you operate the panel by hand while the script is controlling the desk. You may need to pair again.

## In progress / planned

- **Universal version**: The maximum height can't be read from the desk yet. As a first step, instead of the fixed 65 cm, it will record the highest position the desk has actually reached.
- **iPhone app**: An app that controls the desk directly from an iPhone over Bluetooth, without a Mac, and also works with Siri.
- **Mac menu bar app**: A Mac app that controls the desk right from a menu bar icon at the top right of the screen.
- **`desk serve --no-key`**: An option to run the server without a key.
- **`desk scan <number>`**: Pick a device from a list of nearby devices and save it, whatever its name.
- **DPG1C support**: Will be added separately because its protocol differs from DPI1C.

## Protocol (reference)

Uses characteristics of the BLE service `99fa0001-338a-1024-8a49-009c0215f78a`.

| Characteristic | Purpose |
| --- | --- |
| `0002` | Stop `FF 00` |
| `0011` | Read/write settings: name `7F 26 00`, minimum height `7F 81 00`, preset `7F 99 00 <slot>` |
| `0021` | Height/speed notifications `[height lo, height hi, speed lo, speed hi]` |
| `0031` | Target height `(cm - minimum height) × 100`, sent every 50 ms; heartbeat `01 80` when done |

## License

[MIT](LICENSE) © 2026 syki66

The DPI1C Bluetooth (BLE) protocol is based on [kkqq9320/ha-linak-dpi1c-control](https://github.com/kkqq9320/ha-linak-dpi1c-control) (MIT, © 2026 KKQQ).
