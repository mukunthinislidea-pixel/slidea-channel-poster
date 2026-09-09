/**
 * pair.js - one-time (or after-a-logout) WhatsApp login.
 *
 * Run manually via the "Pair WhatsApp" GitHub Actions workflow, mode "qr":
 * open the running step's log and scan the printed QR with a phone that is
 * an admin of the target channel. On success, state/wa-session.enc is
 * written/updated and committed by the workflow.
 *
 * Three hard-won traps, do not "simplify" these away:
 *
 *  1. Browser identity: pairing-code mode must use exactly
 *     ["Ubuntu", "Chrome", "20.0.04"]. Browsers.macOS("Desktop") lets
 *     WhatsApp issue pairing codes but then rejects every one of them with
 *     "Couldn't link device - get a new code", which looks like an
 *     expiry bug and isn't one.
 *
 *  2. Each retry needs a FRESH auth folder. Reusing one across attempts
 *     leaves half-registered credentials behind, and every later
 *     connection gets "device logged out" instead of a new code/QR - so
 *     only the very first attempt in a stale folder was ever real. This
 *     script wipes and recreates the auth folder at the top of every
 *     attempt inside the retry loop.
 *
 *  3. QR rendering: qrcode-terminal's ANSI background colours get
 *     stripped by GitHub's log viewer (renders as blank lines), and a
 *     hand-rolled "██ per module" renderer is 74 chars wide and wraps in
 *     the log, destroying the pattern. QRCode.toString({type:"utf8"})
 *     uses half-block characters - one per module, two module rows per
 *     line, ~37 chars wide - and survives the log viewer intact.
 */

const fs = require("fs");
const path = require("path");
const QRCode = require("qrcode");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  Browsers,
  DisconnectReason,
} = require("@itsliaaa/baileys");
const session = require("./session");
const { resolveChannel, waitForOpen } = require("./wa");

const WA_SESSION_KEY = process.env.WA_SESSION_KEY;
const MODE = (process.argv[2] || "qr").toLowerCase(); // "qr" | "code"
const PHONE_NUMBER = process.env.PAIR_PHONE_NUMBER || ""; // required for "code" mode

function wipeAuthDir() {
  fs.rmSync(session.AUTH_DIR, { recursive: true, force: true });
  fs.mkdirSync(session.AUTH_DIR, { recursive: true });
}

async function attemptOnce() {
  wipeAuthDir(); // trap #2 - never reuse a folder across attempts

  const { state, saveCreds } = await useMultiFileAuthState(session.AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    browser: ["Ubuntu", "Chrome", "20.0.04"], // trap #1 - do not swap for Browsers.macOS(...)
    logger: { level: "silent" },
  });

  sock.ev.on("creds.update", saveCreds);

  if (MODE === "code") {
    if (!PHONE_NUMBER) throw new Error("PAIR_PHONE_NUMBER env var required for code mode");
    const code = await sock.requestPairingCode(PHONE_NUMBER);
    console.log(`\nPairing code: ${code}\nEnter this in WhatsApp > Linked Devices > Link with phone number.\n`);
  } else {
    sock.ev.on("connection.update", async (update) => {
      if (update.qr) {
        const rendered = await QRCode.toString(update.qr, { type: "utf8" }); // trap #3
        console.log("\n" + rendered + "\n(scan within ~100 seconds - a new one replaces this automatically)\n");
      }
    });
  }

  await waitForOpen(sock, 120000);
  session.persist(WA_SESSION_KEY);
  console.log("Linked successfully. state/wa-session.enc has been written.");

  try {
    const invite = process.env.WA_CHANNEL_INVITE;
    if (invite) {
      const jid = await resolveChannel(sock, invite);
      console.log(`Channel resolved: ${jid}`);
    }
  } catch (e) {
    console.log(`Note: channel not resolved yet (${e.message}). This is fine if the bot number `
      + `hasn't been made an admin of the channel yet - do that, then re-run a normal post.`);
  }

  sock.end(undefined); // never logout()
  process.exit(0);
}

attemptOnce().catch((e) => {
  console.error(`Pairing attempt failed: ${e.message}`);
  console.error("If this says 'Couldn't link device - get a new code', re-run the workflow for a fresh attempt (trap #2/#1 above).");
  process.exit(1);
});
