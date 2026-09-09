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

async function connect({ silent = true } = {}) {
  session.restore(WA_SESSION_KEY);
  const { state, saveCreds } = await useMultiFileAuthState(session.AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false, // pair.js handles QR rendering itself
    browser: Browsers.macOS("Desktop"),
    logger: silent ? { level: "silent" } : undefined,
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

module.exports = { connect, resolveChannel, waitForOpen };
