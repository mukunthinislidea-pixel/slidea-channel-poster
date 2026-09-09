/**
 * send.js
 *
 * One Baileys connection for the whole run, driven over stdin/stdout as
 * newline-delimited JSON:
 *
 *   ->  {"caption": "...", "media": "data:image/png;base64,..." | null}
 *   <-  {"ok": true}                     on success
 *   <-  {"ok": false, "error": "..."}    on failure (that item is skipped
 *                                        by the caller; it stays un-seen
 *                                        so a later run retries it)
 *
 * The process exits when stdin closes. It never calls logout().
 */

const readline = require("readline");
const wa = require("./wa");

function replyLine(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function dataUriToBuffer(dataUri) {
  const m = /^data:(.+?);base64,(.*)$/s.exec(dataUri || "");
  if (!m) return null;
  return Buffer.from(m[2], "base64");
}

async function main() {
  const sock = await wa.connect();
  await wa.waitForOpen(sock);

  let channelJid;
  try {
    channelJid = await wa.resolveChannel(sock);
  } catch (e) {
    replyLine({ ok: false, error: `channel_resolve_failed: ${e.message}` });
    sock.end(undefined);
    process.exit(0);
  }

  const rl = readline.createInterface({ input: process.stdin, terminal: false });

  rl.on("line", async (line) => {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch (e) {
      replyLine({ ok: false, error: "bad_json_from_caller" });
      return;
    }

    try {
      const buf = dataUriToBuffer(msg.media);
      const payload = buf
        ? { image: buf, caption: msg.caption }
        : { text: msg.caption };
      await sock.sendMessage(channelJid, payload);
      replyLine({ ok: true });
    } catch (e) {
      replyLine({ ok: false, error: `send_failed: ${e.message}` });
    }
  });

  rl.on("close", () => {
    sock.end(undefined); // NEVER sock.logout() - that unlinks the device
    process.exit(0);
  });
}

main().catch((e) => {
  replyLine({ ok: false, error: `sender_error: ${e.message}` });
  process.exit(0);
});
