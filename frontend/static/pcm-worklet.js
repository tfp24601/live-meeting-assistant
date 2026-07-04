// AudioWorklet: downsample the live mic/system audio to 16 kHz mono Int16 PCM
// and post ~80 ms chunks back to the main thread (which sends them over the WS).
//
// `sampleRate` is a global available inside AudioWorklet scope (the context rate,
// usually 44.1 or 48 kHz). We linearly resample to 16 kHz.

class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate; // input samples per output sample
    this.buf = [];        // pending input samples (mono float)
    this.pos = 0;         // fractional read position into buf
    this.outAccum = [];   // accumulated output samples awaiting a flush
    this.flushAt = 1280;  // 80 ms @ 16 kHz
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0]; // first channel (mono)

    for (let i = 0; i < ch.length; i++) this.buf.push(ch[i]);

    // Resample buf -> 16 kHz via linear interpolation.
    while (this.pos + 1 < this.buf.length) {
      const i0 = Math.floor(this.pos);
      const frac = this.pos - i0;
      const s = this.buf[i0] * (1 - frac) + this.buf[i0 + 1] * frac;
      this.outAccum.push(s);
      this.pos += this.ratio;
    }

    // Drop consumed input samples, keep the fractional remainder aligned.
    const consumed = Math.floor(this.pos);
    if (consumed > 0) {
      this.buf.splice(0, consumed);
      this.pos -= consumed;
    }

    if (this.outAccum.length >= this.flushAt) {
      const pcm = new Int16Array(this.outAccum.length);
      for (let i = 0; i < this.outAccum.length; i++) {
        let v = Math.max(-1, Math.min(1, this.outAccum[i]));
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      this.outAccum.length = 0;
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
