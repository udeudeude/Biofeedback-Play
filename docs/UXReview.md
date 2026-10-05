# Biofeedback Play UI/UX and reliability review

Scope: both app tabs, every device card (Lightstone, four emWave slots, Muse, browser camera), direct/derived/comparison signals, individual and combined EEG graphs, audio, recording, OSC, diagnostics, themes, navigation, and launch/quit/reconnection behavior.

| Area | Finding and action | Verification |
| --- | --- | --- |
| Workspace | Added signal search, actionable empty states, device jumps, camera shortcut, compact-all and reset-layout controls | Running-browser interaction checks |
| Graphs | Compact layout, axes/units, time-preserving extrema, robust ranges, optional logarithmic EEG bands | Prior graph regressions plus desktop/mobile visual review |
| Audio | Kept all five bands available; added volume/mute; corrected filtered-out audio polling; mute stale values and service outages | Browser audio state and sample-cursor checks; physical speakers require Mac check |
| Derived results | Mark unavailable/stale features explicitly; corrected RMS to remove each electrode's own baseline | Python freshness and unequal-offset RMS regressions |
| Muse setup | Retain unsaved selection and previously saved connection; avoid simultaneous scans | Code review and shared setup/browser checks |
| USB setup | Start/stop controls for each module; diagnostics guard every live emWave slot | Slot-specific Python regression; browser setup checks |
| Camera | Cancel pending startup, release late/failed/ended tracks, clear video object, prevent overlapping data uploads, actionable permission/device errors | Real browser with fake media; denial/cancellation/stop paths exercised |
| Diagnostics | Preserve selected device, clear removed selection, disable conflicting operations, capture progress, copy visible results | Browser selection/rescan/capture checks; physical HID access requires Mac check |
| Recording | Keep full-rate raw/derived data; CSV and metadata snapshots stream without locking acquisition for the download; elapsed recording time | CSV/OSC output regression, HTTP 500-frame EEG smoke check, browser download/start/stop |
| OSC | Preserve in-progress edits, validate ports, full five-band receiver and schema | UDP regression and browser edit/apply checks |
| Accessibility/layout | Tab semantics and arrow-key navigation, filter selection state, live error/report regions, horizontal containment for diagnostic tables, min-width fixes | Both tabs at 1180, 768, 390, 320 pixels, dark and light themes; no page overflow or JS errors |
| Lifecycle | Detect a second launch before opening sensors; explicit Quit app; reconnect and reset cursors after service restart | Startup regression and browser outage/restart tests |

Validation uses synthetic sensor streams and the real HTTP handler/UI in headless Chromium. It does not establish physical device compatibility, Safari-specific behavior, electrode calibration, camera physiological accuracy, or audible output on the user's Mac. Those need on-device checks; acquisition protocols remain unchanged.
