# Biofeedback Play

Biofeedback Play is a local, device-agnostic workspace for live physiological signals, visual feedback, audio sonification, recording, diagnostics, derived metrics, and creative-control output.

Currently supported live devices:

- Wild Divine Lightstone, USB HID 0x14FA:0x0001
- HeartMath emWave USB Pulse Sensor, USB HID 0x0E30:0x0002
- InteraXon Muse 2014 / MU-01, classic Bluetooth serial

Up to four emWave USB modules can be opened simultaneously. Additional units are presented as emWave 2, emWave 3, and emWave 4 for the current session.

## Interface

The browser interface has two primary tabs.

### Use devices

This is the live-feedback workspace.

Each raw or derived signal gets its own panel. Panels identify:

- the source device or device pair
- what is being measured or calculated
- units and interpretation cautions
- current value and recent range
- total samples received
- nominal update/sample rate when known
- OSC address
- useful device-specific status such as emWave packet gaps

If the required source device is not connected, the panel collapses automatically.

Each live panel has an independent **Audio** control. Audio is generated locally with the Web Audio API. It is sonification, not a reconstructed heartbeat or diagnostic sound.

### Visual language

The live workspace uses a consistent visual grammar:

- each input device has its own accent color, reused on signal panels, source chips, setup cards, and graph traces
- direct sensor streams carry a **Direct** badge and solid graph line
- single-device calculations carry a **Calculated** badge and dashed graph line
- cross-device calculations carry a **Cross-device** badge and a multi-device accent stripe
- panels explicitly list every device that contributes data
- live panels sort ahead of disconnected panels
- disconnected panels stay compact instead of stretching to match a neighboring live panel
- the toolbar can filter the workspace to All, Direct, Calculated, or Comparisons
- panels can be rearranged by dragging their ⠿ handle; the custom order is remembered in the browser and can be reset from the toolbar

These distinctions are intentionally secondary to the signal names and values: they should help orientation without turning the dashboard into a color-key puzzle.

### Device setup

This tab contains:

- configured-device connection state
- start/stop acquisition controls per device
- USB/Bluetooth identity and transport information
- Muse serial-port setup
- global OSC settings
- HID scanning and diagnostics
- raw report capture
- notes for multi-emWave experiments

Obviously unrelated HID devices such as keyboards, trackpads, cameras, storage devices, and Touch Bar interfaces are hidden by default in diagnostics. Unknown hardware remains visible.

## Raw signals

### Wild Divine Lightstone

- raw skin conductance
- raw pulse / blood-volume waveform

### HeartMath emWave

- raw 8-bit optical pulse waveform from each connected module

Observed emWave reports have the form:

    01 CC S0 S1 S2 S3 S4 S5

where CC behaves as an 8-bit packet counter and S0 through S5 behave as six consecutive waveform samples.

A local capture produced about 371 samples/second. HeartMath documentation for emWave Pro Plus specifies a 370 Hz pulse-wave sample rate, so Biofeedback Play now uses 370 Hz as the nominal emWave rate.

### Muse 2014 / MU-01

- EEG TP9
- EEG FP1
- EEG FP2
- EEG TP10
- head motion X
- head motion Y
- head motion Z

The current Muse adapter treats EEG as nominally 500 Hz and accelerometer data as nominally 50 Hz. EEG microvolt scaling remains explicitly experimental; accelerometer values are preserved as raw signed counts.

## Derived physiology panels

### Pulse-derived panels

For Lightstone and every connected emWave:

- heart rate
- inter-beat interval
- HRV RMSSD
- HRV SDNN
- pNN50
- open coherence ratio
- coherence peak share
- experimental breathing-rate estimate from HRV
- pulse amplitude
- beat-detection confidence

The coherence panels are open calculations based on the concentration of HRV spectral power around a dominant peak in the coherence range. They are not presented as HeartMath's proprietary emWave coherence score.

