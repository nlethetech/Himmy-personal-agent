// Himmy — Electron main process.
// Responsibilities: (1) start the Python himmy backend as a child process, (2) launch Zotero
// hidden as the invisible library engine, (3) open the native macOS window onto the React UI.
const { app, BrowserWindow, shell, dialog, ipcMain, Notification } = require("electron");
const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const crypto = require("node:crypto");

// Per-launch shared secret. The backend requires it (X-Himmy-Token) on the sensitive
// provider/key endpoints so no other process or web page on this machine can read/clear the
// user's keys or trigger an outbound provider call. Generated fresh each launch, passed to
// the backend via env and to the renderer via preload — it never touches disk.
const APP_TOKEN = process.env.HIMMY_APP_TOKEN || crypto.randomBytes(32).toString("hex");
// Make the token visible to the preload (which reads process.env) so the renderer can send it.
process.env.HIMMY_APP_TOKEN = APP_TOKEN;

// Native "Add papers" file picker — returns the chosen PDF paths to the renderer.
ipcMain.handle("library:pickFiles", async () => {
  const res = await dialog.showOpenDialog(mainWindow ?? undefined, {
    title: "Add papers",
    buttonLabel: "Add",
    properties: ["openFile", "multiSelections"],
    filters: [{ name: "PDF", extensions: ["pdf"] }],
  });
  return res.canceled ? [] : res.filePaths;
});

// Show a native macOS notification when a scheduled routine produces a result.
// Clicking it focuses the Himmy window so the user can open the notifications inbox.
ipcMain.handle("notify:show", async (_e, payload) => {
  if (!Notification.isSupported()) return false;
  const { title, body } = payload || {};
  const n = new Notification({
    title: String(title || "Himmy"),
    body: String(body || ""),
    silent: false,
  });
  n.on("click", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
  n.show();
  return true;
});

// Reveal Himmy's library folder in Finder (so it can be moved into iCloud/Dropbox).
ipcMain.handle("app:revealData", async () => {
  const p = path.join(__dirname, "..", "..", ".scholar-desk");
  shell.openPath(p);
  return p;
});

// Open a URL in the system browser (Google OAuth sign-in opens here, not in-app).
ipcMain.handle("app:openExternal", async (_e, url) => {
  if (typeof url === "string" && /^https?:\/\//.test(url)) {
    await shell.openExternal(url);
    return true;
  }
  return false;
});

// Pick a backup .zip to restore from.
ipcMain.handle("app:pickZip", async () => {
  const res = await dialog.showOpenDialog(mainWindow ?? undefined, {
    title: "Restore from a Himmy backup",
    filters: [{ name: "Himmy backup", extensions: ["zip"] }],
    properties: ["openFile"],
  });
  return res.canceled ? null : res.filePaths[0];
});

const REPO_ROOT = path.join(__dirname, "..", ".."); // …/Himmy
const IS_WIN = process.platform === "win32";
// A venv puts the interpreter at .venv\Scripts\python.exe on Windows, .venv/bin/python on Unix.
const VENV_PYTHON = IS_WIN
  ? path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
  : path.join(REPO_ROOT, ".venv", "bin", "python");
const BACKEND_PORT = process.env.HIMMY_APP_PORT || "8131";
const DEV_URL = "http://localhost:5173";

let backendProc = null;
let mainWindow = null;

function backendHealthy() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: BACKEND_PORT, path: "/health", timeout: 1200 },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function startBackend() {
  if (await backendHealthy()) {
    console.log("[Himmy] backend already running on :" + BACKEND_PORT);
    return;
  }

  const env = {
    ...process.env,
    HIMMY_APP_PORT: BACKEND_PORT,
    HIMMY_APP_TOKEN: APP_TOKEN,
    PYTHONUNBUFFERED: "1",
  };

  let cmd;
  let args;
  let cwd;

  if (app.isPackaged) {
    // Packaged Himmy.app: run the self-contained, PyInstaller-frozen backend that ships inside
    // the bundle (Resources/himmy-backend/) — no system Python, no venv, no terminal required.
    // PyInstaller names the frozen binary himmy-backend on macOS, himmy-backend.exe on Windows.
    cmd = path.join(
      process.resourcesPath,
      "himmy-backend",
      IS_WIN ? "himmy-backend.exe" : "himmy-backend"
    );
    args = [];
    cwd = path.dirname(cmd);
    // The .app bundle is read-only, so the user's library/memory/keys must live in a real
    // per-user location. (config.py also defaults here when frozen — belt-and-suspenders.)
    env.HIMMY_APP_DATA_DIR =
      env.HIMMY_APP_DATA_DIR || path.join(app.getPath("appData"), "Himmy");
  } else {
    // Dev: run from source via the project venv.
    cmd = VENV_PYTHON;
    args = ["-m", "himmy_app.server"];
    cwd = REPO_ROOT;
  }

  console.log(`[Himmy] starting backend… (${app.isPackaged ? "frozen" : "dev"}) ${cmd}`);
  backendProc = spawn(cmd, args, { cwd, env, stdio: "inherit" });
  backendProc.on("error", (err) =>
    console.error("[Himmy] backend failed to start:", err)
  );
  backendProc.on("exit", (code) =>
    console.log(`[Himmy] backend exited (${code})`)
  );
}

function createWindow() {
  // Per-OS window chrome. macOS keeps the native frosted-glass (vibrancy) look; Windows 11 gets
  // the Mica material with an opaque dark fallback and a standard frame (real min/max/close).
  const isDarwin = process.platform === "darwin";
  const chrome = isDarwin
    ? {
        backgroundColor: "#00000000",      // transparent so the vibrancy material shows
        vibrancy: "under-window",          // native macOS frosted-glass material
        visualEffectState: "active",       // keep the blur lively even when unfocused
        titleBarStyle: "hiddenInset",
        trafficLightPosition: { x: 19, y: 24 },
      }
    : {
        backgroundMaterial: "mica",        // Windows 11 translucent material
        backgroundColor: "#0E1116",        // opaque dark fallback (Win10 / Mica unavailable)
        titleBarStyle: "default",          // standard window frame + controls
      };

  mainWindow = new BrowserWindow({
    width: 1340,
    height: 880,
    minWidth: 1080,
    minHeight: 720,
    ...chrome,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());

  // Open external links in the system browser, not inside the app.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (app.isPackaged) {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  } else {
    mainWindow.loadURL(DEV_URL);
  }
}

app.whenReady().then(async () => {
  await startBackend();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (backendProc && !backendProc.killed) backendProc.kill();
});
