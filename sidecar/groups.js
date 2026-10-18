/** Transcription batching: asr_groups config parsing + per-stream 100 ms
 * frame accumulators. Grouped members share one accumulator keyed
 * `batch:<group>` so the whole group costs one transcription request. */

const FRAME_SAMPLES = 1600; // 100 ms of mono 16 kHz float32

/** Gate (asr_engine must be "groq" — batching exists to save the free
 * tier's 20 RPM) + config shapes -> lowercase member -> {key, language}.
 * Group value is either a member array or {members, language}. */
function parseGroups(asrEngine, asrGroups, log = console.log) {
  const map = new Map();
  if (asrEngine !== "groq" || !asrGroups || typeof asrGroups !== "object") {
    return map;
  }
  for (const [key, spec] of Object.entries(asrGroups)) {
    const members = Array.isArray(spec) ? spec
      : (spec && Array.isArray(spec.members)) ? spec.members : [];
    const language = (!Array.isArray(spec) && spec && spec.language) || null;
    for (const m of members) {
      if (m === null || m === undefined) continue;
      const k = String(m).trim().toLowerCase();
      if (k) map.set(k, { key, language: language || null });
    }
    if (members.length) {
      log(`[sidecar] batch group '${key}': ${members.length} member(s)` +
          (language ? ` (lang ${language})` : ""));
    }
  }
  if (!map.size) log("[sidecar] asr_groups empty — ignored");
  return map;
}

/** 100 ms frame accumulators keyed by output-stream uid. Ungrouped users
 * get their own entry (uid = discord snowflake); a batch group's members
 * all feed the same `batch:<group>` entry. */
class Accumulators {
  constructor(frameSamples = FRAME_SAMPLES) {
    this.frameSamples = frameSamples;
    this.map = new Map(); // uid -> {label, acc: Float32Array, sent}
  }

  feed(uid, label, chunk, send) {
    let st = this.map.get(uid);
    if (!st) {
      st = { label, acc: new Float32Array(0), sent: 0 };
      this.map.set(uid, st);
    }
    const merged = new Float32Array(st.acc.length + chunk.length);
    merged.set(st.acc);
    merged.set(chunk, st.acc.length);
    st.acc = merged;
    while (st.acc.length >= this.frameSamples) {
      send(uid, st.label, st.acc.subarray(0, this.frameSamples));
      st.acc = st.acc.subarray(this.frameSamples);
      st.sent += this.frameSamples;
    }
  }

  sentSamples(uid) {
    const st = this.map.get(uid);
    return st ? st.sent : 0;
  }

  drop(uid) {
    // solo streams die with their user; batch entries persist while any
    // member remains subscribed (drop only when the last member leaves)
    this.map.delete(uid);
  }
}

module.exports = { parseGroups, Accumulators, FRAME_SAMPLES };
