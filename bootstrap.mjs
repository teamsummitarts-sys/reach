import { createHash } from "node:crypto";
import { readFileSync, unlinkSync } from "node:fs";
import { spawnSync } from "node:child_process";

const encryptedBundle = "street-banker-v2-render.enc";
const decryptedBundle = "street-banker-v2-render.tar.gz";
const expectedSha256 = "a9d2821d55079c3577cc3ca31cfd316a3061ed1fe7e36c85e708c2d41d17f1a0";

if (!process.env.V2_BUNDLE_KEY) {
  throw new Error("V2_BUNDLE_KEY is required to unpack this deployment bundle");
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: "inherit", ...options });
  if (result.status !== 0) {
    throw new Error(`${command} failed with status ${result.status}`);
  }
}

run("openssl", [
  "enc",
  "-d",
  "-aes-256-cbc",
  "-pbkdf2",
  "-iter",
  "200000",
  "-in",
  encryptedBundle,
  "-out",
  decryptedBundle,
  "-pass",
  "env:V2_BUNDLE_KEY",
]);

const archive = readFileSync(decryptedBundle);
const actualSha256 = createHash("sha256").update(archive).digest("hex");
if (actualSha256 !== expectedSha256) {
  throw new Error("Decrypted V2 bundle failed its integrity check");
}

const listing = spawnSync("tar", ["-tzf", decryptedBundle], { encoding: "utf8" });
if (listing.status !== 0) {
  throw new Error("V2 bundle is not a valid tar archive");
}
for (const path of listing.stdout.split("\n").filter(Boolean)) {
  if (path.startsWith("/") || path.split("/").includes("..")) {
    throw new Error(`Unsafe path in V2 bundle: ${path}`);
  }
}

run("tar", ["-xzf", decryptedBundle, "-C", "."]);
unlinkSync(decryptedBundle);
console.log("Street Banker V2 deployment bundle unpacked and verified");
