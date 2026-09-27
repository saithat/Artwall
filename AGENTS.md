# Working on Artwall

Artwall is a local museum-art wallpaper application. Keep the artwork dominant,
museum facts authoritative, and rotation usable without a network or model.

## Start here

- Read `README.md` for the current behavior and `CONTRIBUTING.md` for setup.
- Run `bash scripts/check` before and after a change. Its tests need no network,
  museum account, graphical session, or running model.
- Keep each change focused. Do not rewrite unrelated modules or add a framework
  to solve a small local problem.

## Code map

- `artwall/models.py`: source-neutral artwork, curator decision, and composition types.
- `artwall/sources/`: museum adapters, rights checks, and image retrieval.
- `artwall/curation.py`: candidate scoring, curator decisions, validation, and layout coverage.
- `artwall/render.py`: deterministic wallpaper layouts and captions.
- `artwall/storage.py`: SQLite catalog, queue, history, and feedback.
- `artwall/rotation.py`: offline rotation, navigation history, and votes.
- `artwall/desktop/`: wallpaper and lock-screen adapters.
- `artwall/config.py`: TOML configuration and validation.
- `artwall/cli.py`: command orchestration; keep substantial logic in modules.
- `artwall/gui.py` and `artwall/gui_assets/`: local gallery API and interface.
- `tests/`: offline contract and behavior tests.

## Contracts to preserve

- Museum metadata determines title, artist, date, rights, and source URL.
  Missing facts stay missing. Model output is untrusted and must pass local
  validation before rendering or storage.
- Cached compositions rotate offline. A failed source or model call cannot
  prevent showing an already prepared wallpaper.
- The renderer never stretches or recolors artwork. Its output dimensions
  match the configured display. A single artwork is the default.
- Platform-specific wallpaper, lock-screen, and scheduling behavior belongs
  behind adapters. Do not introduce a Niri or systemd dependency into the
  source, curation, storage, or rendering modules.
- Default tests are end-to-end scenarios through the CLI and gallery server,
  in a temporary home with fake desktop commands on PATH (`tests/support.py`).
  Network, Ollama, and desktop commands are not prerequisites for `scripts/check`.
  The explicit `scripts/check-model` uses a real local Ollama vision model.

When changing a contract, update the relevant test and README in the same
change. Add a new dependency only when its concrete benefit outweighs the
packaging and cross-platform cost.
