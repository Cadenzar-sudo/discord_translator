/**
 * Voice-bot sidecar: the primary audio source for source="ws".
 *
 * Joins a Discord voice channel as a bot (DAVE E2EE handled by
 * @discordjs/voice + @snazzah/davey), decodes each speaking user to mono
 * 16 kHz PCM, and streams it to the translator server's /audio endpoint as
 * [2B uid-len][utf8 user id][2B name-len][utf8 display name][float32-LE
 * samples] binary frames (the uid keys the speaker's session server-side,
 * so two members with the same display name stay separate).
 *
 * Config comes from the SAME config.json the Python server uses (mount it):
 *   discord.token, discord.guild_id (set = instant slash commands),
 *   discord_web.channel_id + auto_join (boot-time join + watchdog),
 *   server.access_token (sent as ?token= — no headers on browser-style WS),
 *   audio_shipper.url (empty = ws://127.0.0.1:{server.port}/audio).
 *
 * The bot never self-deafens: a deafened bot receives no audio.
 */

const fs = require("fs");
const http = require("http");
const path = require("path");

const {
  Client,
  Events,
  GatewayIntentBits,
  SlashCommandBuilder,
} = require("discord.js");
const {
  EndBehaviorType,
  enterState,
  getVoiceConnection,
  getVoiceConnections,
  joinVoiceChannel,
  VoiceConnectionStatus,
} = require("@discordjs/voice");
const prism = require("prism-media");
const WebSocket = require("ws"); // undici's global WS swallows refused connects

const FRAME_SAMPLES = 1600; // 100 ms of mono 16 kHz float32 per WS frame

const WATCHDOG_MS = 15_000;
const MAX_QUEUE = 40; // ~4 s of frames before drop-oldest
// server fell this far behind while connected: drop live frames instead of
// letting ws's internal send buffer grow without bound (OOM on long soaks)
const MAX_BUFFERED = 262144;
const HEALTH_PORT = process.env.HEALTH_PORT || 8766;

// ------------------------------------------------------------------ config
function loadConfig() {
  const candidates = [
    process.env.CONFIG_PATH,
    path.join(__dirname, "config.json"),
    "/data/config.json",
  ].filter(Boolean);
  for (const p of candidates) {
    try {
      return JSON.parse(fs.readFileSync(p, "utf8"));
    } catch (e) {
      if (e.code !== "ENOENT") throw e;
    }
  }
  throw new Error(`config.json not found (tried: ${candidates.join(", ")})`);
}

const cfg = loadConfig();
const SERVER_TOKEN = (cfg.server && cfg.server.access_token) || "";
const AUDIO_URL =
  (cfg.audio_shipper && cfg.audio_shipper.url) ||
  `ws://127.0.0.1:${(cfg.server && cfg.server.port) || 8765}/audio`;
const CHANNEL_ID = cfg.discord_web && cfg.discord_web.channel_id;
const AUTO_JOIN = !!(cfg.discord_web && cfg.discord_web.auto_join);

if (!cfg.discord || !cfg.discord.token) {
  console.error("[sidecar] discord.token is empty — run server/setup.bat " +
    "(the sidecar reads the same config.json as the Python server)");
  process.exit(1);
}

try {
  require("@snazzah/davey");
  console.log("[sidecar] davey present — DAVE E2EE receive enabled");
} catch {
  console.error("[sidecar] WARNING: @snazzah/davey not installed — receive " +
    "will fail on DAVE-enforced channels (npm i @snazzah/davey)");
}

// ------------------------------------------------- transcription batching
// asr_groups mixes listed members into ONE stream: one Groq request per
// utterance for the whole group (RPM saver for the free tier). Batched
// frames carry uid `batch:<group>` + empty display name — the server keeps
// them as unlabeled subtitles. groq-only: other engines have no RPM gate.
const { parseGroups, Accumulators } = require("./groups");
const GROUPS = parseGroups(cfg.asr_engine, cfg.asr_groups);

// ------------------------------------------------------------- audio link
let ws = null;
let wsBackoff = 1000;
const sendQueue = [];

