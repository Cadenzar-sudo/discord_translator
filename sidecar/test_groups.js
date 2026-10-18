/** Unit tests for the batching module (no Discord, no network):
 *    node test_groups.js
 * Exits non-zero on the first failure. */

const assert = require("assert");
const { parseGroups, Accumulators } = require("./groups");

let passed = 0;
function ok(name, fn) {
  fn();
  passed++;
  console.log(`  PASS  ${name}`);
}

// ---- parseGroups: gate + shapes + matching --------------------------------
ok("gate: groups ignored unless asr_engine is groq", () => {
  const groups = { english: ["Alice"] };
  for (const engine of ["local", "local_first", undefined]) {
    assert.strictEqual(parseGroups(engine, groups).size, 0, engine);
    assert.strictEqual(parseGroups(engine, groups, () => {}).size, 0);
  }
  assert.strictEqual(parseGroups("groq", undefined).size, 0);
  assert.strictEqual(parseGroups("groq", {}).size, 0);
});

ok("shapes: member array shorthand + {members, language} object", () => {
  const g = parseGroups("groq", {
    english: ["Alice", "Bob"],
    japanese: { members: ["Kenji"], language: "ja" },
  }, () => {});
  assert.deepStrictEqual(g.get("alice"), { key: "english", language: null });
  assert.deepStrictEqual(g.get("bob"), { key: "english", language: null });
  assert.deepStrictEqual(g.get("kenji"), { key: "japanese", language: "ja" });
});

ok("matching is case-insensitive + whitespace-tolerant", () => {
  const g = parseGroups("groq", { english: ["  dIsCoRd4HoPe "] }, () => {});
  assert.ok(g.has("discord4hope"));
  assert.strictEqual(g.get("discord4hope").key, "english");
});

ok("empty/blank members are skipped", () => {
  const g = parseGroups("groq", { english: ["", "   ", null, "Alice"] }, () => {});
  assert.strictEqual(g.size, 1);
  assert.ok(g.has("alice"));
});

// ---- Accumulators: framing + batch merging + lifecycle --------------------
function collector() {
  const frames = [];
  const send = (uid, label, samples) => frames.push({ uid, label,
    samples: Float32Array.from(samples) });
  return { frames, send };
}

ok("feed drains exact 100 ms frames in order", () => {
  const acc = new Accumulators();
  const { frames, send } = collector();
  const chunk = Float32Array.from({ length: 320 }, (_, i) => i); // 20 ms
  for (let i = 0; i < 5; i++) acc.feed("user1", "Alice", chunk, send);
  assert.strictEqual(frames.length, 1);
  assert.strictEqual(frames[0].uid, "user1");
  assert.strictEqual(frames[0].label, "Alice");
  assert.strictEqual(frames[0].samples.length, 1600);
  // order preserved: first drained sample is chunk0[0]
  assert.strictEqual(frames[0].samples[0], 0);
  assert.strictEqual(acc.sentSamples("user1"), 1600);
});

ok("batch members merge into ONE labeled-mute stream", () => {
  const acc = new Accumulators();
  const { frames, send } = collector();
  // alice and bob share uid batch:english with empty display label
  acc.feed("batch:english", "", Float32Array.from([1, 2, 3]), send);
  acc.feed("user-alice-id", "Alice", Float32Array.from([4]), send); // noise guard
  acc.feed("batch:english", "", Float32Array.from({ length: 1600 }, () => 0.5), send);
  const batched = frames.filter((f) => f.uid === "batch:english");
  assert.strictEqual(batched.length, 1);
  assert.strictEqual(batched[0].label, ""); // unlabeled subtitle line
  assert.strictEqual(batched[0].samples.length, 1600);
  // merged in arrival order: the 3 alice samples precede the 0.5 fill
  assert.strictEqual(batched[0].samples[0], 1);
  assert.strictEqual(batched[0].samples[3], 0.5);
  // the ungrouped user got their own accumulator, nothing emitted yet
  assert.strictEqual(frames.filter((f) => f.uid === "user-alice-id").length, 0);
});

ok("lifecycle: drop solo accumulator, batch persists for other members", () => {
  const acc = new Accumulators();
  const { send } = collector();
  acc.feed("batch:english", "", Float32Array.from([1]), send);
  acc.feed("solo-id", "Solo", Float32Array.from([1]), send);
  acc.drop("solo-id");          // user left: their accumulator goes
  acc.feed("solo-id", "Solo", Float32Array.from([1]), send); // fresh state
  acc.feed("batch:english", "", Float32Array.from([2]), send); // still merged
  // no throw + batch state untouched is the contract; verify via sentSamples
  assert.strictEqual(acc.sentSamples("solo-id"), 0); // fresh after drop+feed
  assert.strictEqual(acc.sentSamples("batch:english"), 0); // < 1 frame held
});

console.log(`\n${passed} batching tests passed`);
