# Biofeedback Play

Biofeedback Play is a local, device-agnostic workspace for live physiological signals, visual feedback, audio sonification, recording, diagnostics, and creative-control output.

Currently supported live devices:

- Wild Divine Lightstone, USB HID 0x14FA:0x0001
- HeartMath emWave Pulse Sensor, USB HID 0x0E30:0x0002

The architecture is intentionally organized around devices and signals so later adapters such as Muse and MindFlex can add channels without redesigning the interface.

## Interface

The browser interface has two primary tabs.

### Use devices

This is the live-feedback workspace.

Each configured signal gets its own panel. A panel identifies:

- the source device
- what signal is being displayed
- units and interpretation cautions
- current value and recent range
- total samples received
- nominal sample rate when known
- OSC address
- useful device-specific status such as emWave packet gaps

If the source device is not connected, its signal panels collapse automatically.

Each live signal panel also has its own **Audio** control. Audio is generated locally with the browser Web Audio API. It is a sonification of the changing sensor value, not a reconstructed heartbeat or diagnostic sound. Each signal can be turned on or off independently.

### Device setup

This tab contains:

- configured-device connection state
- start/stop acquisition controls per device
- USB identity and transport information
- global OSC settings
- HID scanning and diagnostics
- raw report capture

Obviously unrelated HID devices such as keyboards, trackpads, cameras, storage devices, and Touch Bar interfaces are hidden by default in diagnostics. Unknown hardware remains visible.

## Current signals

### Wild Divine Lightstone

- **Skin conductance**: raw skin-conductance channel from the two plain finger electrodes
- **Pulse waveform**: raw blood-volume pulse waveform from the gold-dot finger sensor

The raw channels were identified experimentally by sensor-removal tests. Values are kept in their original device units.

### HeartMath emWave

- **Pulse waveform**: direct 8-bit optical pulse waveform from the ear clip

Observed emWave reports have the experimental form:

    01 CC S0 S1 S2 S3 S4 S5

where CC behaves as an 8-bit packet counter and S0 through S5 behave as six consecutive waveform samples. Biofeedback Play tracks packet gaps. Display timing currently uses 375 Hz as a nominal rate derived from capture behavior rather than as a manufacturer specification.

## Recording

Recording is session-wide rather than tied to one device. Connected signals are written to one long-format CSV file under recordings/:

    unix_time, elapsed_s, device_id, signal_id, value

This format allows different devices and sample rates to coexist in one session.

## OSC

OSC is off by default. The Device setup tab can enable it and choose a host and port.

Default destination:

    127.0.0.1:57120

Current messages:

    /biofeedback/lightstone/skin_raw
    /biofeedback/lightstone/pulse_raw
    /biofeedback/emwave/pulse_raw

Each currently carries one integer argument.

A starter SuperCollider receiver is included under supercollider/.

## Device diagnostics

The HID workbench can:

- scan connected HID devices
- show manufacturer, product, USB vendor/product IDs, usage page, and usage
- test whether Biofeedback Play can open a selected device
- capture raw HID reports for 2, 5, or 10 seconds
- save full captures as JSON under captures/
- copy a compact diagnostic report

Raw capture is deliberately generic. Device-specific interpretation is added only after the protocol has evidence behind it.

## Run on macOS

The normal workflow is to double-click:

    Biofeedback Play.command

The launcher checks GitHub for a safe fast-forward update, creates the local Python environment if necessary, installs required dependencies, starts the local service, and opens the browser interface.

For manual development use:

    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python biofeedback_play.py

The interface opens at:

    http://127.0.0.1:8765

## Design direction

Hardware-specific readers feed a shared device/signal model. Visualization, audio feedback, recording, OSC, future MIDI output, and later experiments consume those signals rather than being hard-coded to one piece of hardware.

Derived physiology remains conservative. Raw signals come first; heart-rate detection, smoothing, calibration, signal quality, spectral analysis, and other derived measurements should remain explicit layers rather than silently changing source data.


## Muse 2014 / MU-01

Biofeedback Play now includes an experimental adapter for the first-generation InteraXon Muse MU-01.

The MU-01 uses classic Bluetooth RFCOMM rather than Bluetooth Low Energy. On macOS the current adapter expects the paired headband to appear as a virtual serial port under /dev/cu.*.

Device setup includes Muse-specific controls to:

- open macOS Bluetooth Settings
- scan serial ports
- identify likely Muse ports
- save the selected port
- start or stop Muse acquisition

If exactly one serial port is clearly named as a Muse, Biofeedback Play can select it automatically.

Current live Muse panels:

- EEG TP9
- EEG FP1
- EEG FP2
- EEG TP10
- head motion X
- head motion Y
- head motion Z

The EEG stream is treated as nominally 500 Hz using preset AD. Accelerometer data is treated as nominally 50 Hz. EEG microvolt scaling is explicitly experimental; accelerometer values are kept as raw signed 10-bit counts. Battery telemetry and AFE gain are shown in Device setup.

The Muse protocol adapter is based on the clean-room, MIT-licensed Muse 2014 implementation by Matthew Piercey:
https://github.com/mtpiercey/muse-2014-lsl

If modern macOS does not expose a usable RFCOMM virtual serial port after pairing, the next fallback is a native IOBluetooth bridge rather than asking the user to operate the protocol manually from Terminal.
