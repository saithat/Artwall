"use strict";

let gallery = null;
let token = "";
let selectedId = null;
let artworkChoices = new Map();
let noticeTimer = null;
let feedbackPending = false;
let preparationTimer = null;
const commentDrafts = new Map();

const $ = (id) => document.getElementById(id);

function node(tag, className = "", text = "") {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text) result.textContent = text;
  return result;
}

function clear(element) { element.replaceChildren(); }

function showNotice(message, error = false) {
  const notice = $("notice");
  notice.textContent = message;
  notice.classList.toggle("error", error);
  notice.classList.add("visible");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => notice.classList.remove("visible"), 4500);
}

async function readResponse(response) {
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}

async function post(path, body) {
  return readResponse(await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Artwall-Token": token },
    body: JSON.stringify(body)
  }));
}

async function reload() {
  const result = await readResponse(await fetch("/api/state", { cache: "no-store" }));
  token = result.csrf_token;
  gallery = result;
  render();
}

function findRecord(id) {
  return [gallery.current, ...gallery.queued, ...gallery.history].find((item) => item && item.id === id);
}

function byline(...parts) { return parts.filter(Boolean).join(" · "); }

function dateLabel(seconds) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function image(source, className, alt) {
  const picture = node("img", className);
  picture.src = source;
  picture.alt = alt;
  picture.loading = "lazy";
  picture.addEventListener("error", () => { picture.alt = "Composition image unavailable"; });
  return picture;
}

function reactionButton(record, kind, compact = true) {
  const symbols = { like: "👍", dislike: "👎", favorite: "☆" };
  const names = { like: "Thumbs up", dislike: "Thumbs down", favorite: "Favorite" };
  const button = node("button", "feedback-button" + (kind === "dislike" ? " dislike" : ""));
  button.type = "button";
  button.setAttribute("aria-label", names[kind] + " " + record.title);
  button.setAttribute("aria-pressed", String(Boolean(record.feedback[kind])));
  button.title = names[kind];
  button.textContent = compact ? symbols[kind] : `${symbols[kind]}  ${names[kind]}`;
  button.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (feedbackPending) return;
    feedbackPending = true;
    document.querySelectorAll(".feedback-button").forEach((control) => { control.disabled = true; });
    try {
      const previousCurrentId = gallery.current?.id;
      const response = await post("/api/feedback", { id: record.id, kind, current_id: previousCurrentId });
      await reload();
      if ($("detailDialog").open &&
          (!findRecord(record.id) || (kind === "dislike" && record.id === previousCurrentId &&
           gallery.current?.id !== previousCurrentId))) {
        $("detailDialog").close();
        selectedId = null;
      }
      showNotice(response.notice);
    } catch (error) { showNotice(error.message, true); }
    finally {
      feedbackPending = false;
      document.querySelectorAll(".feedback-button").forEach((control) => { control.disabled = false; });
    }
  });
  return button;
}

function feedbackGroup(record) {
  const group = node("div", "feedback-group");
  for (const kind of ["like", "dislike", "favorite"]) group.append(reactionButton(record, kind));
  return group;
}

function openDetails(record) {
  selectedId = record.id;
  renderDetails(record);
  $("detailDialog").showModal();
}

function commentForm(record, location) {
  const form = node("form", "comment-form" + (location === "current" ? " current-feedback" : ""));
  const inputId = `${location}CommentInput`;
  const label = node("label", "field-label", "Feedback for the curator");
  label.htmlFor = inputId;
  const text = node("textarea");
  text.id = inputId;
  text.dataset.compositionId = record.id;
  text.maxLength = 5000;
  text.rows = 3;
  text.placeholder = "Too much empty space? An awkward pairing? Tell the curator what works and what to change.";
  text.value = commentDrafts.get(record.id) ?? record.comment ?? "";
  const help = node("p", "field-note", "Saved on this device. Your local model uses this feedback when preparing future wallpapers.");
  help.id = `${inputId}Help`;
  text.setAttribute("aria-describedby", help.id);
  text.addEventListener("input", () => {
    commentDrafts.set(record.id, text.value);
    document.querySelectorAll("textarea[data-composition-id]").forEach((other) => {
      if (other !== text && other.dataset.compositionId === record.id) other.value = text.value;
    });
  });
  const save = node("button", "primary-button", "Save feedback →");
  save.type = "submit";
  form.append(label, text, help, save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    const submitted = text.value;
    try {
      const response = await post("/api/comment", { id: record.id, comment: submitted });
      if (commentDrafts.get(record.id) === submitted) commentDrafts.delete(record.id);
      await reload();
      showNotice(response.notice);
    } catch (error) { showNotice(error.message, true); }
    finally { save.disabled = false; }
  });
  return form;
}

