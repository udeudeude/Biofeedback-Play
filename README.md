# Biofeedback Play

A local biofeedback playground for hardware such as the Wild Divine Lightstone, Muse headband, HeartMath emWave, and future sensors.

The first working target is the Wild Divine Lightstone (USB vendor 0x14FA, product 0x0001).

## Current prototype

- Finds the Lightstone through HID
- Reassembles its fragmented 8-byte HID reports
- Parses the Lightstone RAW stream
- Displays live skin-conductance and pulse-waveform graphs in a browser
- Shows current raw values and connection state
- Starts and stops acquisition from the browser
- Records timestamped CSV files
- Sends OSC messages for SuperCollider and other creative software
- Uses no web framework or JavaScript libraries
- Includes a browser-based HID workbench for scanning devices, testing access, and making raw captures
- Saves diagnostic captures as JSON in the local captures directory

The two RAW fields were experimentally identified on the physical device:

1. first field: skin-conductance channel
2. second field: pulse / blood-volume waveform channel

## Run

From the repository:

    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python biofeedback_play.py

The app opens http://127.0.0.1:8765 automatically.

A macOS launcher is also included as Biofeedback Play.command and is committed as an executable file. After the repository is pulled, normal use should be as simple as double-clicking that launcher in Finder. It checks GitHub for a safe fast-forward update, starts the local service, and opens the browser interface. If tracked local edits are present, the launcher skips the automatic update rather than overwriting them.

## OSC

OSC is off by default. The browser interface can enable it and choose the destination host and port.

Default destination:

    127.0.0.1:57120

Messages:

    /biofeedback/lightstone/skin_raw
    /biofeedback/lightstone/pulse_raw

Each carries one integer argument.

## Recordings

CSV recordings are written into the recordings directory with columns:

    unix_time, elapsed_s, skin_raw, pulse_raw

## Architecture direction

Hardware-specific adapters should eventually feed a shared event model rather than forcing later devices to imitate the Lightstone protocol. The browser interface, recording, OSC, MIDI, and visual experiments can then consume that common stream independently.

This first prototype intentionally keeps derived physiology conservative. It exposes the raw signals first; heart-rate detection, smoothing, normalization, calibration, and signal-quality measures belong in later layers.


## Device diagnostics

The Devices & Diagnostics panel is intended to replace most one-off Terminal probing during hardware discovery.

It can:

- scan connected HID devices
- show manufacturer, product, USB vendor/product IDs, usage page, and usage
- test whether Biofeedback Play can open a selected device
- capture raw HID reports for 2, 5, or 10 seconds
- save the full capture as JSON under captures/
- copy a compact report suitable for pasting into a development chat

Raw capture is intentionally generic. Device-specific interpretation belongs in adapters once a protocol is understood.

Known devices so far:

- Wild Divine Lightstone: 0x14FA:0x0001
- HeartMath emWave Pulse Sensor: 0x0E30:0x0002
