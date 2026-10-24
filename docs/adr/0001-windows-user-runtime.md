# ADR-0001: Self-contained Windows user runtime

## Status
Accepted for isolated implementation; release requires user approval.

## Context
The complete developer preview exposes environment setup to nontechnical users. The user approved a Windows edition with no Python/terminal steps and a two-service initial configuration, preserving complete capabilities and existing data.

## Decision
Bundle a CPython Windows x64 runtime, the complete wheel and dependencies. Use a small GUI launcher and tray lifecycle; keep the current browser UI. Program files and a new desktop-specific per-user data home are separate. Duplicate launch only opens an identity-checked existing instance. Graceful exit waits for the existing draining mechanism, not a timeout kill. Configuration tests remain explicit and journalled.

## Consequences
Larger downloads, but no end-user package installation or global environment modifications. Python security updates, native extensions, font/component notices and OCR models become release-maintenance responsibilities. Unattended monitoring stops during sleep/shutdown. No bundled service credentials. Existing homes need explicit migration, not silent reuse. Code signing and fresh-machine validation remain release gates.

## Alternatives
- Auto-install global Python: smaller initial download, but keeps permissions, PATH and package-network failures in first use.
- Electron/Tauri shell now: adds another runtime/build surface without removing backend packaging work. Defer until browser presentation is demonstrably insufficient.
- PyInstaller one-file: attractive executable, but dynamic imports, parser subprocesses and OCR/native dependencies add freeze-specific changes. Prefer an ordinary runtime first and verify the complete installed package.

## References
- https://docs.python.org/3.13/using/windows.html#the-embeddable-package
- https://www.python.org/downloads/release/python-31316/
- https://pystray.readthedocs.io/en/latest/usage.html
