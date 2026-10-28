// Audio vlákno: sbírá mono Float32 vzorky po blocích 2048 a posílá je hlavnímu vláknu.
// Zpráva 'flush' odešle zbytek bufferu a potvrdí {flushed: true}.
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.offset = 0;
    this.port.onmessage = e => {
      if (e.data !== 'flush') return;
      if (this.offset) { this.port.postMessage(this.buffer.slice(0, this.offset)); this.offset = 0; }
      this.port.postMessage({ flushed: true });
    };
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) {
      for (let i = 0; i < input.length; i++) {
        this.buffer[this.offset++] = input[i];
        if (this.offset === this.buffer.length) {
          this.port.postMessage(this.buffer, [this.buffer.buffer]);
          this.buffer = new Float32Array(2048);
          this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