function wsConnect() {
  console.log(`[sidecar] connecting to ${AUDIO_URL}`);
  const sock = new WebSocket(AUDIO_URL, {
    headers: SERVER_TOKEN ? { Authorization: `Bearer ${SERVER_TOKEN}` } : {},
  });

  sock.on("open", () => {
    wsBackoff = 1000;
    console.log("[sidecar] /audio connected");
    sock.send(JSON.stringify({ type: "hello", client: "voice-bot",
                               speaker: cfg.speaker_name || "Discord" }));
    flushQueue();
  });
  sock.on("message", (data) => {
    try {
      const msg = JSON.parse(String(data));
      if (msg.type === "welcome") console.log("[sidecar] server welcomed us");
    } catch { /* ignore non-JSON */ }
  });
  sock.on("close", (code, reason) => {
    if (ws === sock) ws = null;
    console.log(`[sidecar] /audio closed (code ${code} ${reason || ""}) — ` +
                `retrying in ${wsBackoff} ms`);
    setTimeout(wsConnect, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 10_000);
  });
  sock.on("error", (err) =>
    console.log(`[sidecar] /audio error: ${err.message}`));
  ws = sock;
}

function flushQueue() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  while (sendQueue.length) ws.send(sendQueue.shift());
}

function sendFrame(uid, name, float32) {
  let uidBytes = Buffer.from(uid, "utf8");
  if (uidBytes.length > 64) uidBytes = uidBytes.subarray(0, 64);
  let nameBytes = Buffer.from(name, "utf8");
  if (nameBytes.length > 128) nameBytes = nameBytes.subarray(0, 128);
  const uidLen = Buffer.alloc(2);
  uidLen.writeUInt16LE(uidBytes.length, 0);
  const nameLen = Buffer.alloc(2);
  nameLen.writeUInt16LE(nameBytes.length, 0);
  // wire order matches the server parser: len-prefixed uid, len-prefixed
  // name, then samples (an earlier draft put both lengths up front — the
  // server read the name length from inside the uid and dropped everything
  // as malformed)
  const payload = Buffer.concat([
    uidLen, uidBytes, nameLen, nameBytes,
    Buffer.from(float32.buffer, float32.byteOffset, float32.byteLength),
  ]);
  if (ws && ws.readyState === WebSocket.OPEN) {
    if (ws.bufferedAmount > MAX_BUFFERED) return; // server behind: drop
    ws.send(payload);
    return;
  }
  if (sendQueue.length >= MAX_QUEUE) sendQueue.shift(); // live audio: drop old
  sendQueue.push(payload);
}

// ------------------------------------------------------- per-user pipeline
let activeConnection = null;

// 48 kHz stereo s16le PCM -> mono 16 kHz float32 (3:1 average decimation)
function toMono16k(stereoPcm) {
  const frames = Math.floor(stereoPcm.length / 4);
  const mono = new Float32Array(frames);
  for (let i = 0; i < frames; i++) {
    mono[i] = (stereoPcm.readInt16LE(i * 4) +
               stereoPcm.readInt16LE(i * 4 + 2)) / 65536;
  }
  const out = new Float32Array(Math.floor(frames / 3));
  for (let i = 0; i < out.length; i++) {
    out[i] = (mono[i * 3] + mono[i * 3 + 1] + mono[i * 3 + 2]) / 3;
  }
  return out;
}

// one persistent stream + decoder per channel member: subscribing only when
// the "speaking" indicator fires races the first RTP packets and clips the
// onset of every utterance (the accuracy regression vs loopback capture).
// Manual end-behavior keeps the stream alive across the user's pauses.
const subscriptions = new Map(); // userId -> {stream, decoder, name, batchUid}
const speakingStart = new Map(); // userId -> perf_now at last speaking start
// per-output-stream 100 ms accumulators: one per ungrouped user, one SHARED
// per batch group (members' decoded chunks merge into the same entry)
const streamAcc = new Accumulators(FRAME_SAMPLES);

