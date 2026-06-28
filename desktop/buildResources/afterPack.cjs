// electron-builder afterPack hook — ad-hoc code-sign the macOS bundle.
//
// We ship UNSIGNED (no paid Apple Developer ID), but a downloaded .app is quarantined by
// Gatekeeper, and an UNSIGNED nested helper binary (our PyInstaller-frozen Python backend)
// can be killed when the app tries to spawn it — which would silently break the app for
// everyone who installs it. Ad-hoc signing (`codesign --sign -`, free, no certificate) makes
// every nested binary VALID code, so once the user approves the app (right-click → Open the
// first time) it can launch its backend normally.
//
// This does NOT remove the one-time "unidentified developer" prompt — only a real Developer ID
// + notarization does that. It just makes the app actually WORK after the user opens it.

const { execFileSync } = require("node:child_process");
const path = require("node:path");

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;

  const product = context.packager.appInfo.productFilename; // e.g. "Himmy"
  const appPath = path.join(context.appOutDir, `${product}.app`);
  const backend = path.join(
    appPath,
    "Contents",
    "Resources",
    "himmy-backend",
    "himmy-backend"
  );

  console.log(`[afterPack] ad-hoc codesigning bundle: ${appPath}`);
  // Sign inside-out: the frozen backend executable first (with its own dylibs via --deep),
  // then the whole app bundle. Ad-hoc identity is "-".
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", backend], {
    stdio: "inherit",
  });
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", appPath], {
    stdio: "inherit",
  });

  // Fail the build loudly if the resulting signature isn't valid.
  execFileSync("codesign", ["--verify", "--deep", "--verbose=2", appPath], {
    stdio: "inherit",
  });
  console.log("[afterPack] ad-hoc signature verified ✓");
};
