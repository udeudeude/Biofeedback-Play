# Biofeedback Play

Biofeedback Play is a local, device-agnostic workspace for live physiological signals, visual feedback, audio sonification, recording, diagnostics, derived metrics, and creative-control output.

Currently supported live devices:

- Wild Divine Lightstone, USB HID 0x14FA:0x0001
- HeartMath emWave USB Pulse Sensor, USB HID 0x0E30:0x0002
- InteraXon Muse 2014 / MU-01, classic Bluetooth serial

Up to four emWave USB modules can be opened simultaneously. Additional units are presented as emWave 2, emWave 3, and emWave 4 for the current session.

## Interface

The browser interface has two primary tabs.

### Live data

This is the live-feedback workspace.

Only **live** device signals are shown here. Signals are grouped first by physical source, so connecting an emWave creates one clearly labeled emWave section rather than adding cards to an undifferentiated wall.

Inside each device section:

- **Direct sensor data** comes first and is visually dominant. This is what the hardware itself is sending.
- **Derived from this sensor** follows in smaller cards. These are calculations made from the direct signal.
- Each card leads with the current value and a one-line plain-language statement of what it signifies.
- Technical material such as recent range, sample count, OSC address, detailed definition, and nominal rate is tucked under **Details & technical information**.
- Cross-device calculations live in their own **Cross-device comparisons** section rather than being mixed into the source-device groups.

Disconnected devices remain available under **Device setup** instead of occupying the live workspace. The **Device views** row uses persistent iOS-style switches so each configured source can be included or excluded even while unplugged. All four emWave slots are available before connection.

The optional camera experiment sits below the live signal groups and remains compact while off. Its full camera workspace appears only after the camera is started. Turning the Camera view off also stops a running browser-camera session so it cannot consume processor time invisibly.

The top bar includes a persistent **Light mode / Dark mode** switch. The selected theme is remembered in the browser.

Each live panel has a speaker icon that toggles sonification. Audio is generated locally with the Web Audio API and stops automatically if the source disappears. Panels have three persistent sizes: **Standard** uses the normal derived-panel width, **Mini** keeps that compact width with a reduced-height live graph, and **Wide** spans the width used by direct sensor panels. Direct sensor streams default to Wide; derived and comparison panels default to Standard.

### Visual language

The live workspace uses a stronger hierarchy:

- each physical input device gets a distinct section with its own accent color and device description
- direct hardware streams are labeled **Direct sensor**, use the strongest visual treatment, and span the section width
- calculations from one device are labeled **Derived** and appear as smaller secondary cards
- cross-device calculations are labeled **Comparison** and live in a separate validation section
- one-line meanings remain visible at a glance; deeper definitions and transport details stay collapsed
- the toolbar can filter the workspace to Everything, Direct sensor data, Derived, or Comparisons

The goal is that source, provenance, and meaning are apparent before detailed reading is necessary.

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

A local capture produced about 371 samples/second. HeartMath documentation for emWave Pro Plus specifies a 370 Hz pulse-wave sample rate, so Biofeedback Play uses 370 Hz as the nominal emWave rate. Live acquisition does not use USB report-arrival jitter as beat timing: it reconstructs sample time from the rolling packet counter and six-sample packet structure, slowly estimates each module's long-run rate, and reserves time for detected missing packets. Identical modules also keep stable HID-path session slots so unplugging one does not silently relabel another sensor.

### Muse 2014 / MU-01

- EEG TP9
- EEG FP1
- EEG FP2
- EEG TP10
- head motion X
- head motion Y
- head motion Z

The current Muse adapter treats EEG as nominally 500 Hz and accelerometer data as nominally 50 Hz. EEG microvolt scaling remains explicitly experimental; accelerometer values are preserved as raw signed counts.

Live Muse data also includes a battery meter, an experimental four-electrode contact indicator based on recent EEG spread, and a combined Delta/Theta/Alpha/Beta/Gamma history graph in logarithmic power. These display aids do not change the recorded raw EEG or the separate band-power signals.

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

The coherence panels are open calculations based on the concentration of HRV spectral power around a dominant peak in the coherence range. They are not presented as HeartMath's proprietary emWave coherence score. Advanced beat-to-beat metrics are withheld when beat confidence is low or the newest interval sequence contains a timing artifact; bad intervals are not silently discarded and stitched around.

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

The **Camera lab** on the Live data tab can:

- request the Mac camera only after the user presses Start camera
- keep video entirely in the local browser page
- show one mirrored preview with adjustable forehead and lower-cheek sampling guides
- reject very dark, clipped, and extreme-color pixels from those guided regions
- combine recent red, green, and blue changes with a POS-style remote-PPG transform
- band-pass that waveform around approximately 0.7–3 Hz
- compute a frame-to-frame facial-motion waveform as an artifact channel
- show a local periodicity-versus-motion quality estimate
- optionally switch that same preview to heartbeat-band color magnification inside the guided skin regions
- adjust guide size and horizontal/vertical placement without moving the user or computer

For now the camera workspace is deliberately in **tuning mode**. It shows only the signals useful for deciding whether extraction is improving:

- pulse waveform
- facial motion
- signal quality
- pulse amplitude
- conservative spectral heart-rate estimate

Beat-to-beat interval, HRV, coherence, respiration, and related camera-derived panels are intentionally hidden until camera timing can be validated against a contact pulse sensor. Camera heart rate is currently estimated from the dominant recent pulse-wave frequency and is withheld when the quality score is too low, rather than always emitting a physiological-looking number.

