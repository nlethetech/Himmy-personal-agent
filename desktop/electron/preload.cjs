// Safe bridge. The renderer talks to the local backend over HTTP directly; here we expose
// only the few native capabilities it needs (file picker + resolving dropped-file paths).
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("himmy", {
  backendPort: process.env.HIMMY_APP_PORT || "8131",
  platform: process.platform,
  // Open the native "Add papers" dialog → array of chosen PDF paths.
  pickPapers: () => ipcRenderer.invoke("library:pickFiles"),
  // Resolve a dropped File to its absolute path (Electron 32+ way).
  pathForFile: (file) => {
    try { return webUtils.getPathForFile(file); }
    catch { return ""; }
  },
  // Settings / sync helpers.
  revealData: () => ipcRenderer.invoke("app:revealData"),
  pickZip: () => ipcRenderer.invoke("app:pickZip"),
  // Open a URL in the system browser (used by the Google sign-in flow).
  openExternal: (url) => ipcRenderer.invoke("app:openExternal", url),
  // Show a native macOS notification (fired when a scheduled routine produces a result).
  notify: (payload) => ipcRenderer.invoke("notify:show", payload),
});
