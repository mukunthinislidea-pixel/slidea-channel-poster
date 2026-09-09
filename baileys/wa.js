/**
 * wa.js - shared Baileys connection helper used by both send.js and pair.js.
 *
 * Channel posting notes (see HANDOVER.md section 7-8):
 *   - There is no official Meta API for WhatsApp Channels at any price.
 *     Baileys speaks the WhatsApp Web protocol directly and supports the
 *     "@newsletter" JIDs that Channels use.
 *   - One connection per run (open here, close once when the caller is
 *     done). Opening a fresh connection per message looks like an attack.
 *   - Never call sock.logout() - that permanently unlinks the device.
 *     Use sock.end(undefined) to just close the socket.
 */

const path = require("path");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  Browsers,
} = require("@itsliaaa/baileys");
const session = require("./session");

const WA_SESSION_KEY = process.env.WA_SESSION_KEY;
const CHANNEL_INVITE = process.env.WA_CHANNEL_INVITE;

/**
 * Trap #4: Baileys expects a pino-style logger and calls logger.child({...})
 * on it internally. Passing a bare { level: "silent" } object crashes the
 * connection immediately with "logger.child is not a function" (hit live on
 * 9 Sep 2026, pair run 34332866712). This stub has the full shape Baileys
 * touches and avoids pulling pino in as a dependency. Do not "simplify" it
 * back to a plain object.
 */
const silentLogger = {
  level: "silent",
  trace() {},
  debug() {},
  info() {},
  warn() {},
  error() {},
  fatal() {},
  child() {
    return silentLogger;
  },
};

// Same shape, but actually prints - used when connect({ silent: false }).
// Everything goes to stderr so it can never corrupt send.js's JSON stdout.
const verboseLogger = {
  level: "debug",
  trace: (...a) => console.error("[wa trace]", ...a),
  debug: (...a) => console.error("[wa debug]", ...a),
  info: (...a) => console.error("[wa info]", ...a),
  warn: (...a) => console.error("[wa warn]", ...a),
  error: (...a) => console.error("[wa error]", ...a),
  fatal: (...a) => console.error("[wa fatal]", ...a),
  child() {
    return verboseLogger;
  },
};

async function connect({ silent = true } = {}) {
  session.restore(WA_SESSION_KEY);
  const { state, saveCreds } = await useMultiFileAuthState(session.AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false, // pair.js handles QR rendering itself
    browser: Browsers.macOS("Desktop"),
    logger: silent ? silentLogger : verboseLogger,
  });

  sock.ev.on("creds.update", async () => {
    await saveCreds();
    session.persist(WA_SESSION_KEY);
  });

  return sock;
}

/**
 * Resolve the target channel's JID from its invite code.
 * Falls back to matching by subscribed-channel name if the invite lookup
 * fails (e.g. the bot number is already a member/admin).
 */
async function resolveChannel(sock, invite = CHANNEL_INVITE, fallbackName = null) {
  try {
    const meta = await sock.newsletterMetadata("invite", invite);
    if (meta && meta.id) return meta.id;
  } catch (e) {
    // fall through to the name-match fallback below
  }
  if (fallbackName) {
    try {
      const subscribed = await sock.newsletterSubscribed();
      const match = (subscribed || []).find(
        (c) => (c.name || "").toLowerCase() === fallbackName.toLowerCase()
      );
      if (match) return match.id;
    } catch (e) {
      // give up - caller will report sender_error
    }
  }
  throw new Error(`could not resolve channel for invite code ${invite}`);
}

function waitForOpen(sock, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("connection timed out")), timeoutMs);
    sock.ev.on("connection.update", (update) => {
      if (update.connection === "open") {
        clearTimeout(timer);
        resolve();
      }
      if (update.connection === "close") {
        const statusCode = update.lastDisconnect?.error?.output?.statusCode;
        clearTimeout(timer);
        reject(new Error(`connection closed before opening (status ${statusCode})`));
      }
    });
  });
}

module.exports = { connect, resolveChannel, waitForOpen, silentLogger, verboseLogger };
