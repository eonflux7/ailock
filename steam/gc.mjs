// Deadlock game coordinator client using your own Steam login.
//
//   node gc.mjs login                       QR login (approve in the Steam mobile app); saves a refresh token
//   node gc.mjs history [account_id] [--max-pages N]   full match history as JSON (default: your account)
//   node gc.mjs salts <match_id> ...        replay cluster + salts per match as JSON lines
//
// The refresh token lives in $AILOCK_DATA/steam/refresh_token (default ../data), mode 600, and is only sent
// to Steam. Commands refuse to run while your account is in a game, since starting a GC session would
// kick that game.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import protobuf from "protobufjs";
import qrcode from "qrcode-terminal";
import { EAuthTokenPlatformType, LoginSession } from "steam-session";
import SteamUser from "steam-user";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA = process.env.AILOCK_DATA ? path.resolve(process.env.AILOCK_DATA) : path.join(HERE, "..", "data");
const TOKEN = path.join(DATA, "steam", "refresh_token");
const PROTO_DIR = path.join(HERE, "..", "build", "proto"); // cleaned copies from scripts/build_protos.py
const GOOGLE_PROTO = path.join(HERE, "..", ".venv", "lib", "python3.13", "site-packages", "grpc_tools", "_proto");

const DEADLOCK = 1422450;
const MSG = {
  clientWelcome: 4004,
  clientHello: 4006,
  getMatchHistory: 9112,
  getMatchHistoryResponse: 9113,
  getMatchMetaData: 9167,
  getMatchMetaDataResponse: 9168,
};

const log = (...a) => console.error("[steam]", ...a);

function loadProtos() {
  const root = new protobuf.Root();
  root.resolvePath = (_origin, target) =>
    target.startsWith("google/") ? path.join(GOOGLE_PROTO, target) : path.join(PROTO_DIR, target);
  root.loadSync(["gcsdk_gcmessages.proto", "citadel_gcmessages_client.proto"], { keepCase: true });
  return root;
}

async function login() {
  const session = new LoginSession(EAuthTokenPlatformType.SteamClient);
  const { qrChallengeUrl } = await session.startWithQR();
  console.error("Scan with the Steam mobile app (Steam Guard > scan QR code):");
  qrcode.generate(qrChallengeUrl, { small: true }, (q) => console.error(q));
  await new Promise((resolve, reject) => {
    session.on("authenticated", resolve);
    session.on("timeout", () => reject(new Error("QR login timed out")));
    session.on("error", reject);
  });
  fs.mkdirSync(path.dirname(TOKEN), { recursive: true });
  fs.writeFileSync(TOKEN, session.refreshToken, { mode: 0o600 });
  log(`logged in as ${session.accountName}; refresh token saved to ${TOKEN}`);
}

// Log in with the saved token, check nothing is being played, start a Deadlock GC session.
async function connectGC() {
  if (!fs.existsSync(TOKEN)) throw new Error(`no refresh token at ${TOKEN}: run "node gc.mjs login" first`);
  const client = new SteamUser({ autoRelogin: false, renewRefreshTokens: true });
  client.on("refreshToken", (t) => fs.writeFileSync(TOKEN, t, { mode: 0o600 }));
  client.logOn({ refreshToken: fs.readFileSync(TOKEN, "utf8").trim() });
  await new Promise((resolve, reject) => {
    client.once("loggedOn", resolve);
    client.once("error", reject);
  });
  const blocked = await new Promise((resolve) => {
    client.once("playingState", (isBlocked, app) => resolve(isBlocked ? app || true : false));
    setTimeout(() => resolve(false), 5000); // no event: nothing else is playing
  });
  if (blocked) {
    client.logOff();
    throw Object.assign(new Error(`your account is in a game (app ${blocked}); try again after playing`),
      { code: "IN_GAME" });
  }
  client.gamesPlayed([DEADLOCK]);

  const root = loadProtos();
  const hello = root.lookupType("CMsgClientHello").encode({ engine: 1 }).finish(); // k_ESE_Source2
  await new Promise((resolve, reject) => {
    const timer = setInterval(() => client.sendToGC(DEADLOCK, MSG.clientHello, {}, Buffer.from(hello)), 3000);
    client.sendToGC(DEADLOCK, MSG.clientHello, {}, Buffer.from(hello));
    const onMsg = (appid, type) => {
      if (appid === DEADLOCK && type === MSG.clientWelcome) {
        clearInterval(timer);
        client.removeListener("receivedFromGC", onMsg);
        resolve();
      }
    };
    client.on("receivedFromGC", onMsg);
    setTimeout(() => { clearInterval(timer); reject(new Error("game coordinator did not answer")); }, 60000);
  });
  log("game coordinator session ready");
  return { client, root };
}