function subscribeUser(connection, userId) {
  if (subscriptions.has(userId) || !connection) return;
  const guild = client.guilds.cache.get(connection.joinConfig.guildId);
  const member = guild ? guild.members.cache.get(userId) : null;
  const name = (member && member.displayName) || `user-${userId.slice(-4)}`;
  // asr_groups matches the Discord TAG (user.username) or the server
  // display name, case-insensitive — whichever the config author used
  const tag = ((member && member.user && member.user.username) || "")
    .trim().toLowerCase();
  const group = GROUPS.get(name.trim().toLowerCase()) ||
    (tag && GROUPS.get(tag)) || null;
  // grouped members share one VAD session per group, labeled batch:<group>;
  // the empty display name makes their subtitle lines unlabeled
  const uid = group ? `batch:${group.key}` : userId;
  const label = group ? "" : name;

  const stream = connection.receiver.subscribe(userId, {
    end: { behavior: EndBehaviorType.Manual },
  });
  const decoder = new prism.opus.Decoder({
    rate: 48000, channels: 2, frameSize: 960,
  });
  subscriptions.set(userId, { stream, decoder, name, batchUid: group ? uid : null });
  console.log(`[voice] subscribed to ${name}` +
              (group ? ` -> batch '${group.key}'` : ""));

  let packets = 0;

  stream.on("error", (e) =>
    console.log(`[voice] ${name} stream ERROR: ${e.message || e}`));
  decoder.on("error", (e) =>
    console.log(`[voice] ${name} decode ERROR: ${e.message || e}`));

  decoder.on("data", (pcm) => {
    packets++;
    const started = speakingStart.get(userId);
    if (started !== undefined) {
      const deltaMs = Math.round(performance.now() - started);
      if (deltaMs < 5000) {
        console.log(`[voice] ${name}: speaking->audio delta ${deltaMs} ms` +
                    (packets === 1 ? " (first packet of the stream)" : ""));
      }
      speakingStart.delete(userId);
    }
    streamAcc.feed(uid, label, toMono16k(pcm), sendFrame);
  });
  stream.pipe(decoder);
}

function unsubscribeUser(userId) {
  const sub = subscriptions.get(userId);
  if (!sub) return;
  try { sub.stream.destroy(); } catch { /* already gone */ }
  try { sub.decoder.destroy(); } catch { /* already gone */ }
  subscriptions.delete(userId);
  speakingStart.delete(userId);
  // a solo stream's accumulator dies with the user; a batch accumulator
  // stays — its remaining members still feed it
  if (!sub.batchUid) streamAcc.drop(userId);
  console.log(`[voice] unsubscribed ${sub.name}`);
}

function clearSubscriptions() {
  for (const userId of [...subscriptions.keys()]) unsubscribeUser(userId);
}

// -------------------------------------------------------------- voice join
let joinedChannelId = null;

function joinChannel(channel) {
  console.log(`[sidecar] joining voice '${channel.name}' (${channel.id})`);
  clearSubscriptions(); // stale streams belong to the old connection
  const connection = joinVoiceChannel({
    channelId: channel.id,
    guildId: channel.guildId,
    adapterCreator: channel.guild.voiceAdapterCreator,
    // a deafened bot receives no audio; note the option is `selfDeaf`
    // (not "selfDeafen" — unknown options are silently ignored and Discord
    // defaults bots to self-deafened)
    selfDeaf: false,
    selfMute: false,
  });
  activeConnection = connection;
  joinedChannelId = channel.id;
  connection.on(VoiceConnectionStatus.Ready, () => {
    console.log(`[sidecar] voice ready in '${channel.name}' — subscribing to ` +
                `everyone (dave session: ${Boolean(connection.daveSession)})`);
    // streams for people ALREADY in the channel; late joiners come through
    // the voiceStateUpdate handler below
    channel.members.forEach((m) => subscribeUser(connection, m.id));
    // speaking indicator only powers the onset-delta diagnostic now
    connection.receiver.speaking.on("start", (userId) =>
      speakingStart.set(userId, performance.now()));
  });
  connection.on(VoiceConnectionStatus.Disconnected, async () => {
    // try to signal-resume before declaring death (discordjs guide pattern)
    try {
      await Promise.race([
        enterState(connection, VoiceConnectionStatus.Signalling, 5_000),
        enterState(connection, VoiceConnectionStatus.Connecting, 5_000),
      ]);
    } catch {
      connection.destroy();
    }
  });
  return connection;
}

async function watchdogTick() {
  if (!joinedChannelId) return;
  if (!activeConnection ||
      activeConnection.state.status === VoiceConnectionStatus.Destroyed) {
    try {
      const ch = await client.channels.fetch(joinedChannelId);
      if (ch && ch.isVoiceBased()) joinChannel(ch);
    } catch (e) {
      console.log(`[sidecar] watchdog rejoin failed: ${e.message}`);
    }
  }
}