### Skin-conductance panels

From Lightstone:

- tonic skin level
- phasic skin activity
- recent skin-conductance trend
- relative phasic response rate
- skin-conductance variability

Because the Lightstone stream is not calibrated to microsiemens, Biofeedback Play keeps these measures in raw device units and uses relative thresholds rather than pretending they are standardized EDA measurements.

### Muse-derived panels

When Muse is connected:

- delta power
- theta power
- alpha power
- beta power
- gamma power
- frontal alpha asymmetry
- broadband EEG RMS
- head-motion intensity

These are signal features, not mind-reading labels. Biofeedback Play deliberately does not rename them “peace,” “focus,” “stress,” or similar psychological states.

## Multiple pulse sensors

When more than one pulse sensor is live, comparison panels appear automatically.

Current pairwise measures include:

- heart-rate difference
- median beat timing offset
- waveform correlation
- relative raw pulse amplitude

This supports experiments such as:

- one emWave on each earlobe
- emWave ear sensor plus a compatible finger sensor
- Lightstone pulse sensor versus emWave
- multiple emWave sensors on different people for exploratory rhythm comparisons

The timing-offset panel is intentionally **not** labeled pulse-transit time. USB scheduling, device buffering, optical sensor latency, and placement all contribute to the measured offset.

## Recording

Recording is session-wide. Raw and derived signals are written into one long-format CSV file under recordings/:

    unix_time, elapsed_s, device_id, signal_id, value

This allows devices with different sample rates to coexist in one session.

## OSC

OSC is off by default. Device setup can enable it and select the destination host and port.

Default destination:

    127.0.0.1:57120

Every signal definition has an OSC address, including derived measures and additional emWave units.

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

The launcher checks GitHub for a safe fast-forward update, creates the local Python environment if necessary, installs dependencies, starts the local service, and opens the browser interface.

For manual development use:

    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python biofeedback_play.py

The interface opens at:

    http://127.0.0.1:8765

## Design principle

Hardware-specific readers feed a shared device/signal model. Visualization, audio feedback, recording, OSC, diagnostics, and derived physiology consume those signals rather than being hard-coded to one device.

Raw measurements remain visible beside derived quantities. Derived metrics are explicitly labeled where calibration, window length, or algorithmic assumptions limit interpretation.


## Camera / remote pulse

Biofeedback Play includes a local browser-camera experiment inspired by remote photoplethysmography and video color magnification.

The **Camera lab** on the Use devices tab can:

- request the Mac camera only after the user presses Start camera
- keep video entirely in the local browser page
- show a normal mirrored preview with forehead and lower-cheek sampling guides
- reject very dark, clipped, and extreme-color pixels from those guided regions
- combine recent red, green, and blue changes with a POS-style remote-PPG transform
- band-pass that waveform around approximately 0.7–3 Hz
- compute a frame-to-frame facial-motion waveform as an artifact channel
- show a local periodicity-versus-motion quality estimate
- optionally show heartbeat-band color magnification inside the guided skin regions
- leave the expensive magnified view off by default to reduce browser CPU use

For now the camera workspace is deliberately in **tuning mode**. It shows only the signals useful for deciding whether extraction is improving:

- pulse waveform
- facial motion
- signal quality
- pulse amplitude
- conservative spectral heart-rate estimate

Beat-to-beat interval, HRV, coherence, respiration, and related camera-derived panels are intentionally hidden until camera timing can be validated against a contact pulse sensor. Camera heart rate is currently estimated from the dominant recent pulse-wave frequency and is withheld when the quality score is too low, rather than always emitting a physiological-looking number.

When Lightstone or any emWave is live at the same time, camera comparison panels become available only while the camera quality gate is satisfied. These comparisons are intended for validation of the camera pipeline before richer camera physiology is re-enabled.

This is intentionally transparent and experimental. The quality percentage is a software signal-quality heuristic, not a medical confidence score.
