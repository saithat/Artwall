# Artwall

A local, open-source-friendly museum curator for a Wayland desktop. It discovers public-domain works from the Art Institute of Chicago, asks a **local vision model** to select and compose them, renders 3440 × 1440 wallpapers with small editorial captions, and keeps a ready-to-show offline queue. One artwork per wallpaper is the default.

Artwork is never generated or recolored. The museum supplies titles, artists, dates, rights, and source pages. The model chooses the work, layout, and short source-grounded caption. Rendering is deterministic. A narrow work remains intact against a softly blurred, subdued backdrop drawn from its colors. A fine border and soft shadow distinguish the sharp original from its surround, with the caption in the exposed space when it fits.

## First wallpaper

This Fedora/Niri setup has DMS (DankMaterialShell), which artwall auto-detects for both desktop wallpaper and lock screen. `awww` and `swww` wallpaper adapters are also available if installed. The same rendered composition is sent to the desktop and DMS lock screen.

```bash
uv tool install --editable .
artwall init
artwall refresh
artwall curate --count 3
artwall next
```

`artwall next --no-apply` updates the local current composition without contacting the graphical session; it is useful from a terminal outside Niri. The canonical current files are `~/.cache/artwall/current.png` and `current.json` (unless XDG paths are customized). Source originals and queued PNGs live under `~/.local/share/artwall/`.

The default local curator is the installed `qwen3.6:27b` vision model through Ollama. Keep the Ollama daemon running when preparing new compositions. A failed or unavailable model falls back to deterministic, metadata-based curation. Rotation itself never calls Ollama or a museum API. To prepare a queue entirely without the model:

```bash
artwall curate --offline --count 5
```

The Art Institute asks clients to download one image at a time with about a second between requests. An initial refresh and cache fill can take a few minutes. Later runs reuse SQLite API responses and image files. Cached and downloaded JPEGs must fully decode before curation. A damaged cache file is downloaded again; if its replacement fails, that candidate is skipped so other works can still be prepared. A work the museum serves only below 1400 pixels is remembered and not downloaded again. Curation runs started together (a timer, the command line, and the gallery) re-check the queue before rendering, so no work is queued twice. Scheduled refreshes advance through results pages, so the local catalog continues to expand; `artwall refresh --page N` targets a page explicitly.

## Everyday commands

```text
artwall next                    Next cached composition
artwall gui                     Open the local curator interface
artwall previous                Return to an earlier composition
artwall like | dislike          Teach taste; dislike also shows the next work
artwall favorite | skip         Save or move on
artwall more-from-artist        Increase the current artist's score
artwall less-from-artist        Decrease it
artwall blacklist-current       Exclude the primary work
artwall blacklist-artist        Exclude the primary artist
artwall info                    All works, caption, relationship, museum links
artwall open                    Open the primary museum page
artwall history | favorites     Inspect local records
artwall status                  Queue and current image
artwall debug-current           Raw museum metadata and curator decision
artwall render --output FILE    Re-render the current decision
artwall refresh                 Update museum metadata
artwall curate --count 14       Prepare future wallpapers
artwall curate --count 3 --next Prepare wallpapers to show ahead of the old queue
```

The commands are intentionally small so they can be bound in `~/.config/niri/config.kdl`. For example, a Niri `spawn` binding can run `artwall next` or `artwall favorite`; see Niri's syntax for the version installed on your machine.

## Local interface

Run `artwall gui` to open the local interface at `http://127.0.0.1:8765`. It shows the current wallpaper, upcoming rotation, and history. You can like, dislike, or comment on a composition; set the rotation interval and local Ollama model; maintain artist and artwork blacklists; and allow one, two, or three concurrent works. It serves only on loopback and uses no remote web assets. The command `artwall gui --no-open` starts the interface without opening a browser.

**Prepare new wallpapers**, beside **Coming next**, prepares up to three wallpapers in the background using saved settings and feedback. Progress and failures appear beside the button. New results go to the front of the queue; the current wallpaper, older queued work, history, and favorites are preserved. Use it after saving layout settings or feedback to see their effect without waiting through an older batch. The command-line equivalent is `artwall curate --count 3 --next`; ordinary scheduled curation continues to append to the queue.

