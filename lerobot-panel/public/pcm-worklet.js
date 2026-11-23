// Mic -> 16 kHz mono 16-bit PCM in ~50 ms frames for AssemblyAI streaming (used by /kiosk).
// Each message is {pcm: ArrayBuffer, level: dBFS of that frame} so the page can tell near from far voices.
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.target = options.processorOptions?.targetRate ?? 16000;
    this.ratio = sampleRate / this.target;
    this.frame = new Int16Array(Math.round(this.target * 0.05));
    this.fill = 0;
    this.energy = 0; // sum of squares of the raw (unmuted) input in the current frame
    this.pos = 0; // fractional read position into the incoming stream
    this.port.onmessage = (e) => { this.muted = !!e.data?.muted; };
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    // Average the samples that fall into each output sample (a cheap low-pass before decimating).
    while (this.pos < input.length) {
      const start = Math.floor(this.pos);
      const end = Math.min(input.length, Math.floor(this.pos + this.ratio));
      let sum = 0;
      for (let i = start; i < Math.max(end, start + 1); i++) sum += input[i];
      const avg = sum / Math.max(1, end - start);
      this.energy += avg * avg;
      const v = this.muted ? 0 : avg;
      this.frame[this.fill++] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)));
      if (this.fill === this.frame.length) {
        const rms = Math.sqrt(this.energy / this.frame.length);
        const pcm = this.frame.buffer.slice(0);
        this.port.postMessage({ pcm, level: 20 * Math.log10(rms + 1e-9) }, [pcm]);
        this.fill = 0;
        this.energy = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= input.length;
    return true;
  }
}

registerProcessor("pcm16-downsampler", Pcm16Downsampler);