// ------------------------------------------------------------------ client
const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
});

const commands = [
  new SlashCommandBuilder()
    .setName("join")
    .setDescription("Join your voice channel and translate everyone"),
  new SlashCommandBuilder()
    .setName("leave")
    .setDescription("Stop translating and leave"),
].map((c) => c.toJSON());

client.once(Events.ClientReady, async (c) => {
  console.log(`[sidecar] logged in as ${c.user.tag}`);
  try {
    // per-guild registration appears instantly (global sync can lag ~1h);
    // iterate the guilds we're actually in — discord.guild_id in JSON is a
    // number and loses precision beyond Number.MAX_SAFE_INTEGER
    for (const [, guild] of c.guilds.cache) {
      await guild.commands.set(commands);
      console.log(`[sidecar] /join /leave ready in '${guild.name}'`);
    }
  } catch (e) {
    console.error(`[sidecar] command registration failed: ${e.message}`);
  }
  if (AUTO_JOIN && CHANNEL_ID) {
    try {
      const ch = await c.channels.fetch(CHANNEL_ID);
      if (ch && ch.isVoiceBased()) joinChannel(ch);
      else console.error(`[sidecar] discord_web.channel_id ${CHANNEL_ID} ` +
                         "is not a voice channel");
    } catch (e) {
      console.error(`[sidecar] auto-join failed: ${e.message} — the bot may ` +
                    "not be in that server yet, or lacks View Channel");
    }
  } else {
    console.log("[sidecar] idle — waiting for /join");
  }
  setInterval(() => watchdogTick().catch(() => {}), WATCHDOG_MS);
});

client.on(Events.InteractionCreate, async (i) => {
  if (!i.isChatInputCommand()) return;
  try {
    if (i.commandName === "join") {
      const voice = i.member?.voice;
      if (!voice?.channel) {
        await i.reply({ content: "Join a voice channel first, then run /join.",
                        ephemeral: true });
        return;
      }
      await i.deferReply({ ephemeral: true });
      for (const [, conn] of getVoiceConnections()) conn.destroy();
      joinChannel(voice.channel);
      await i.editReply(`Listening to **${voice.channel.name}** — everyone ` +
        "in this channel can now run the overlay client.");
    } else if (i.commandName === "leave") {
      const conns = [...getVoiceConnections().values()];
      if (!conns.length) {
        await i.reply({ content: "I'm not in a voice channel.", ephemeral: true });
        return;
      }
      for (const conn of conns) conn.destroy();
      activeConnection = null;
      joinedChannelId = null;
      await i.reply({ content: "Left the channel.", ephemeral: true });
    }
  } catch (e) {
    console.error(`[sidecar] interaction error: ${e.message}`);
    if (!i.replied) {
      await i.reply({ content: `Something broke: ${e.message}`, ephemeral: true })
        .catch(() => {});
    }
  }
});

// keep per-user subscriptions in sync with channel membership
client.on(Events.VoiceStateUpdate, (before, after) => {
  const member = after.member ?? before.member;
  if (!member || (activeConnection && member.id === client.user.id)) return;
  const wasHere = before.channelId === joinedChannelId;
  const isHere = after.channelId === joinedChannelId;
  if (!wasHere && isHere) {
    if (activeConnection) subscribeUser(activeConnection, member.id);
  } else if (wasHere && !isHere) {
    unsubscribeUser(member.id);
  }
});

// ------------------------------------------------------------- health probe
http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({
    in_voice: !!activeConnection &&
              activeConnection.state.status === VoiceConnectionStatus.Ready,
    channel: joinedChannelId,
    ws: ws && ws.readyState === WebSocket.OPEN ? "up" : "down",
    queued_frames: sendQueue.length,
  }));
}).listen(HEALTH_PORT, () =>
  console.log(`[sidecar] health on http://0.0.0.0:${HEALTH_PORT}/`));

wsConnect();
client.login(cfg.discord.token);

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    console.log("\n[sidecar] shutting down...");
    for (const [, conn] of getVoiceConnections()) conn.destroy();
    try { if (ws) ws.close(); } catch { /* closing */ }
    process.exit(0);
  });
}
