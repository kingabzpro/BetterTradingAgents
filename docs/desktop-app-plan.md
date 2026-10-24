# P2.20 Desktop app: feasibility review and build plan

Planning phase, 2026-10-24. Research: two Tavily deep-research reports (shell comparison, packaging and release) plus pywebview 6.2.1 and Tauri 2 docs verified via Context7. This doc is the feasibility review the roadmap item calls for; no app code exists yet.

## Scope (from the roadmap item)

One installer per platform (Windows and Linux), the server starts on launch, the UI opens in its own window, no terminal and no browser tab. The FastAPI core, SQLite storage, and vanilla web UI stay unchanged; no frontend build step. The work is the shell, packaging, and an update path, staying local-only. Web mode (`uv run app`) remains the developer path and is never removed.

## Recommendation

**pywebview + PyInstaller onedir + Inno Setup (Windows) and AppImage (Linux).** The desktop shell is one new thin module and one Python dependency; v1 update path is a version-check banner linking to GitHub Releases, no silent auto-update yet.

## Shell comparison

| Option | Window | Frontend build | New toolchain | Added weight | Verdict |
|---|---|---|---|---|---|
| pywebview | System webview: WebView2 on Windows, WebKitGTK on Linux | None | None (one Python dep) | ~10-15 MB shell | **Recommended** |
| Tauri 2 + Python sidecar | System webview | Static files work, but Rust config + capability JSON | Rust toolchain, sidecar target-triple naming | ~5-10 MB core; our sidecar dwarfs it | Overkill here |
| Electron + Python sidecar | Bundled Chromium | Node toolchain expected | Node | 120 MB+ baseline | Rejected |
| BeeWare Briefcase | System webkit | None | Briefcase config | ~25 MB core | Runner-up; built for Toga apps, FastAPI+webview is not its path |

Why pywebview wins for this app: installer size is dominated by our own Python dependency tree (CrewAI and friends, roughly 150 packages) no matter which shell wraps it, so Tauri's tiny core buys nothing. pywebview adds a single dependency and zero toolchains, points a native window straight at `http://127.0.0.1:<port>`, and has a `js_api` bridge if we later want native menus, dialogs, or tray integration.

## Architecture

New `app/desktop.py` behind a `desktop` entry point, about a hundred lines:

1. Resolve the per-user data dir and set `DB_PATH` (and sibling paths) in the environment before `app.config` is imported, same layering the check suite already uses.
2. Single-instance probe: if `/api/health` answers on our port, an instance is already running, so open a window on it and exit; otherwise pick a free port (default 8000, ephemeral fallback).
3. Start uvicorn (`app.main:app`, 127.0.0.1) in a thread.
4. `webview.create_window(..., url=http://127.0.0.1:<port>, private_mode=False, storage_path=<data dir>/webview)` then `webview.start()`.
5. On window close: shut the server down and exit.

The P2.11 loopback guards are untouched: the webview navigates to `127.0.0.1`, so Host/Origin checks pass exactly as in a browser.

## Packaging (build)

Freezer: **PyInstaller, onedir, never onefile.** Nuitka is the fallback if antivirus false positives persist; cx_Freeze is rejected (lags on wheel support).

- Onefile extracts to temp on every launch (slow cold start on a several-hundred-MB tree) and draws more antivirus scrutiny; onedir starts fast and is debuggable.
- CrewAI, pydantic, and httpx use dynamic imports: expect to enumerate `hiddenimports` in the spec (community-documented for CrewAI projects). Iterate with `--debug=imports`.
- Recent PyInstaller fixed DLL collection from uv-created venvs, but the build must be verified against our exact `uv.lock`, not a pip venv.
- No UPX in release builds; the size win is not worth the antivirus friction.
- Expected installed size: roughly 300-500 MB, dominated by the crewai/chromadb tree. P1.8 (drop CrewAI) would cut most of it later. Torch is already gone from the tree (P2.19 parked), which keeps the freeze feasible.
- Windows: Inno Setup script produces `setup.exe` with icon, shortcuts, optional run-after-install. Linux: AppImage first (portable, single file, GPG-signable); deb only if asked.

## Data and paths

- A frozen install lives in read-only locations (Program Files), so `portfolio.db`, `backtests/`, and caches must move to a per-user data dir: `%APPDATA%\BetterTradingAgents` on Windows, `~/.local/share/BetterTradingAgents` on Linux. `DB_PATH` via env already supports this.
- Keyring is unchanged: Windows Credential Manager / Linux Secret Service.
- pywebview defaults to `private_mode=True`, which wipes localStorage every launch. The UI keeps `client_id` and notification preferences in localStorage, so the shell must set `private_mode=False` with `storage_path` under the data dir.
- First run in a fresh data dir is today's onboarding flow. Migrating an existing dev `portfolio.db` is a manual copy, documented in the README.

## Release pipeline

- GitHub Actions matrix job (windows-latest, ubuntu-22.04) triggered on version tags: freeze, build installer and AppImage, attach artifacts to a GitHub Release.
- Signing options:

| Option | Cost | Effect |
|---|---|---|
| Self-signed | Free | SmartScreen and antivirus warnings for everyone; fine for private use only |
| OV certificate | ~$200-300/yr | Builds reputation over time; no instant SmartScreen bypass (EV no longer grants one either) |
| Azure Trusted Signing | ~$10/mo | Microsoft-managed, best reputation path, requires identity vetting |

- v1 call: ship unsigned for owner use and revisit signing when distribution widens.
- Hygiene that applies whenever we do sign: RFC-3161 timestamping, onedir, no UPX, submit false positives to vendors.

## Update path

- v1: on startup the app checks a version manifest (GitHub Releases `latest.json`) and shows a banner with a download link when a newer release exists; the user installs manually; the data dir survives reinstalls.
- v2 (later, only if wanted): signed in-app updater; AppImageUpdate on Linux.

## Milestones

- **M1 spike:** `uv run desktop` runs the real app in a pywebview window on Windows; a PyInstaller onedir build launches it. Exit: manual smoke on this machine.
- **M2 packaging:** PyInstaller spec, icon, Inno Setup script, AppImage build, CI release job on tags, headless `check_desktop.py` (port pick, health probe, data-dir resolution; the window itself stays a manual check).
- **M3 polish:** single-instance behavior, first-run onboarding in the data dir, update banner, WebKitGTK dependency notes for Linux.
- **M4 release:** signing decision, GitHub Release artifacts, wiki page.

## Risks and traps

| Risk | Mitigation |
|---|---|
| WebKitGTK missing on minimal Linux distros | Document the packages (`python3-gi`, `webkit2gtk`), or detect and print the fix command |
| WebView2 runtime absent | Present by default on Win10/11; ship the evergreen bootstrapper as fallback |
| Antivirus false positives on frozen builds | onedir, no UPX, eventual signing; Nuitka as fallback |
| CrewAI hidden imports and data files | Spec iteration against the uv.lock venv |
| Port conflicts or double launch | Single-instance `/api/health` probe before boot |
| pywebview private mode wipes localStorage | `private_mode=False` + `storage_path` |
| Two servers sharing one portfolio.db | Single instance; second launch joins the running one |
| Installer size in the hundreds of MB | Acceptable for a local app; P1.8 shrinks it later |

## Open decisions for the owner

1. Signing budget: none now (recommended) vs Azure Trusted Signing vs OV certificate.
2. Linux format: AppImage only (recommended) or AppImage plus deb.
3. Tray icon and launch-on-login: wanted, or plain window only.
4. Confirm web mode stays the primary developer path through M1-M3 (recommended).
