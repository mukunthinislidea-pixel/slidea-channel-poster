/**
 * session.js
 *
 * Baileys stores WhatsApp login credentials as a folder of small JSON
 * files (AUTH_DIR). That folder IS the WhatsApp login - anyone holding a
 * copy can send as this number - and this repo is public. So we never
 * commit AUTH_DIR itself. Instead we pack it into one AES-256-GCM blob
 * keyed on WA_SESSION_KEY and commit that single file (state/wa-session.enc).
 * GitHub does not expose committed file contents any differently on a
 * public repo, but the blob is inert without WA_SESSION_KEY, which only
 * lives in GitHub Secrets.
 *
 * persist() is a no-op (returns 0) when the packed bytes are identical to
 * what's already on disk, so a fresh random IV every run doesn't create a
 * spurious "changed" commit every single hour forever.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// Computed at call time (not cached at require-time) so tests can point
// these at a scratch directory via env vars without touching the real
// .wa-auth/ or state/wa-session.enc that a live run depends on.
function defaultAuthDir() {
  return process.env.WA_AUTH_DIR || path.join(__dirname, "..", ".wa-auth");
}
function defaultEncPath() {
  return process.env.WA_ENC_PATH || path.join(__dirname, "..", "state", "wa-session.enc");
}

const ALGO = "aes-256-gcm";

function keyFromPassphrase(passphrase) {
  if (!passphrase || passphrase.length < 16) {
    throw new Error("WA_SESSION_KEY must be set and at least 16 characters long");
  }
  return crypto.createHash("sha256").update(passphrase, "utf8").digest();
}

function packAuthDir(authDir = defaultAuthDir()) {
  const files = {};
  if (fs.existsSync(authDir)) {
    for (const name of fs.readdirSync(authDir)) {
      const full = path.join(authDir, name);
      if (fs.statSync(full).isFile()) {
        files[name] = fs.readFileSync(full, "utf8");
      }
    }
  }
  return Buffer.from(JSON.stringify(files), "utf8");
}

function unpackIntoAuthDir(buf, authDir = defaultAuthDir()) {
  const files = JSON.parse(buf.toString("utf8"));
  fs.mkdirSync(authDir, { recursive: true });
  for (const [name, content] of Object.entries(files)) {
    fs.writeFileSync(path.join(authDir, name), content, "utf8");
  }
}

function encrypt(plainBuf, passphrase) {
  const key = keyFromPassphrase(passphrase);
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv(ALGO, key, iv);
  const ciphertext = Buffer.concat([cipher.update(plainBuf), cipher.final()]);
  const tag = cipher.getAuthTag();
  // layout: [12-byte iv][16-byte tag][ciphertext]
  return Buffer.concat([iv, tag, ciphertext]);
}

function decrypt(blob, passphrase) {
  const key = keyFromPassphrase(passphrase);
  const iv = blob.subarray(0, 12);
  const tag = blob.subarray(12, 28);
  const ciphertext = blob.subarray(28);
  const decipher = crypto.createDecipheriv(ALGO, key, iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}

/** Load state/wa-session.enc (if present) into .wa-auth/. */
function restore(passphrase, authDir = defaultAuthDir(), encPath = defaultEncPath()) {
  if (!fs.existsSync(encPath)) return false;
  const blob = fs.readFileSync(encPath);
  const plain = decrypt(blob, passphrase);
  unpackIntoAuthDir(plain, authDir);
  return true;
}

/**
 * Pack .wa-auth/ and write state/wa-session.enc if (and only if) the
 * decrypted contents would actually differ from what's already committed.
 * Returns true if it wrote a new file, false if it was a no-op.
 */
function persist(passphrase, authDir = defaultAuthDir(), encPath = defaultEncPath()) {
  const packed = packAuthDir(authDir);

  if (fs.existsSync(encPath)) {
    try {
      const existingPlain = decrypt(fs.readFileSync(encPath), passphrase);
      if (Buffer.compare(existingPlain, packed) === 0) {
        return false; // unchanged - do not touch the file (avoids noisy commits)
      }
    } catch (e) {
      // existing file unreadable with this key - fall through and overwrite
    }
  }

  fs.mkdirSync(path.dirname(encPath), { recursive: true });
  fs.writeFileSync(encPath, encrypt(packed, passphrase));
  return true;
}

module.exports = {
  get AUTH_DIR() { return defaultAuthDir(); },
  get ENC_PATH() { return defaultEncPath(); },
  restore, persist, encrypt, decrypt, packAuthDir, unpackIntoAuthDir,
};