When Lightstone or any emWave is live at the same time, camera comparison panels are available for validation, but their calculations update only while the camera quality gate is satisfied. These comparisons are intended to validate the camera pipeline before richer camera physiology is re-enabled.

This is intentionally transparent and experimental. The quality percentage is a software signal-quality heuristic, not a medical confidence score.


### Muse macOS transport

On macOS, Biofeedback Play can use either a legacy `/dev/cu.Muse-*` / `/dev/tty.Muse-*` serial endpoint or Apple's native IOBluetooth RFCOMM API. Device setup scans both serial endpoints and paired classic-Bluetooth devices, and prefers a uniquely identified paired Muse over stale legacy serial endpoints.

The native Muse transport runs in a small helper subprocess so Apple's IOBluetooth connection lifecycle executes on that process's main thread. This mirrors the standalone Terminal path that successfully opened the user's MU-01 RN-iAP channel when the same call repeatedly timed out from Biofeedback Play's background acquisition thread.

Muse connection attempts keep a visible trace of transport stages, RFCOMM channels, command framing, short text responses, and failures. The trace can be copied from Device setup. Biofeedback Play tries the CRLF command framing used by the current open-source Muse 2014 LSL implementation first, then CR as a fallback, and probes an opened channel for already-streaming Muse packets before sending configuration commands.

### Live graph performance

High-rate sensors such as the Muse continue to be acquired and recorded at full device rate. The browser display is intentionally cheaper: live panels poll at 5 Hz, offscreen panels are not polled or repainted unless their audio is active, Retina canvas resolution is capped, and dense graph histories are reduced to a spike-preserving display trace before drawing. These changes affect visualization only, not the underlying acquisition or recording rate.

### Readable graphs and band audio

Mini cards place the channel title above the graph. Graphs show elapsed time,
vertical values, and units. Dense traces preserve both extrema and their original
timestamps. Robust range uses the displayed trace's 5th–95th percentiles and marks
clipped peaks at the plot edges; turn it off to see the full range. Individual EEG
band plots default to logarithmic power and can switch to linear power. Display
scaling does not alter audio input, recording, or OSC values.

The five band cards are folded by default, in Delta → Theta → Alpha → Beta → Gamma
order. Each band has an independent audio button beside the combined graph, so
alpha, beta, and all other bands remain audible with their cards folded. Band
sonification maps recent log power to pitch; it does not play the EEG frequency
itself. Session controls include master volume, Mute all, and Pause graphs.
Pausing drawing continues acquisition, recording, OSC, numerical values, and audio.

EEG power and RMS use all four electrodes; frontal alpha asymmetry uses FP1 and
FP2. The interface names those contributors and flags uncertain contact without
silently excluding channels or changing calculations. Flatlines are marked unknown.
Battery charge at or below 10% is highlighted; the age of the last telemetry is
shown, with readings older than 30 seconds marked stale.

### Export and SuperCollider

Start recording in Live data. Download CSV and Metadata appear after the first
recording starts, and remain available after it stops. The metadata JSON preserves
signal IDs, units, descriptions, rates, OSC addresses, and the EEG contributor policy.
CSV remains the same five-column long format. Recordings now also include Muse
battery percentage and per-electrode contact spread. Recording filenames are unique
for rapid successive sessions.

In Device setup → OSC output, download the SuperCollider receiver, evaluate its
parenthesized block in SuperCollider, then enable OSC to `127.0.0.1:57120` and Apply.
The receiver handles all five band powers, raw EEG, motion, battery, and contact
spread. Band powers remain **linear µV²** on OSC and in CSV; the sample includes a
dB conversion. OSC is UDP: enabling a destination does not confirm a receiver is
listening. Raw EEG remains full rate and contact metadata is experimental.

- Schema: `GET /api/signal_schema`
- CSV: `GET /api/recording_download`
- Metadata: `GET /api/recording_download?format=json`
- Receiver: `GET /api/supercollider_receiver`
- Contact: `/biofeedback/muse/contact/{tp9,fp1,fp2,tp10}/spread_uv`
- Battery: `/biofeedback/muse/battery_percent`

Verification: `python -m unittest discover -s tests -q` and `node tests/test_ui.js`.
The JavaScript checks exercise graph and audio logic with a simulated browser API;
Safari layout, speaker output, and physical devices require a Mac check.

### Workspace and lifecycle improvements

- Find signal searches names, device names, and IDs. Empty filtered views offer Clear filters.
- Jump to device, Open camera, Compact panels, and Reset panel layout reduce navigation through many streams.
- Keyboard arrow keys switch tabs; tabs and filters expose selected state to assistive technology.
- Derived values older than three seconds show Waiting for valid data and stop sonification; battery uses a 30-second telemetry window.
- Filtered-out audio continues receiving data. Service loss mutes audio, shows recovery instructions, and reconnects automatically. A new service session resets graph cursors.
- Camera startup can be cancelled; late camera permission responses and playback failures release tracks. Camera errors offer specific recovery instructions.
- Diagnostics preserve selection across scans, disable concurrent work, show timed capture progress, and copy the currently displayed report. Testing/capturing requires stopping live acquisition on all emWave modules.
- Quit app in Device setup closes recording and device workers. A second launcher invocation opens the existing app without starting competing sensor workers.
- EEG broadband RMS now centers each electrode separately so differing electrode DC offsets are not counted as EEG fluctuations.

Optional browser regressions use Playwright: `node tests/test_browser.cjs`.
Set `BIOFEEDBACK_BROWSER_EXECUTABLE` if using a custom Chromium binary. The fixture uses synthetic hardware and fake camera input; it does not connect to physical devices.
See `docs/UXReview.md` for the scope and validation of this review.