The **Feedback for the curator** field appears directly below the current wallpaper. Upcoming and past compositions have the same field under **Details & feedback**. Describe spacing, pairings, captions, or what you like about the work. Notes are saved locally, can be edited or cleared, and the eight most recently edited notes are supplied to the local model with their artwork and layout context during future curation (up to 800 characters per note and about 4,000 characters in total, newest first). Feedback guides preferences; it cannot override museum facts or validation. It does not re-render existing wallpapers, and offline curation does not interpret written feedback.

Thumbs down on the current work immediately shows the next eligible cached composition. Thumbs down on an upcoming work removes it from rotation. The disliked work stays out of future curation until that feedback is reversed. If no alternative is cached, the dislike is saved and the gallery says why the wallpaper stayed put. A favorite includes a like; removing the favorite removes that like as well. Artist blacklists match names regardless of capitalization.

Lowering the artwork limit filters previously queued compositions that exceed it immediately. Raising the limit allows more works in newly prepared layouts; it does not rearrange already rendered wallpapers. Save the setting, then choose **Prepare new wallpapers** to put fresh layouts next in rotation. Previously viewed compositions remain in history.

With **Up to two** or **Up to three**, a work that already fits the display stays alone. A narrow selection is compared with related pairs and, when allowed, trios at the actual display proportions. The coverage check prefers balanced groups with compatible colors and tonal contrast. Whole works share aligned edges, a common height, and a small consistent gap. If a suitable grouping is unavailable, the intact original keeps its diffuse surround; an enlarged detail is only used when explicitly chosen by the curator. Existing cached PNGs retain their prepared layouts; `artwall curate` prepares wallpapers with the current layout rules, while `artwall render --output FILE` previews the current decision with the updated renderer.

Wallpaper captions wrap titles, metadata, companion labels, and the prepared description without cutting them off at a fixed number of lines. Line spacing follows the measured font size. If a complete caption is too tall, the renderer tries a wider column and then smaller type, keeping the text and its shadow inside the display margins. Full museum titles and catalog details remain available in the gallery and `artwall info`.

## Scheduling

After the first wallpaper works in the Niri session:

```bash
artwall install-timers
```

This writes five `systemd --user` units under `~/.config/systemd/user/` and enables a login service plus the rotation and daily curation timers. The login service advances one cached wallpaper when the time since the last successful wallpaper change exceeds the configured interval. Rotation then shows another every two hours by default. Daily curation refreshes ArtIC metadata and renders the next queue. The interface updates an installed rotation timer when you change the interval. If you edit `[rotation].interval_minutes` by hand, run `artwall install-timers` again. `artwall install-timers --no-enable` writes the units without enabling them.

The rotation timer starts with the graphical session and does not catch up missed intervals while the computer is off. At login, Artwall keeps the current wallpaper if it changed within the interval; after a longer gap it advances once. Each later timer run also advances once. A failed desktop update leaves that work queued for the next attempt. The login service retries briefly if the wallpaper backend is still starting. Run `artwall next --if-due` to apply the same check manually.

## Configuration

`artwall init` writes `~/.config/artwall/config.toml` from [config.example.toml](artwall/config.example.toml). Defaults target 3440 × 1440, but width and height are configurable. Values must use their TOML types (for example `true`, not `"true"`); a mistyped value is refused rather than guessed. Notable options:

- `[wallpaper].backend`: `auto`, `dms`, `awww`, or `swww`.
- `[lockscreen].backend`: `auto`, `dms`, `hyprlock`, or `none` for a separate lock image.
- `[lockscreen].public_lock_screen_safe`: conservative metadata keyword filtering when `true`. This is a best-effort filter, not image classification.
- `[llm].enabled`, `model`, and `base_url`: local Ollama vision curation. No cloud API key is used.
- `[curation].max_artworks`: `1` by default; raise to `2` or `3` to allow paired works again.
- `[curation].explore_fraction` and `wildcard_fraction`: retain discovery alongside learned favorites.

When using `awww` or `swww`, start its daemon in your Niri session. On this machine, DMS is already the active wallpaper and lock-screen layer, so `auto` selects it. The DMS backend passes an immutable composition path on every rotation to avoid stale wallpaper caching.

