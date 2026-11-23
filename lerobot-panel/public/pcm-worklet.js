// Mic -> 16 kHz mono 16-bit PCM in ~50 ms frames for AssemblyAI streaming (used by /kiosk).
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.target = options.processorOptions?.targetRate ?? 16000;
    this.ratio = sampleRate / this.target;
    this.frame = new Int16Array(Math.round(this.target * 0.05));
    this.fill = 0;
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
      const v = this.muted ? 0 : sum / Math.max(1, end - start);
      this.frame[this.fill++] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)));
      if (this.fill === this.frame.length) {
        this.port.postMessage(this.frame.buffer.slice(0));
        this.fill = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= input.length;
    return true;
  }
}

registerProcessor("pcm16-downsampler", Pcm16Downsampler);
