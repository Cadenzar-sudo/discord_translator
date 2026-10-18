/**
 * Voice-bot sidecar: the primary audio source for source="ws".
 *
 * Joins a Discord voice channel as a bot (DAVE E2EE handled by
 * @discordjs/voice + @snazzah/davey), decodes each speaking user to mono
 * 16 kHz PCM, and streams it to the translator server's /audio endpoint as
 * [2-byte name-len][utf8 display name][float32-LE samples] binary frames.
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

const FRAME_SAMPLES = 1600; // 100 ms of mono 16 kHz float32 per WS frame
const INACTIVITY_END_MS = 1000;
const WATCHDOG_MS = 15_000;
const MAX_QUEUE = 40; // ~4 s of frames before drop-oldest
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
const GUILD_ID = cfg.discord && cfg.discord.guild_id;
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

// ------------------------------------------------------------- audio link
let ws = null;
let wsBackoff = 1000;
const sendQueue = [];

function wsConnect() {
  const url = AUDIO_URL + (SERVER_TOKEN
    ? `${AUDIO_URL.includes("?") ? "&" : "?"}token=${encodeURIComponent(SERVER_TOKEN)}`
    : "");
  console.log(`[sidecar] connecting to ${AUDIO_URL}`);
  const sock = new WebSocket(url);
  sock.binaryType = "arraybuffer";

  sock.onopen = () => {
    wsBackoff = 1000;
    console.log("[sidecar] /audio connected");
    sock.send(JSON.stringify({ type: "hello", client: "voice-bot",
                               speaker: cfg.speaker_name || "Discord" }));
    flushQueue();
  };
  sock.onmessage = (ev) => {
    try {
      const msg = JSON.parse(String(ev.data));
      if (msg.type === "welcome") console.log("[sidecar] server welcomed us");
    } catch { /* ignore non-JSON */ }
  };
  sock.onclose = (ev) => {
    if (ws === sock) ws = null;
    console.log(`[sidecar] /audio closed (code ${ev.code}) — retrying in ` +
                `${wsBackoff} ms`);
    setTimeout(wsConnect, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 10_000);
  };
  sock.onerror = () => { /* onclose follows */ };
  ws = sock;
}

function flushQueue() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  while (sendQueue.length) ws.send(sendQueue.shift());
}

function sendFrame(name, float32) {
  let nameBytes = Buffer.from(name, "utf8");
  if (nameBytes.length > 128) nameBytes = nameBytes.subarray(0, 128);
  const header = Buffer.alloc(2);
  header.writeUInt16LE(nameBytes.length, 0);
  const payload = Buffer.concat([
    header, nameBytes,
    Buffer.from(float32.buffer, float32.byteOffset, float32.byteLength),
  ]);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(payload);
    return;
  }
  if (sendQueue.length >= MAX_QUEUE) sendQueue.shift(); // live audio: drop old
  sendQueue.push(payload);
}

// ------------------------------------------------------- per-user pipeline
let activeConnection = null;

function displayName(guild, userId) {
  const m = guild.members.cache.get(userId);
  return (m && m.displayName) || `user-${userId.slice(-4)}`;
}

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

function attachUser(connection, userId) {
  const guild = client.guilds.cache.get(connection.joinConfig.guildId);
  const name = guild ? displayName(guild, userId) : `user-${userId.slice(-4)}`;
  console.log(`[voice] ${name} started speaking — subscribing`);

  const stream = connection.receiver.subscribe(userId, {
    end: { behavior: EndBehaviorType.AfterInactivity, duration: INACTIVITY_END_MS },
  });
  const decoder = new prism.opus.Decoder({
    rate: 48000, channels: 2, frameSize: 960,
  });
  let acc = new Float32Array(0);
  let sentSamples = 0;

  stream.on("data", (opusPacket) => {
    let pcm;
    try {
      pcm = decoder.decode(opusPacket);
    } catch {
      return; // bad packet: skip, the next one carries its own timing
    }
    const chunk = toMono16k(pcm);
    const merged = new Float32Array(acc.length + chunk.length);
    merged.set(acc);
    merged.set(chunk, acc.length);
    acc = merged;
    while (acc.length >= FRAME_SAMPLES) {
      sendFrame(name, acc.subarray(0, FRAME_SAMPLES));
      acc = acc.subarray(FRAME_SAMPLES);
      sentSamples += FRAME_SAMPLES;
    }
  });
  stream.on("end", () => {
    if (acc.length) sendFrame(name, acc); // flush the sub-100 ms tail
    try { decoder.destroy(); } catch { /* already gone */ }
    console.log(`[voice] ${name} stream ended ` +
                `(${(sentSamples / 16000).toFixed(1)}s sent)`);
  });
  stream.on("error", () => { /* receiver cleans up; speaking restarts us */ });
}

// -------------------------------------------------------------- voice join
let joinedChannelId = null;

function joinChannel(channel) {
  console.log(`[sidecar] joining voice '${channel.name}' (${channel.id})`);
  const connection = joinVoiceChannel({
    channelId: channel.id,
    guildId: channel.guildId,
    adapterCreator: channel.guild.voiceAdapterCreator,
    selfDeafen: false, // a deafened bot receives no audio
  });
  activeConnection = connection;
  joinedChannelId = channel.id;
  connection.on(VoiceConnectionStatus.Ready, () => {
    console.log(`[sidecar] voice ready in '${channel.name}' — attaching receiver`);
    const receiver = connection.receiver;
    receiver.speaking.on("start", (userId) => attachUser(connection, userId));
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
