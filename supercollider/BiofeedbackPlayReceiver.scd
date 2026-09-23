(
// Biofeedback Play -> SuperCollider starter receiver
// In Biofeedback Play, enable OSC and use 127.0.0.1:57120.

~biofeedback = (
    skinRaw: 0,
    pulseRaw: 0
);

OSCdef(\biofeedbackSkin, { |msg|
    ~biofeedback[\skinRaw] = msg[1].asInteger;
}, '/biofeedback/lightstone/skin_raw');

OSCdef(\biofeedbackPulse, { |msg|
    ~biofeedback[\pulseRaw] = msg[1].asInteger;
}, '/biofeedback/lightstone/pulse_raw');

"Biofeedback Play OSC receiver ready.".postln;
)

// Inspect current values whenever you want:
// ~biofeedback.postln;

// Stop listening later with:
// OSCdef(\biofeedbackSkin).free;
// OSCdef(\biofeedbackPulse).free;