function renderCurrent() {
  const slot = $("current");
  clear(slot);
  const record = gallery.current;
  if (!record) {
    slot.append(node("div", "empty-state", "No wallpaper has been shown yet. Prepare a composition with artwall curate, then artwall next."));
    return;
  }
  const imageButton = node("button", "current-image-button");
  imageButton.type = "button";
  imageButton.setAttribute("aria-label", "View " + record.title + " details");
  imageButton.append(image(record.image, "current-image", "Current wallpaper: " + record.title));
  imageButton.addEventListener("click", () => openDetails(record));
  slot.append(imageButton);

  const meta = node("div", "current-meta");
  const information = node("div");
  information.append(node("h3", "work-title", record.title));
  information.append(node("p", "work-byline", byline(record.artist, record.date)));
  if (record.description) information.append(node("p", "work-description", record.description));
  if (record.artworks.length > 1) information.append(node("p", "work-byline", `Also showing: ${record.artworks.slice(1).map((item) => item.title).join(" · ")}`));
  const controls = node("div", "current-actions");
  controls.append(feedbackGroup(record));
  meta.append(information, controls);
  slot.append(meta);
  const details = node("button", "detail-link", "View artwork details ↗");
  details.type = "button";
  details.addEventListener("click", () => openDetails(record));
  slot.append(details);
  slot.append(commentForm(record, "current"));
}

function renderCards(records, targetId, emptyMessage) {
  const slot = $(targetId);
  clear(slot);
  if (!records.length) {
    slot.append(node("div", "empty-state", emptyMessage));
    return;
  }
  for (const record of records) {
    const card = node("article", "art-card");
    const imageButton = node("button", "art-card-image");
    imageButton.type = "button";
    imageButton.setAttribute("aria-label", "View details for " + record.title);
    imageButton.append(image(record.thumbnail, "", record.title));
    imageButton.addEventListener("click", () => openDetails(record));
    card.append(imageButton);
    const body = node("div", "art-card-body");
    body.append(node("h3", "art-card-title", record.title));
    body.append(node("p", "art-card-meta", byline(record.artist, record.date)));
    const footer = node("div", "art-card-footer");
    footer.append(feedbackGroup(record));
    const detail = node("button", "detail-link", "Details & feedback ↗");
    detail.type = "button";
    detail.addEventListener("click", () => openDetails(record));
    footer.append(detail);
    body.append(footer);
    card.append(body);
    slot.append(card);
  }
}

function safeMuseumLink(value) {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
  } catch { return null; }
}

function renderDetails(record) {
  const content = $("detailContent");
  clear(content);
  content.append(image(record.image, "dialog-image", record.title));
  const body = node("div", "dialog-body");
  body.append(node("p", "eyebrow", record.shown_at ? `SHOWN ${dateLabel(record.shown_at).toUpperCase()}` : "COMING UP"));
  const heading = node("h2", "", record.title);
  heading.id = "detailTitle";
  body.append(heading);
  body.append(node("p", "work-byline", byline(record.artist, record.date)));
  if (record.description) body.append(node("p", "work-description", record.description));
  if (record.relationship) body.append(node("p", "work-description", record.relationship));
  if (record.artworks.length) {
    body.append(node("p", "dialog-section-title", record.artworks.length === 1 ? "THE WORK" : "WORKS IN THIS COMPOSITION"));
    const list = node("ul", "dialog-works");
    for (const art of record.artworks) {
      const item = node("li");
      const linkUrl = safeMuseumLink(art.url);
      const title = linkUrl ? node("a", "", art.title) : node("strong", "", art.title);
      if (linkUrl) { title.href = linkUrl; title.target = "_blank"; title.rel = "noopener noreferrer"; }
      item.append(title, node("span", "", byline(art.artist, art.date, art.source)));
      list.append(item);
    }
    body.append(list);
  }
  const primary = record.artworks.find((art) => art.id === record.primary_artwork_id);
  if (primary) {
    const blocked = gallery.blacklisted_artworks.some((art) => art.id === primary.id);
    const blacklist = node("button", "text-button", blocked ? "Allow this artwork again ↗" : "Exclude this artwork from future rotation ↗");
    blacklist.type = "button";
    blacklist.addEventListener("click", () => changeBlacklist("artwork", { id: primary.id, blocked: !blocked }));
    body.append(blacklist);
  }
  body.append(node("p", "dialog-section-title", "YOUR IMPRESSION"));
  const reactions = node("div", "dialog-feedback");
  for (const kind of ["like", "dislike", "favorite"]) reactions.append(reactionButton(record, kind, false));
  body.append(reactions);
  body.append(commentForm(record, "detail"));
  content.append(body);
}

function blacklistChip(label, onRemove) {
  const chip = node("span", "blacklist-chip", label);
  const remove = node("button", "", "×");
  remove.type = "button";
  remove.title = "Remove " + label + " from blacklist";
  remove.setAttribute("aria-label", remove.title);
  remove.addEventListener("click", onRemove);
  chip.append(remove);
  return chip;
}

