"""In-app provider API-key store — let a non-coder set Himmy up *in the app* (no .env).

A user picks an AI provider, pastes their key, and Himmy stores it through himmy's
**writable secrets** layer — the *same* mechanism the Google sign-in uses
(:func:`himmy.config.secrets.get_writable_provider`). On macOS (the app's target platform)
that is the login **keychain**, where the value is protected by the OS and never written to
disk in plaintext.

SECURITY — storage at rest:
  * macOS keychain (the default; the app forces ``HIMMY_SECRETS=keychain``): encrypted by
    the OS, the only supported production path.
  * Off-macOS / keychain unavailable: himmy's ``FileSecrets`` backend writes the key to a
    ``0600`` file under the secrets dir. This file is **NOT encrypted** — it is plaintext
    with restrictive permissions only. We therefore refuse to silently store a key this way:
    :func:`set_key` raises a clear error unless ``HIMMY_ALLOW_PLAINTEXT_SECRETS=1`` is set,
    so a non-macOS user must explicitly opt in to plaintext-at-rest.

This module *writes* and *checks presence of* keys. It NEVER returns a stored key value to a
caller and NEVER logs one. ``is_configured`` returns only a boolean.
"""

from __future__ import annotations

import os

from himmy.config.secrets import get_secret, get_writable_provider

#: provider id -> the SECRET-STORE KEY NAME its API key lives under (these are *names*,
#: never values). Mirrors what ``himmy.cli.provider`` reads for each provider:
#:   * openrouter        -> OPENROUTER_API_KEY
#:   * openai            -> OPENAI_API_KEY
#:   * anthropic         -> ANTHROPIC_API_KEY
#:   * openai-compatible -> HIMMY_OPENAI_COMPAT_API_KEY (also needs a base_url via /models)
#:   * ollama            -> (no key — local, private; just pick a model)
PROVIDER_KEY_NAMES: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai-compatible": "HIMMY_OPENAI_COMPAT_API_KEY",
}

#: Human-facing metadata for each provider the onboarding/Settings UI offers.
PROVIDER_META: dict[str, dict[str, object]] = {
    "openrouter": {
        "label": "OpenRouter",
        "key_url": "https://openrouter.ai/keys",
        "blurb": "One key, hundreds of models, very cheap. Recommended.",
        "recommended": True,
        "default_model": "google/gemini-2.5-flash",
    },
    "openai": {
        "label": "OpenAI",
        "key_url": "https://platform.openai.com/api-keys",
        "blurb": "GPT models straight from OpenAI.",
        "recommended": False,
        "default_model": "gpt-4o-mini",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_url": "https://console.anthropic.com/settings/keys",
        "blurb": "Claude models straight from Anthropic.",
        "recommended": False,
        "default_model": "claude-haiku-4-5-20251001",
    },
    "openai-compatible": {
        "label": "Custom endpoint",
        "key_url": "",
        "blurb": "A self-hosted OpenAI-compatible endpoint (also needs its address).",
        "recommended": False,
        "default_model": None,
    },
    "ollama": {
        "label": "Local (Ollama)",
        "key_url": "https://ollama.com/download",
        "blurb": "Private — runs on your computer, no key needed.",
        "recommended": False,
        "default_model": None,
    },
}

#: Display order for the 5 providers the UI shows.
PROVIDER_ORDER: tuple[str, ...] = (
    "openrouter",
    "openai",
    "anthropic",
    "openai-compatible",
    "ollama",
)

#: A pasted key must be at least this long to be plausibly real (guards empty/typo input
#: without ever inspecting the value's content).
_MIN_KEY_LEN = 8
#: An absurdly long paste is almost certainly not a key — reject before it reaches a store.
_MAX_KEY_LEN = 8192


class ProviderKeyError(ValueError):
    """A user-facing, key-redacting validation error (the message is safe to show)."""


def needs_key(provider: str) -> bool:
    """True when this provider requires an API key (everything except local Ollama)."""
    return provider in PROVIDER_KEY_NAMES


def _key_name(provider: str) -> str:
    name = PROVIDER_KEY_NAMES.get(provider)
    if not name:
        raise ProviderKeyError(f"{provider} does not use an API key.")
    return name


def _writable():  # type: ignore[no-untyped-def]
    """The active writable secrets backend (keychain/file), or raise a clear error.

    Mirrors ``himmy.api.studio_google._writable`` — the app forces
    ``HIMMY_SECRETS=keychain`` (see ``himmy_app.config.load_config``) so this is
    normally the macOS keychain or, off-macOS, an encrypted file store.
    """
    w = get_writable_provider()
    if w is None:
        raise ProviderKeyError(
            "Himmy can't save keys on this machine "
            "(the secrets store is read-only). "
            "Set HIMMY_SECRETS=keychain or file."
        )
    return w


def is_configured(provider: str) -> bool:
    """True iff this provider has a usable credential right now.

    Key-needing providers: a non-empty secret is present under their key name. Ollama
    needs no key, so it is considered configured only when its local server is reachable
    (probed by the caller for the top-level ``ready`` flag — here we report False since
    there is no stored credential to check).
    """
    if not needs_key(provider):
        return False
    return bool(get_secret(PROVIDER_KEY_NAMES[provider]))


def set_key(provider: str, key: str) -> None:
    """Validate + persist ``key`` for ``provider`` via the writable secrets backend.

    The value is written straight into the keychain/file store (in-process; on macOS it
    never touches argv) and is NEVER echoed back or logged. Raises
    :class:`ProviderKeyError` (a safe, value-free message) on bad input.
    """
    name = _key_name(provider)
    cleaned = (key or "").strip()
    if not cleaned:
        raise ProviderKeyError("Paste your key first.")
    if len(cleaned) < _MIN_KEY_LEN:
        raise ProviderKeyError("That key looks too short — double-check it.")
    if len(cleaned) > _MAX_KEY_LEN:
        raise ProviderKeyError("That doesn't look like an API key.")
    backend = _writable()
    # SECURITY: the macOS keychain encrypts at rest; himmy's FileSecrets backend writes
    # PLAINTEXT (0600 only). Refuse to silently store an API key in plaintext — the user
    # must explicitly opt in (or, on Mac, use the keychain, which is the default).
    if type(backend).__name__ == "FileSecrets" and os.environ.get(
        "HIMMY_ALLOW_PLAINTEXT_SECRETS", ""
    ).strip().lower() not in ("1", "true", "yes"):
        raise ProviderKeyError(
            "Himmy won't store your key in a plaintext file. "
            "On a Mac, use the keychain (HIMMY_SECRETS=keychain). "
            "To allow a plaintext 0600 file anyway, set HIMMY_ALLOW_PLAINTEXT_SECRETS=1."
        )
    backend.set(name, cleaned)


def clear_key(provider: str) -> None:
    """Remove the stored key for ``provider`` (idempotent — absent is success)."""
    name = _key_name(provider)
    _writable().delete(name)


def key_url(provider: str) -> str:
    """The page where the user gets a key for ``provider`` (empty for custom/ollama)."""
    return str(PROVIDER_META.get(provider, {}).get("key_url") or "")


def default_model(provider: str) -> str | None:
    """A sensible starter model id for ``provider`` (None for custom/ollama)."""
    val = PROVIDER_META.get(provider, {}).get("default_model")
    return str(val) if val else None


__all__ = [
    "PROVIDER_KEY_NAMES",
    "PROVIDER_META",
    "PROVIDER_ORDER",
    "ProviderKeyError",
    "needs_key",
    "is_configured",
    "set_key",
    "clear_key",
    "key_url",
    "default_model",
]
