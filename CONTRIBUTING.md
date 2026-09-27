# Contributing to Artwall

Artwall is Python 3.11+ with Pillow. The CLI, local gallery, renderer, SQLite
storage, and museum adapter live in one package. A local vision model is useful
for preparing wallpapers but is not needed for development or tests.

## Set up

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
bash scripts/check
```

The check script compiles the package and runs the offline test suite, including
Hypothesis-generated gallery settings cases. CI runs the same command. For a focused test, run
`python -m unittest tests/test_gui.py -v` (substitute the relevant file).

## Find the right place to change

| Change | Start with |
| --- | --- |
| Museum records or image downloads | `artwall/sources/`, `artwall/models.py` |
| Decision and composition types | `artwall/models.py` |
| Selection, prompts, model transport, or layout coverage | `artwall/curation.py` |
| Layout, cropping, color, or captions | `artwall/render.py` |
| Rotation, navigation, or votes | `artwall/rotation.py` |
| SQLite records | `artwall/storage.py` |
| Desktop integration | `artwall/desktop/` |
| Gallery interactions | `artwall/gui.py`, `artwall/gui_assets/` |
| User settings | `artwall/config.py`, `artwall/config.example.toml` |

Keep provider and operating-system details at their boundaries. For example,
a new model server should produce the same validated curator decision as the
current one; a new wallpaper backend should accept the same rendered path.
Prefer small functions and explicit data structures. Avoid broad abstractions
until two working implementations show what they need to share.

## Review checklist

- The change has a focused test when behavior or a boundary contract changes.
- Tests are offline and do not depend on a live desktop session.
- Museum facts and rights remain sourced from the museum record.
- Existing cached wallpapers still rotate if curation is unavailable.
- User-visible behavior and configuration changes are documented in `README.md`.
- `bash scripts/check` passes.

For visual changes, attach a representative wallpaper preview and inspect it
at the target aspect ratio. Unit tests cannot establish whether art placement
or typography looks good.