function renderBlacklist() {
  $("blacklistedArtists").replaceChildren(...gallery.blacklisted_artists.map((artist) =>
    blacklistChip(artist, () => changeBlacklist("artist", { artist, blocked: false }))));
  $("blacklistedWorks").replaceChildren(...gallery.blacklisted_artworks.map((art) => {
    const chip = blacklistChip(art.title, () => changeBlacklist("artwork", { id: art.id, blocked: false }));
    chip.title = byline(art.title, art.artist);
    return chip;
  }));
  const options = $("artworkOptions");
  clear(options);
  artworkChoices = new Map();
  const artistOptions = $("artistOptions");
  clear(artistOptions);
  for (const name of [...new Set(gallery.artworks.map((art) => art.artist))].sort()) {
    if (name === "Artist unknown") continue;
    const option = node("option");
    option.value = name;
    artistOptions.append(option);
  }
  for (const art of gallery.artworks) {
    const label = `${art.title} — ${art.artist} [${art.id}]`;
    artworkChoices.set(label, art.id);
    const option = node("option");
    option.value = label;
    options.append(option);
  }
}

async function changeBlacklist(kind, payload) {
  try {
    const response = await post(`/api/blacklist/${kind}`, payload);
    await reload();
    showNotice(response.notice);
  } catch (error) { showNotice(error.message, true); }
}

function renderSettings() {
  $("interval").value = gallery.settings.interval_minutes;
  $("model").value = gallery.settings.model;
  const chosen = document.querySelector(`input[name="count"][value="${gallery.settings.max_artworks}"]`);
  if (chosen) chosen.checked = true;
  renderBlacklist();
}

function render() {
  renderCurrent();
  $("queueCount").textContent = `(${gallery.queued.length})`;
  renderCards(gallery.queued, "queue", "No upcoming compositions. Choose Prepare new wallpapers to add some.");
  renderCards(gallery.history, "history", "Your recently shown works will appear here.");
  renderSettings();
  renderPreparation(gallery.preparation);
  if ($("detailDialog").open && selectedId) {
    const record = findRecord(selectedId);
    if (record) renderDetails(record);
  }
}

function renderPreparation(preparation) {
  clearTimeout(preparationTimer);
  const running = preparation.status === "running";
  $("prepareButton").disabled = running;
  $("prepareButton").textContent = running ? "Preparing…" : "Prepare new wallpapers";
  $("preparationStatus").textContent = preparation.message;
  if (running) preparationTimer = setTimeout(pollPreparation, 3000);
}

async function pollPreparation() {
  try {
    const preparation = await readResponse(await fetch("/api/preparation", { cache: "no-store" }));
    renderPreparation(preparation);
    if (preparation.status !== "running") await reload();
  } catch (error) {
    $("preparationStatus").textContent = "Unable to check progress. Reconnecting…";
    preparationTimer = setTimeout(pollPreparation, 5000);
  }
}

async function loadModels() {
  try {
    const result = await readResponse(await fetch("/api/models"));
    const options = $("modelOptions");
    clear(options);
    for (const name of result.models) {
      const option = node("option");
      option.value = name;
      options.append(option);
    }
    $("modelHelp").textContent = result.models.length ?
      `${result.models.length} installed Ollama model${result.models.length === 1 ? "" : "s"} found. Choose one with vision support for artwork curation.` :
      "No local Ollama models detected right now. You can still enter a model name.";
  } catch { /* The model list is optional; the configured name remains editable. */ }
}

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const chosen = document.querySelector('input[name="count"]:checked');
  try {
    const response = await post("/api/settings", {
      interval_minutes: Number($("interval").value),
      model: $("model").value.trim(),
      max_artworks: Number(chosen.value)
    });
    await reload();
    showNotice(response.notice);
  } catch (error) { showNotice(error.message, true); }
});

$("artistForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const artist = $("artistInput").value.trim();
  if (!artist) return;
  await changeBlacklist("artist", { artist, blocked: true });
  $("artistInput").value = "";
});

$("workForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const identifier = artworkChoices.get($("workInput").value.trim());
  if (!identifier) { showNotice("Choose an artwork from the suggestions.", true); return; }
  await changeBlacklist("artwork", { id: identifier, blocked: true });
  $("workInput").value = "";
});

$("refreshButton").addEventListener("click", async () => {
  try { await reload(); showNotice("Gallery is up to date"); }
  catch (error) { showNotice(error.message, true); }
});
$("prepareButton").addEventListener("click", async () => {
  $("prepareButton").disabled = true;
  try {
    const response = await post("/api/curate", {});
    await pollPreparation();
    showNotice(response.notice);
  } catch (error) {
    $("prepareButton").disabled = false;
    showNotice(error.message, true);
  }
});
$("closeDialog").addEventListener("click", () => $("detailDialog").close());
$("detailDialog").addEventListener("click", (event) => {
  if (event.target === $("detailDialog")) $("detailDialog").close();
});

reload().then(loadModels).catch((error) => showNotice(error.message, true));