// Send a request and wait for the response type (one request in flight at a time).
function request(client, root, reqType, reqMsg, body, respType, respMsg, timeoutMs = 30000) {
  const payload = Buffer.from(root.lookupType(reqMsg).encode(body).finish());
  return new Promise((resolve, reject) => {
    const onMsg = (appid, type, data) => {
      if (appid !== DEADLOCK || type !== respType) return;
      client.removeListener("receivedFromGC", onMsg);
      clearTimeout(timer);
      const T = root.lookupType(respMsg);
      resolve(T.toObject(T.decode(data), { longs: Number, enums: String, defaults: false }));
    };
    const timer = setTimeout(() => {
      client.removeListener("receivedFromGC", onMsg);
      reject(new Error(`${reqMsg}: no response`));
    }, timeoutMs);
    client.on("receivedFromGC", onMsg);
    client.sendToGC(DEADLOCK, reqType, {}, payload);
  });
}

// process.exit() right after a big console.log truncates piped output (~64 KB); wait for the write.
const emit = (obj) => new Promise((resolve) => process.stdout.write(JSON.stringify(obj) + "\n", resolve));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function history(args) {
  const { client, root } = await connectGC();
  try {
    const maxPages = args.includes("--max-pages") ? Number(args[args.indexOf("--max-pages") + 1]) : 1000;
    const accountId = Number(args.find((a) => /^\d+$/.test(a) && args[args.indexOf(a) - 1] !== "--max-pages"))
      || client.steamID.accountid;
    const matches = [];
    let cursor;
    for (let page = 0; page < maxPages; page++) {
      const resp = await request(client, root, MSG.getMatchHistory, "CMsgClientToGCGetMatchHistory",
        { account_id: accountId, ...(cursor ? { continue_cursor: cursor } : {}) },
        MSG.getMatchHistoryResponse, "CMsgClientToGCGetMatchHistoryResponse");
      if (resp.result !== "k_eResult_Success") throw new Error(`match history: ${resp.result}`);
      const batch = resp.matches || [];
      matches.push(...batch.map((m) => ({ account_id: accountId, ...m })));
      log(`page ${page + 1}: ${batch.length} matches (total ${matches.length})`);
      if (!batch.length || !resp.continue_cursor || resp.continue_cursor === cursor) break;
      cursor = resp.continue_cursor;
      await sleep(1000);
    }
    await emit(matches);
  } finally {
    client.logOff();
  }
}

async function salts(ids) {
  const { client, root } = await connectGC();
  try {
    // The GC rate-limits metadata lookups with a quota that outlasts any wait we can do in one session
    // (observed: > 8 min, then the session drops). Retry briefly, then report the rest as rate limited so
    // the caller keeps what it got and retries later.
    let limited = false;
    for (const id of ids) {
      let r = { result: "k_eResult_RateLimited" };
      for (let attempt = 0; !limited && attempt < 3; attempt++) {
        r = await request(client, root, MSG.getMatchMetaData, "CMsgClientToGCGetMatchMetaData",
          { match_id: Number(id) }, MSG.getMatchMetaDataResponse, "CMsgClientToGCGetMatchMetaDataResponse");
        if (r.result !== "k_eResult_RateLimited") break;
        log(`rate limited at match ${id}; waiting ${15 * (attempt + 1)} s`);
        await sleep(15000 * (attempt + 1));
      }
      if (r.result === "k_eResult_RateLimited" && !limited) {
        limited = true;
        log("still rate limited; skipping remaining lookups");
      }
      await emit({
        match_id: Number(id), result: r.result, cluster_id: r.replay_group_id ?? null,
        metadata_salt: r.metadata_salt ?? null, replay_salt: r.replay_salt ?? null,
      });
      if (!limited) await sleep(2000);
    }
  } finally {
    client.logOff();
  }
}

const [cmd, ...args] = process.argv.slice(2);
const commands = { login: () => login(), history: () => history(args), salts: () => salts(args) };
if (!commands[cmd]) {
  console.error("usage: node gc.mjs login | history [account_id] [--max-pages N] | salts <match_id>...");
  process.exit(2);
}
commands[cmd]()
  .then(() => process.exit(0))
  .catch((e) => { log(e.message); process.exit(e.code === "IN_GAME" ? 3 : 1); });