With Hyprlock, artwall configures its background to reference `~/.cache/artwall/current.png` once and leaves other lock-screen settings intact. If the Hyprlock configuration only includes sourced files, set its background path manually to the current image or use a direct background block; artwall does not guess which included file to rewrite.

## How it works

1. **Retrieve:** ArtIC's API supplies public-domain artworks with source URLs and rights metadata. SQLite caches API responses and normalized records.
2. **Filter and rank:** Recent works and blacklisted items are excluded; taste seeds, explicit artist feedback, novelty, resolution, and a configurable mix of exploration select a candidate pool.
3. **Curate:** Ollama receives a labeled contact sheet of cached thumbnails, museum metadata, a short composition rubric, and recent written feedback. This request uses a 16,384-token context window so the rules, image, and up to 24 candidates are never silently truncated. It returns a constrained JSON decision. With the default limit it selects one work. Local geometry checks compare intact single, paired, and grouped works for useful display coverage. If the selection changes, a second local vision pass describes the final works. Local validation checks IDs, crop bounds, layout counts, and unsupported historical claims. A metadata-based fallback works without the model.
4. **Render:** Pillow renders a wide work full bleed with only a very mild crop. Other proportions remain whole against a low-contrast, heavily blurred backdrop derived from the artwork. Only the backdrop is blurred and subdued; a thin edge and soft shadow separate the original from its surround. Pairs and trios keep their original proportions and paper margins at a shared height. Captions go in a side or lower surround if they fit; otherwise placement evaluates four image corners and adds a localized gradient when needed. Same-work detail templates remain available when the artwork limit is raised and the curator chooses a meaningful detail.
5. **Queue and show:** PNGs and decisions are stored before display. `next` sets a cached image through the wallpaper adapter, updates the lock screen, and records history and feedback in SQLite.

Source interfaces, desktop backends, and the renderer are separate modules so additional museums or display tools can be added without changing the queue and CLI.

## Composition guidance

The curator's bundled rubric adapts [Getty's Principles of Design](https://www.getty.edu/education/teachers/building_lessons/formal_analysis2.html) and the [National Gallery of Art's Elements of Art: Color](https://www.nga.gov/educational-resources/elements-art/elements-art-color). Getty provides a vocabulary for balance, emphasis, movement, proportion, rhythm, and unity; the Gallery offers concrete ways to assess color relationships and atmosphere. The wallpaper-specific rules apply those ideas to visual weight, compatible palettes and tonal density, aligned edges, restrained captions, and purposeful negative space. These are selection guidelines, not a score that can guarantee taste. The references are documented here; the short rubric ships locally and requires no web retrieval during curation.

## Current scope

This is the first vertical slice. ArtIC is the only museum adapter. Crop safety combines the local vision model with conservative geometry and local image-detail heuristics; there is no dedicated face detector or embedding model yet. Public lock-screen filtering uses metadata keywords. The renderer supports one, two, or three works, including a magnified detail of one work. More sources, stronger visual understanding, and learned image similarity are natural next steps.

## Development

The default tests are end-to-end scenarios that drive the CLI and gallery server in a temporary home. They cover rights and lock-screen filtering, blacklists, dislike navigation, database upgrades opened by several processes at once, and a failed wallpaper update followed by a later login. Hypothesis generates settings cases for the gallery API. Fake `dms` and `systemctl` commands stand in for the desktop; the default check needs no network, model, or graphical session. Install the test extra (`python -m pip install -e '.[test]'`) before running:

```bash
bash scripts/check
```

To check curation against a running local Ollama and installed vision model, run `bash scripts/check-model`. It uses `qwen3.6:27b` by default; set `ARTWALL_TEST_MODEL` or `ARTWALL_TEST_OLLAMA_URL` for another local setup. This live check is separate from CI.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and module ownership.
`AGENTS.md` gives coding agents the same project contracts and check command.

The project is MIT licensed. Museum images retain their own rights metadata and source URL; only ArtIC records explicitly marked public domain are admitted by the current adapter. See [ArtIC API and IIIF documentation](https://api.artic.edu/docs/) for their current usage guidance.
