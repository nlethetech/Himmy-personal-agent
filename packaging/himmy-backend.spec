# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Himmy's frozen Python backend.

Produces a self-contained ``himmy-backend`` (one-dir) tree that the packaged Himmy.app
spawns. One-dir (not one-file) is deliberate: it starts much faster (no per-launch unpack to
a temp dir), which matters for the "launch within 30 seconds" goal, and the whole tree is
already living inside the .app bundle so a single extra folder is invisible to the user.

The himmy framework registers tools/connectors/providers dynamically (by name), so we pull
in EVERY submodule of ``himmy`` and ``himmy_app`` rather than relying on PyInstaller's static
import graph. The data-/native-heavy deps (onnxruntime, tokenizers, fastembed, the RAG stack)
are collected whole so their dylibs and data files come along.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

# On Windows, console=True builds a console-subsystem exe, so a black cmd window pops up (and
# lingers) every time Electron spawns the backend. Build a WINDOWED exe there (no console). On
# macOS the flag is a no-op for a spawned child, so keep console=True to preserve log flow to
# Electron's inherited stdio. (The frozen entry point also mirrors logs to a file, so a windowed
# Windows exe is still diagnosable.)
_CONSOLE = sys.platform != "win32"

datas = []
binaries = []
hiddenimports = []

# Ship the agent spec (agent/agent.yaml) INSIDE the bundle. himmy_app.cli resolves it from
# sys._MEIPASS when frozen, so it lands at <bundle>/_internal/agent/agent.yaml.
_REPO_ROOT = os.path.dirname(SPECPATH)  # SPECPATH = …/packaging
datas += [(os.path.join(_REPO_ROOT, "agent"), "agent")]


def _add_all(pkg):
    """collect_all -> append its datas/binaries/hiddenimports."""
    d, b, h = collect_all(pkg)
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# --- Our own code: every submodule, so dynamic by-name imports resolve --------------------
hiddenimports += collect_submodules("himmy")
hiddenimports += collect_submodules("himmy_app")
# himmy ships prompt templates / yaml / configs as package data.
for pkg in ("himmy", "himmy_app"):
    try:
        _add_all(pkg)
    except Exception:
        pass

# --- ASGI server stack (uvicorn loads its protocol/loop impls dynamically) -----------------
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
]

# --- Data-/native-heavy deps: collect whole (dylibs + data files) --------------------------
for pkg in (
    "openai",  # OpenRouter / OpenAI / custom-compatible providers — imported lazily by himmy
    "keyring",  # OS credential store: macOS Keychain / Windows Credential Manager (DPAPI) / Linux
    "fastembed",
    "onnxruntime",
    "tokenizers",
    "huggingface_hub",
    "nepali_datetime",
    "cronsim",  # cron-grammar routines (scheduled automations) — imported dynamically by himmy
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_core",
    "anyio",
    "sse_starlette",
    "httpx",
    "httpcore",
    "certifi",
):
    try:
        _add_all(pkg)
    except Exception:
        # Optional / not installed — skip rather than fail the whole build.
        pass

# A few small libs PyInstaller sometimes misses in this stack.
hiddenimports += [
    "h11",
    "httptools",
    "websockets",
    "wsproto",
    "click",
    "multipart",
    "email_validator",
    "charset_normalizer",
    "idna",
    "sniffio",
    # openai SDK runtime deps (loaded at import time)
    "jiter",
    "distro",
    "tqdm",
    "typing_extensions",
    # keyring's OS credential backends are discovered dynamically — name the one for THIS
    # platform so the frozen backend can store the user's API key encrypted (not in plaintext).
    "keyring.backends.fail",
]
if sys.platform == "win32":
    # Windows Credential Manager (DPAPI) via keyring + its pywin32-ctypes runtime shim.
    hiddenimports += [
        "keyring.backends.Windows",
        "win32ctypes.core",
        "win32ctypes.pywin32.win32cred",
    ]
elif sys.platform == "darwin":
    hiddenimports += ["keyring.backends.macOS"]
else:
    hiddenimports += ["keyring.backends.SecretService"]


a = Analysis(
    ["himmy_backend_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI / notebook / plotting toolkits we never use — keep the bundle lean.
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "notebook",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="himmy-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=_CONSOLE,  # mac/linux: True (logs to inherited stdio); Windows: False (no cmd window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="himmy-backend",
)
