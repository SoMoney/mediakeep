(function () {
  "use strict";

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    const match = document.cookie.match(/(?:^|; )mediakeep_csrf=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function headers(withBody) {
    const h = { "X-CSRF-Token": csrfToken() };
    if (withBody) h["Content-Type"] = "application/json";
    return h;
  }

  async function api(method, path, body) {
    const opts = {
      method: method,
      headers: headers(body !== undefined),
      credentials: "same-origin"
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    if (!r.ok) {
      const text = await r.text();
      let detail = text;
      try {
        const data = JSON.parse(text);
        if (data && data.detail) detail = String(data.detail);
      } catch (err) {
        // keep raw text
      }
      throw new Error(detail || r.statusText);
    }
    if (r.status === 204) return {};
    return r.json();
  }

  function get(path) {
    return api("GET", path, undefined);
  }

  function post(path, body) {
    return api("POST", path, body || {});
  }

  function del(path, body) {
    return api("DELETE", path, body || {});
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function cleanText(value) {
    let text = String(value == null ? "" : value);
    const pairs = [
      ["needs review", "needs a look"],
      ["needs Review", "needs a look"],
      ["Needs review", "needs a look"],
      ["identity", "match"],
      ["Identity", "Match"],
      ["IDENTITY", "MATCH"],
      ["rename in place", "rename here"],
      ["Rename in place", "Rename here"],
      ["override", "change"],
      ["Override", "Change"],
      ["OVERRIDE", "CHANGE"],
      ["batch", "item"],
      ["Batch", "Item"],
      ["plan", "item"],
      ["Plan", "Item"],
      ["hint", "fix"],
      ["Hint", "Fix"]
    ];
    for (let i = 0; i < pairs.length; i++) {
      text = text.split(pairs[i][0]).join(pairs[i][1]);
    }
    return text;
  }

  function announce(message) {
    const el = document.getElementById("job-progress");
    if (!el) return;
    el.textContent = message;
    el.classList.add("visible");
    window.setTimeout(function () {
      el.classList.remove("visible");
    }, 7000);
  }

  function isVisible(el) {
    return !!(el && el.offsetParent !== null);
  }

  // A row counts when it is shown, or only folded away under "Show all" (Execute shows
  // the first 3 of each group). Rows hidden by a filter don't count.
  function counted(el) {
    return isVisible(el) || Boolean(el && el.closest(".js-hidden-row"));
  }

  function rowBoxes(list) {
    return Array.prototype.slice.call(document.querySelectorAll("[data-select-row]")).filter(function (el) {
      return el.getAttribute("data-list") === list && counted(el);
    });
  }

  function kindWord(kind) {
    const map = {
      movie: "movie",
      show: "TV show",
      dvd: "DVD",
      bluray: "Blu-ray",
      collection: "collection"
    };
    return map[kind] || "item";
  }

  function statusText(status) {
    const map = {
      ready: "Ready",
      needs: "Needs you",
      left: "Left alone",
      tidy: "Already tidy",
      unknown: "Not recognised"
    };
    return map[status] || cleanText(status || "");
  }

  function normPath(path) {
    return String(path || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  }

  function rowChoiceIsInPlace(box) {
    const tr = box.closest("tr");
    if (!tr) return false;
    return tr.dataset.mode === "in_place";
  }

  function diffHtml(parts) {
    return (parts || []).map(function (part) {
      const text = escapeHtml(part && part.text ? part.text : "");
      if (part && part.op === "added") return '<ins data-diff-added>' + text + "</ins>";
      if (part && part.op === "removed") return '<del data-diff-removed>' + text + "</del>";
      return text;
    }).join(" ");
  }

  function titleChoiceHtml(choice) {
    if (!choice) return "";
    const key = escapeHtml(choice.item_key || "");
    const keepChecked = choice.current === "keep" ? " checked" : "";
    const tmdbChecked = choice.current === "tmdb" ? " checked" : "";
    return (
      '<fieldset class="title-choice" data-title-choice-row>' +
      "<legend>Keep your title or use TMDB's</legend>" +
      '<label><input type="radio" name="title-choice-' + key + '" value="keep" data-title-choice data-item-key="' + key + '"' + keepChecked + ">" +
      '<span>Keep your title: <span class="mono">' + escapeHtml(choice.keep_name || "") + "</span></span></label>" +
      '<label><input type="radio" name="title-choice-' + key + '" value="tmdb" data-title-choice data-item-key="' + key + '"' + tmdbChecked + ">" +
      "<span>Use TMDB&#39;s: <span class=\"mono\">" + escapeHtml(choice.tmdb_name || "") + "</span></span></label>" +
      "</fieldset>"
    );
  }

  function thinksHtml(card, status) {
    let html =
      '<div class="thinks">' +
      escapeHtml(kindWord(card.kind)) +
      " · " +
      escapeHtml(cleanText(card.label || "")) +
      "</div>";

    if (status === "needs" && card.why_needs) {
      html += '<div class="why" data-why-needs>' + escapeHtml(card.why_needs) + "</div>";
    } else if (status === "ready" && card.identity && card.identity.title) {
      html += '<div class="why">Matched as ' + escapeHtml(cleanText(card.identity.title)) + "</div>";
    }

    if (card.issues && card.issues.length && card.issues[0].message) {
      html += '<div class="issue">' + escapeHtml(cleanText(card.issues[0].message)) + "</div>";
    }

    if (card.title_choice) {
      html += titleChoiceHtml(card.title_choice);
    }

    return html;
  }

  function goHtml(card, status) {
    if (status === "needs") {
      const path = card.best_guess_target || card.in_place_target || card.target || card.current_path || "";
      return (
        '<div class="best-guess"><span>If you confirm the top match:</span> ' +
        '<span class="path-cell new-path" data-path-cell="true" data-best-guess>' +
        escapeHtml(path) +
        "</span></div>"
      );
    }

    if (status === "tidy") {
      return '<div class="muted">Already named right – nothing to do</div>' +
        '<span class="path-cell" data-path-cell="true">' +
        escapeHtml((card.in_place ? card.in_place_target : (card.target || card.current_path)) || "") + "</span>";
    }

    if (card.poster_only && (status === "ready" || card.has_changes)) {
      return (
        '<div class="outcome" data-outcome="poster"><span class="outcome-verb">Already named right · save the poster</span> → ' +
        '<span class="path-cell new-path" data-path-cell="true">' + escapeHtml(card.poster || "") + "</span></div>"
      );
    }

    if (status === "ready" || card.has_changes) {
      let html = "";
      if (card.current_name && card.new_name && card.current_name !== card.new_name) {
        html += '<div class="name-diff" data-name-diff>' + diffHtml(card.name_diff) + "</div>";
      }

      const here = Boolean(card.in_place);
      const lib = escapeHtml(card.file_library || card.library || "the destination");
      let verb;
      if (here) verb = "Rename here";
      else if (card.current_name === card.new_name) verb = "Move into " + lib + ", same name";
      else verb = "Move into " + lib;
      html +=
        '<div class="outcome" data-outcome="' + (here ? "in_place" : "file") + '">' +
        '<span class="outcome-verb">' + verb + "</span> → " +
        '<span class="path-cell new-path" data-path-cell="true">' +
        escapeHtml(card.new_path || card.target || card.in_place_target || card.current_path || "") +
        "</span></div>";
      // "… instead" only makes sense when a destination folder is chosen.
      const hasDestination = Boolean(document.querySelector("[data-destination-path]"));
      if (hasDestination && !card.same_place && card.file_target) {
        const key = escapeHtml(card.item_key || "");
        html += '<div class="outcome-alt">';
        if (here) {
          html += '<button type="button" class="link-button" data-row-mode="file" data-item-key="' + key + '">Move into ' +
            escapeHtml(card.file_library || "the destination") + " instead</button>";
        } else {
          html += '<button type="button" class="link-button" data-row-mode="in_place" data-item-key="' + key + '">Rename here instead</button>';
        }
        if (card.folder_mode && card.folder_mode !== (here ? "in_place" : "file")) {
          html += ' <span class="tag">just this item</span>';
        }
        html += "</div>";
      }
      return html;
    }

    if (card.target) {
      return '<span class="path-cell new-path" data-path-cell="true">' + escapeHtml(card.target) + "</span>";
    }
    if (card.in_place_target) {
      return '<span class="path-cell new-path" data-path-cell="true">' + escapeHtml(card.in_place_target) + "</span>";
    }
    return '<span class="muted">Nothing to do</span>';
  }

  function statusFromCard(card) {
    if (!card) return "left";
    if (card.needs) return "needs";
    if (card.ready) return "ready";
    if (!card.has_changes && card.identity) return "tidy";
    return "left";
  }

  function updateInboxRowFromCard(row, card) {
    if (!row || !card) return;
    const status = statusFromCard(card);
    row.dataset.status = status;
    row.dataset.samePlace = card.same_place ? "true" : "false";
    row.dataset.mode = card.in_place ? "in_place" : "file";

    const checkCell = row.querySelector(".check-cell");
    if (checkCell) {
      if (status !== "ready") {
        checkCell.innerHTML = "";
      } else if (!checkCell.querySelector("[data-select-row]")) {
        const box = document.createElement("input");
        box.type = "checkbox";
        box.className = "checkbox";
        box.setAttribute("data-select-row", "");
        box.setAttribute("data-list", "inbox");
        box.dataset.keys = String(card.item_key || "");
        box.dataset.files = String(card.file_count || 1);
        box.setAttribute("aria-label", "Select " + (card.label || "item"));
        checkCell.appendChild(box);
      }
    }

    const pill = row.querySelector(".pill");
    if (pill) {
      pill.className = "pill pill-" + status;
      pill.textContent = statusText(status);
    }
    if (status !== "needs") {
      row.querySelectorAll('.row-action a[href^="/match/"]').forEach(function (a) {
        const wrap = a.closest(".row-action");
        if (wrap) wrap.remove();
      });
    }

    const thinksCell = row.querySelector("[data-thinks-cell]");
    if (thinksCell) thinksCell.innerHTML = thinksHtml(card, status);

    const goCell = row.querySelector("[data-go-cell]");
    if (goCell) goCell.innerHTML = goHtml(card, status);

    updateAllCounts();
  }

  function errorTarget(el) {
    return (
      el.closest("[data-setting-error-target]") ||
      el.closest(".inline-form") ||
      el.closest("td") ||
      el.closest(".panel")
    );
  }

  function clearFieldError(el) {
    const target = errorTarget(el);
    if (!target) return;
    const err = target.querySelector("[data-setting-error]");
    if (err) {
      err.hidden = true;
      err.textContent = "";
    }
  }

  function showFieldError(el, message) {
    let target = errorTarget(el);
    if (target && target.tagName === "TR") {
      const td = target.querySelector("td");
      if (td) target = td;
    }
    let err = target ? target.querySelector("[data-setting-error]") : null;
    if (!err && target) {
      err = document.createElement("div");
      err.className = "field-error";
      err.setAttribute("data-setting-error", "");
      target.appendChild(err);
    }
    if (err) {
      err.textContent = message;
      err.hidden = false;
    }
    announce(message);
  }

  function updateAllCounts() {
    document.querySelectorAll("[data-select-all]").forEach(function (header) {
      const list = header.dataset.list || "";
      const rows = rowBoxes(list);
      const checked = rows.filter(function (r) { return r.checked; });
      header.checked = rows.length > 0 && checked.length === rows.length;
      header.indeterminate = checked.length > 0 && checked.length < rows.length;
    });

    document.querySelectorAll("[data-count-list]").forEach(function (el) {
      const list = el.dataset.countList || "";
      el.textContent = String(rowBoxes(list).filter(function (r) { return r.checked; }).length);
    });

    document.querySelectorAll("[data-file-list]").forEach(function (el) {
      const list = el.dataset.fileList || "";
      let files = 0;
      rowBoxes(list).filter(function (r) { return r.checked; }).forEach(function (r) {
        files += parseInt(r.dataset.files || "0", 10);
      });
      el.textContent = String(files);
    });

    document.querySelectorAll("[data-file-into-library-count]").forEach(function (el) {
      const list = el.dataset.fileIntoLibraryCount || "";
      let count = 0;
      rowBoxes(list).filter(function (r) { return r.checked; }).forEach(function (r) {
        if (!rowChoiceIsInPlace(r)) count += 1;
      });
      el.textContent = String(count);
    });

    document.querySelectorAll("[data-in-place-count]").forEach(function (el) {
      const list = el.dataset.inPlaceCount || "";
      let count = 0;
      rowBoxes(list).filter(function (r) { return r.checked; }).forEach(function (r) {
        if (rowChoiceIsInPlace(r)) count += 1;
      });
      el.textContent = String(count);
    });

    document.querySelectorAll("[data-button-count]").forEach(function (el) {
      const list = el.dataset.buttonCount || "";
      el.textContent = String(rowBoxes(list).filter(function (r) { return r.checked; }).length);
    });

    const executeBox = document.getElementById("execute-groups");
    if (executeBox) {
      const rows = Array.prototype.slice.call(executeBox.querySelectorAll("[data-select-row]")).filter(counted);
      const checked = rows.filter(function (r) { return r.checked; });
      let files = 0;
      checked.forEach(function (r) { files += parseInt(r.dataset.files || "0", 10); });
      document.querySelectorAll("[data-count-scope='execute']").forEach(function (el) {
        el.textContent = String(checked.length);
      });
      document.querySelectorAll("[data-file-scope='execute']").forEach(function (el) {
        el.textContent = String(files);
      });
    }
  }

  async function updateExecuteBadge() {
    try {
      const rows = await get("/api/queue");
      const badge = document.getElementById("execute-badge");
      if (badge) badge.textContent = String(rows.length);
    } catch (err) {
      // ignore badge refresh failures
    }
  }

  // Settings apply straight away (they never touch media files); the page then reloads,
  // after a rescan when the naming changed.
  let settingsNeedRescan = false;

  async function saveSetting(path, value, label) {
    const payload = { path: path, value: value, label: label || ("Set " + path) };
    const result = await post("/api/settings/apply", { payload: payload });
    if (result && result.rescan) settingsNeedRescan = true;
    announce("Saved: " + ((result && result.label) || payload.label));
    return result;
  }

  function finishSettingsChange() {
    if (settingsNeedRescan) {
      settingsNeedRescan = false;
      announce("Saved. Rescanning your folders…");
      startScan(null);
    } else {
      window.location.reload();
    }
  }

  async function saveSettingFromElement(el) {
    clearFieldError(el);
    const path = el.dataset.settingPath;
    if (!path) return;
    let value = el.value;
    if (el.type === "checkbox") value = el.checked;
    if (el.dataset.secret === "true" && value === "") return;
    if (value === "********") return;
    try {
      await saveSetting(path, value, el.dataset.settingLabel);
      finishSettingsChange();
    } catch (err) {
      showFieldError(el, String(err.message || err));
    }
  }

  let pickerPath = null;
  let pickerParent = null;

  function closeFolderPicker() {
    const dialog = document.getElementById("folder-picker");
    if (dialog && dialog.open) dialog.close();
  }

  function cancelFolderPicker() {
    const cb = pickerOnCancel;
    pickerOnCancel = null;
    pickerCallback = null;
    closeFolderPicker();
    if (cb) cb();
  }

  let pickerCallback = null;
  let pickerOnCancel = null;

  function chooseFolder(path) {
    pickerOnCancel = null;
    closeFolderPicker();
    if (pickerCallback) {
      const cb = pickerCallback;
      pickerCallback = null;
      if (path) cb(path);
      return;
    }
  }

  function openPickerAt(path, callback, onCancel) {
    const dialog = document.getElementById("folder-picker");
    if (!dialog) return;
    pickerCallback = callback;
    pickerOnCancel = onCancel || null;
    pickerPath = path || null;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    loadFolders(pickerPath);
  }

  async function setWorkspace(data) {
    const err = document.querySelector("[data-workspace-error]");
    if (err) { err.hidden = true; err.textContent = ""; }
    try {
      await post("/api/workspace", data);
    } catch (e) {
      if (err) { err.textContent = String(e.message || e); err.hidden = false; }
      announce(String(e.message || e));
      return false;
    }
    announce("Saved. Scanning the folder…");
    try {
      await post("/api/jobs/scan", { only: null });
      watchJob(function () { window.location.href = "/inbox"; });
    } catch (e) {
      window.location.href = "/inbox";
    }
    return true;
  }

  function parentPath(path) {
    if (!path) return null;
    const trimmed = path.replace(/[\\/]+$/, "");
    const cut = Math.max(trimmed.lastIndexOf("\\"), trimmed.lastIndexOf("/"));
    if (cut <= 0) return trimmed || null;
    const parent = trimmed.slice(0, cut);
    // "Q:" alone means the drive root.
    return /^[A-Za-z]:$/.test(parent) ? parent + "\\" : parent;
  }

  function workingFolderPath() {
    const el = document.querySelector("[data-working-folder]");
    return el ? el.textContent.trim() : null;
  }

  function restoreDestinationChoice() {
    const hasDest = Boolean(document.querySelector("[data-destination-path]"));
    document.querySelectorAll("[data-workspace-destination]").forEach(function (r) {
      r.checked = (r.value === "move") === hasDest;
      const label = r.closest(".dest-option");
      if (label) label.classList.toggle("selected", r.checked);
    });
  }

  function pickDestination() {
    // Start beside the working folder (its parent), where another folder usually is.
    const current = document.querySelector("[data-destination-path]");
    openPickerAt(
      current ? current.textContent.trim() : parentPath(workingFolderPath()),
      function (path) { setWorkspace({ destination: path }); },
      restoreDestinationChoice
    );
  }

  // "Use this match" on a Needs-you row: update that row in place (no page reload),
  // show that it went to Execute, then let the filter tidy it away.
  async function useQuickMatch(btn) {
    const row = btn.closest("tr");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Adding…";
    try {
      const res = await post("/api/match/" + encodeURIComponent(btn.dataset.itemKey || "") + "/choose", {
        tmdb_id: parseInt(btn.dataset.tmdbId || "0", 10),
        kind: btn.dataset.kind || "movie",
        remember: false,
        source: "inbox"
      });
      announce((res && res.message) || "Match confirmed.");
      updateExecuteBadge().catch(function () {});
      if (!row || !res || !res.card) {
        window.location.reload();
        return;
      }
      const wasNeeds = row.dataset.status === "needs";
      updateInboxRowFromCard(row, res.card);
      const go = row.querySelector("[data-go-cell]");
      const tag = res.queued ? "✓ Added to Execute" : "✓ Match confirmed – already named right";
      if (go) go.insertAdjacentHTML("afterbegin", '<div class="tag ok" data-added>' + escapeHtml(tag) + "</div>");
      if (wasNeeds && row.dataset.status !== "needs") bumpInboxBadge(-1);
      refreshInboxChips();
      window.setTimeout(function () { applyInboxFilter(currentInboxFilter()); }, 1200);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = label;
      announce(String(err.message || err));
    }
  }

  function currentInboxFilter() {
    const active = document.querySelector("[data-filter].active");
    return active ? active.dataset.filter || "all" : "all";
  }

  function refreshInboxChips() {
    const rows = Array.prototype.slice.call(document.querySelectorAll("tr[data-status]"));
    const labels = { ready: "Ready", needs: "Needs you" };
    Object.keys(labels).forEach(function (status) {
      const chip = document.querySelector('[data-filter="' + status + '"]');
      if (!chip) return;
      const n = rows.filter(function (r) { return r.dataset.status === status; }).length;
      chip.textContent = labels[status] + " " + n;
    });
  }

  function bumpInboxBadge(delta) {
    const badge = document.querySelector('.nav-link[href="/inbox"] .badge');
    if (!badge) return;
    const n = Math.max(0, (parseInt(badge.textContent || "0", 10) || 0) + delta);
    if (n === 0) badge.remove();
    else badge.textContent = String(n);
  }

  async function setRowMode(btn) {
    const key = btn.dataset.itemKey;
    if (!key) return;
    btn.disabled = true;
    try {
      await post("/api/item/" + encodeURIComponent(key) + "/in_place", { on: btn.dataset.rowMode === "in_place" });
      await updateExecuteBadge();
      window.location.reload();
    } catch (err) {
      btn.disabled = false;
      announce(String(err.message || err));
    }
  }

  function renderFolderPicker(data) {
    pickerPath = data.path || null;
    pickerParent = data.parent || null;
    const crumb = document.querySelector("[data-folder-crumb]");
    const list = document.querySelector("[data-folder-list]");
    const up = document.querySelector("[data-folder-up]");
    const choose = document.querySelector("[data-folder-choose]");

    if (crumb) {
      crumb.innerHTML = "";
      if (pickerPath) {
        const sep = String(pickerPath).indexOf("\\") >= 0 ? "\\" : "/";
        const isUnc = sep === "\\" && String(pickerPath).startsWith("\\\\");
        const parts = String(pickerPath).split(/[\\/]+/).filter(Boolean);
        if (!parts.length) {
          crumb.textContent = pickerPath;
        } else {
          let current = "";
          parts.forEach(function (part, idx) {
            if (idx === 0 && /^[A-Za-z]:$/.test(part)) {
              current = part + sep;
            } else if (isUnc && idx === 0) {
              current = "\\\\" + part;
            } else if (sep === "/" && idx === 0) {
              current = "/" + part;
            } else {
              current = current ? current + sep + part : part;
            }
            const pathForBtn = current;
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "crumb";
            btn.textContent = part;
            btn.addEventListener("click", function () { loadFolders(pathForBtn); });
            crumb.appendChild(btn);
          });
        }
      } else {
        crumb.textContent = "Choose a folder";
      }
    }

    if (list) {
      list.innerHTML = "";
      const entries = (data.folders || []).concat((data.roots || []).map(function (r) {
        return { name: r.name, path: r.path };
      }));
      entries.forEach(function (f) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = f.name;
        btn.addEventListener("click", function () { loadFolders(f.path); });
        btn.addEventListener("dblclick", function () { chooseFolder(f.path); });
        li.appendChild(btn);
        list.appendChild(li);
      });
      if (!entries.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.textContent = "No subfolders";
        list.appendChild(li);
      }
    }

    if (up) up.disabled = !pickerParent;
    if (choose) choose.disabled = !pickerPath;
  }

  async function loadFolders(path) {
    const list = document.querySelector("[data-folder-list]");
    if (list) list.innerHTML = '<li class="muted">Loading…</li>';
    try {
      const data = await get("/api/folders" + (path ? "?path=" + encodeURIComponent(path) : ""));
      renderFolderPicker(data);
    } catch (err) {
      if (path) {
        try {
          const roots = await get("/api/folders");
          renderFolderPicker(roots);
        } catch (err2) {
          announce(String(err2.message || err2));
        }
      } else {
        announce(String(err.message || err));
      }
    }
  }

  document.addEventListener("click", function (e) {
    const browse = e.target.closest("[data-browse]");
    if (browse) {
      e.preventDefault();
      window.location.href = "/inbox?folder=" + encodeURIComponent(browse.dataset.browse || "");
      return;
    }

    const pickWorkspace = e.target.closest("[data-pick-workspace]");
    if (pickWorkspace) {
      openPickerAt(workingFolderPath(), function (path) { setWorkspace({ folder: path }); });
      return;
    }

    const pickDest = e.target.closest("[data-pick-destination]");
    if (pickDest) {
      e.preventDefault();
      pickDestination();
      return;
    }

    const testTmdb = e.target.closest("[data-test-tmdb]");
    if (testTmdb) {
      const out = document.querySelector("[data-tmdb-result]");
      testTmdb.disabled = true;
      if (out) out.textContent = "Asking TMDB…";
      post("/api/tmdb/test", {})
        .then(function (res) {
          if (out) out.textContent = (res && res.message) || "";
          announce((res && res.message) || "");
        })
        .catch(function (err) { if (out) out.textContent = String(err.message || err); })
        .finally(function () { testTmdb.disabled = false; });
      return;
    }

    const forgetRecent = e.target.closest("[data-forget-recent]");
    if (forgetRecent) {
      post("/api/workspace/forget", { path: forgetRecent.dataset.forgetRecent })
        .then(function () {
          const tr = forgetRecent.closest("tr");
          if (tr) tr.remove();
          announce("Forgotten. The folder itself is untouched.");
        })
        .catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const keep = e.target.closest("[data-keep-match]");
    if (keep) {
      keep.disabled = true;
      post("/api/item/" + encodeURIComponent(keep.dataset.itemKey || "") + "/keep_match", {})
        .then(function (res) {
          announce("Kept. This match won't be questioned again.");
          const row = keep.closest("tr[data-status]");
          if (row && res && res.card) {
            const wasNeeds = row.dataset.status === "needs";
            updateInboxRowFromCard(row, res.card);
            if (wasNeeds && row.dataset.status !== "needs") bumpInboxBadge(-1);
            refreshInboxChips();
            window.setTimeout(function () { applyInboxFilter(currentInboxFilter()); }, 1200);
          } else {
            window.location.reload();
          }
        })
        .catch(function (err) {
          keep.disabled = false;
          announce(String(err.message || err));
        });
      return;
    }

    const quick = e.target.closest("[data-quick-match]");
    if (quick) {
      useQuickMatch(quick);
      return;
    }

    const rowMode = e.target.closest("[data-row-mode]");
    if (rowMode) {
      setRowMode(rowMode);
      return;
    }

    const folderUp = e.target.closest("[data-folder-up]");
    if (folderUp) {
      if (pickerParent) loadFolders(pickerParent);
      return;
    }

    const folderCancel = e.target.closest("[data-folder-cancel]");
    if (folderCancel) {
      cancelFolderPicker();
      return;
    }

    const folderChoose = e.target.closest("[data-folder-choose]");
    if (folderChoose) {
      chooseFolder(pickerPath);
      return;
    }

    const queueBtn = e.target.closest("[data-queue-items]");
    if (queueBtn) {
      queueItems(queueBtn).catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const rescan = e.target.closest("[data-rescan-folder]");
    if (rescan) {
      startScan(rescan.dataset.rescanFolder);
      return;
    }

    const editName = e.target.closest("[data-edit-name]");
    if (editName) {
      const key = editName.dataset.editName;
      const current = editName.dataset.title || "";
      const title = window.prompt("Edit the name by hand", current);
      if (title === null) return;
      post("/api/item/" + encodeURIComponent(key) + "/edit", { title: title, kind: "movie" })
        .then(function () { window.location.reload(); })
        .catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const fixAdd = e.target.closest("[data-fix-add]");
    if (fixAdd) {
      addFix().catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const fixEdit = e.target.closest("[data-fix-edit]");
    if (fixEdit) {
      editFix(fixEdit).catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const fixForget = e.target.closest("[data-fix-forget]");
    if (fixForget) {
      forgetFixes([parseInt(fixForget.dataset.fixId || "0", 10)]).catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const forgetSelected = e.target.closest("[data-forget-selected]");
    if (forgetSelected) {
      const ids = Array.prototype.slice.call(document.querySelectorAll('[data-select-row][data-list="fixes"]'))
        .filter(function (r) { return r.checked; })
        .map(function (r) { return parseInt(r.dataset.fixId || "0", 10); });
      forgetFixes(ids).catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const fixesImport = e.target.closest("[data-fixes-import]");
    if (fixesImport) {
      const input = document.getElementById("import-path");
      const path = input ? input.value.trim() : "";
      if (!path) {
        announce("Enter a file path first.");
        return;
      }
      post("/api/fixes/import", { path: path, scan: true })
        .then(function () { window.location.reload(); })
        .catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const openApply = e.target.closest("[data-open-apply]");
    if (openApply) {
      openApplyDialog().catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const clear = e.target.closest("[data-clear-queue]");
    if (clear) {
      clearQueue().catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const forget = e.target.closest("[data-forget-runs]");
    if (forget) {
      forgetRuns(forget).catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const undoRun = e.target.closest("[data-undo-run]");
    if (undoRun) {
      undoRunId(undoRun.dataset.undoRun).catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const matchSearch = e.target.closest("[data-match-search]");
    if (matchSearch) {
      searchMatch().catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const kindButton = e.target.closest("[data-kind-button]");
    if (kindButton) {
      const page = kindButton.closest("[data-match-key]");
      if (page) {
        page.dataset.currentKind = kindButton.dataset.kindButton;
        page.querySelectorAll("[data-kind-button]").forEach(function (b) {
          const active = b.dataset.kindButton === kindButton.dataset.kindButton;
          b.classList.toggle("active", active);
          b.setAttribute("aria-pressed", active ? "true" : "false");
        });
        updateMatchPreview().catch(function () {});
      }
      return;
    }

    const matchChoose = e.target.closest("[data-match-choose]");
    if (matchChoose) {
      chooseMatch().catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const matchSkip = e.target.closest("[data-match-skip]");
    if (matchSkip) {
      skipMatch().catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    const login = e.target.closest("[data-login]");
    if (login) {
      const token = document.getElementById("login-token");
      post("/api/login", { token: token ? token.value : "" })
        .then(function () { window.location.href = "/inbox"; })
        .catch(function () { announce("That token was not accepted."); });
      return;
    }

    const box = e.target.closest("[data-select-row]");
    if (box) {
      const list = box.dataset.list || "";
      const rows = rowBoxes(list);
      const idx = rows.indexOf(box);
      if (e.shiftKey && lastIndex[list] !== undefined && idx >= 0) {
        const start = lastIndex[list];
        const end = idx;
        rows.slice(Math.min(start, end), Math.max(start, end) + 1).forEach(function (r) {
          r.checked = box.checked;
        });
      } else if (idx >= 0) {
        lastIndex[list] = idx;
      }
      updateAllCounts();
      return;
    }

    const showAll = e.target.closest("[data-show-all]");
    if (showAll) {
      const section = showAll.closest("section");
      if (section) {
        section.querySelectorAll(".js-hidden-row").forEach(function (tr) {
          tr.classList.remove("js-hidden-row");
        });
      }
      showAll.closest("tr").remove();
      updateAllCounts();
      return;
    }
  });

  document.addEventListener("change", function (e) {
    if (e.target.matches("[data-select-all]")) {
      const list = e.target.dataset.list || "";
      rowBoxes(list).forEach(function (r) {
        r.checked = e.target.checked;
      });
      updateAllCounts();
      return;
    }

    if (e.target.matches("[data-select-row]")) {
      updateAllCounts();
      return;
    }

    if (e.target.matches("[data-setting-path]")) {
      saveSettingFromElement(e.target).catch(function (err) { announce(String(err.message || err)); });
      return;
    }

    if (e.target.matches("[data-title-choice]")) {
      const row = e.target.closest("tr[data-item-key]");
      const key = e.target.dataset.itemKey;
      if (!key) return;
      post("/api/item/" + encodeURIComponent(key) + "/title_override", { choice: e.target.value })
        .then(function (card) {
          updateExecuteBadge().catch(function () {});
          if (card && card.message) announce(card.message);
          if (row && card && card.item_key) {
            updateInboxRowFromCard(row, card);
            if (card.queued) {
              const go = row.querySelector("[data-go-cell]");
              if (go) go.insertAdjacentHTML("afterbegin", '<div class="tag ok" data-added>✓ Added to Execute</div>');
            }
          } else {
            window.location.reload();
          }
        })
        .catch(function (err) {
          announce(String(err.message || err));
          window.location.reload();
        });
      return;
    }

    if (e.target.matches("[data-workspace-destination]")) {
      document.querySelectorAll("[data-workspace-destination]").forEach(function (r) {
        const label = r.closest(".dest-option");
        if (label) label.classList.toggle("selected", r.checked);
      });
      if (e.target.value === "here") setWorkspace({ destination: "" });
      else pickDestination();
      return;
    }

    if (e.target.matches("[data-recent-folder]")) {
      if (e.target.value) setWorkspace({ folder: e.target.value });
      return;
    }


    if (e.target.matches("[data-source-select]")) {
      window.location.href = "/inbox?folder=" + encodeURIComponent(e.target.value || "");
      return;
    }

    if (e.target.matches(".cond-field")) {
      const row = e.target.closest(".cond-row");
      if (!row) return;
      const genre = row.querySelector(".cond-genre");
      const country = row.querySelector(".cond-country");
      const language = row.querySelector(".cond-language");
      if (genre) genre.classList.toggle("hidden", e.target.value !== "genre");
      if (country) country.classList.toggle("hidden", e.target.value !== "country");
      if (language) language.classList.toggle("hidden", e.target.value !== "language");
      return;
    }

    if (e.target.matches("[data-new-fix-action]")) {
      const value = e.target.value;
      document.querySelectorAll("[data-new-extra]").forEach(function (el) {
        const kind = el.getAttribute("data-new-extra");
        el.classList.toggle("hidden", kind !== value);
      });
      return;
    }

    if (e.target.matches("[data-edit-action]")) {
      const form = e.target.closest(".edit-form");
      if (!form) return;
      form.querySelectorAll("[data-extra-tmdb], [data-extra-kind]").forEach(function (el) {
        el.classList.toggle("hidden", e.target.value !== "match");
      });
    }
  });

  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
      const scope = e.target.closest("#execute-groups") || e.target.closest("[data-select-list]");
      if (!scope) return;
      e.preventDefault();
      if (scope.id === "execute-groups") {
        Array.prototype.slice.call(scope.querySelectorAll("[data-select-row]")).filter(counted).forEach(function (r) {
          r.checked = true;
        });
      } else {
        const list = scope.dataset.selectList || "";
        rowBoxes(list).forEach(function (r) { r.checked = true; });
      }
      updateAllCounts();
      return;
    }

    if (e.key === " ") {
      const tag = e.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON" || tag === "A") return;
      const tr = e.target.closest("tr");
      const box = tr ? tr.querySelector("[data-select-row]") : null;
      if (box) {
        e.preventDefault();
        box.checked = !box.checked;
        box.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  });

  const lastIndex = {};

  async function queueItems(btn) {
    const list = btn.dataset.list || "";
    const keys = [];
    rowBoxes(list).filter(function (b) { return b.checked; }).forEach(function (box) {
      (box.dataset.keys || "").split(",").filter(Boolean).forEach(function (k) {
        if (keys.indexOf(k) < 0) keys.push(k);
      });
    });
    if (!keys.length) {
      announce("Select some items first.");
      return;
    }
    const label = btn.innerHTML;
    const bar = btn.closest(".action-bar");
    btn.disabled = true;
    if (bar) bar.classList.add("busy");
    // Sent in batches, with the count going up on the button, so a big selection
    // never looks stuck.
    const CHUNK = 50;
    let added = 0;
    let missing = 0;
    try {
      for (let i = 0; i < keys.length; i += CHUNK) {
        btn.textContent = "Adding " + Math.min(i + CHUNK, keys.length) + " of " + keys.length + "\u2026";
        const res = await post("/api/queue", {
          kind: "items",
          item_keys: keys.slice(i, i + CHUNK),
          source: btn.dataset.source || "inbox"
        });
        added += (res && res.added) || 0;
        missing += (res && res.missing) || 0;
      }
      await updateExecuteBadge();
      updateAllCounts();
      announce(
        "Added " + added + " item" + (added === 1 ? "" : "s") + " to Execute." +
        (missing ? " " + missing + " were no longer in the scan (rescan to see them)." : "") +
        " Nothing has changed on disk yet."
      );
    } catch (err) {
      announce("Stopped after " + added + " items: " + String(err.message || err));
      await updateExecuteBadge().catch(function () {});
    } finally {
      btn.innerHTML = label;
      btn.disabled = false;
      if (bar) bar.classList.remove("busy");
      updateAllCounts();
    }
  }

  function startScan(only) {
    post("/api/jobs/scan", { only: only || null })
      .then(function () { watchJob(function () { window.location.reload(); }); })
      .catch(function (err) { announce(String(err.message || err)); });
  }

  // ---- Live progress panel: steps, a bar, and the files and folders going by ----
  const JOB_TITLES = {
    scan: "Scanning your folders",
    execute: "Applying changes",
    apply: "Applying changes",
    undo: "Undoing a run",
    replan: "Updating"
  };
  const STEP_ORDER = ["scan", "classify", "identify", "plan"];
  let tickerSeq = 0;
  let tickerQueue = [];
  let tickerTimer = null;

  function jobPanel() {
    return document.getElementById("job-panel");
  }

  function pushTicker(text, stage) {
    const list = document.querySelector("[data-job-ticker]");
    if (!list) return;
    const li = document.createElement("li");
    li.className = "ticker-line" + (stage ? " ticker-" + stage : "");
    li.textContent = text;
    li.title = text;
    list.insertBefore(li, list.firstChild);
    while (list.children.length > 9) list.removeChild(list.lastChild);
  }

  function drainTicker() {
    if (tickerTimer) return;
    tickerTimer = window.setInterval(function () {
      const next = tickerQueue.shift();
      if (!next) {
        window.clearInterval(tickerTimer);
        tickerTimer = null;
        return;
      }
      pushTicker(next.text, next.stage);
    }, 70);
  }

  function updateProgress(job) {
    const progress = job.progress || {};
    const panel = jobPanel();
    if (!panel) {
      if (progress.detail) announce(progress.detail);
      return;
    }
    panel.hidden = false;

    const title = panel.querySelector("[data-job-title]");
    if (title) title.textContent = JOB_TITLES[job.kind] || "Working";

    const stage = progress.stage || "";
    const steps = panel.querySelector("[data-job-steps]");
    if (steps) {
      steps.hidden = STEP_ORDER.indexOf(stage) < 0;
      const at = STEP_ORDER.indexOf(stage);
      steps.querySelectorAll("li").forEach(function (li) {
        const i = STEP_ORDER.indexOf(li.dataset.step || "");
        li.classList.toggle("done", at >= 0 && i < at);
        li.classList.toggle("active", i === at);
      });
    }

    const current = parseInt(progress.current || 0, 10) || 0;
    const total = parseInt(progress.total || 0, 10) || 0;
    const bar = panel.querySelector("[data-job-bar]");
    if (bar) {
      bar.classList.toggle("indeterminate", !total);
      bar.style.width = total ? Math.min(100, Math.round((current / total) * 100)) + "%" : "";
    }
    const count = panel.querySelector("[data-job-count]");
    if (count) count.textContent = total ? current + " of " + total : (current ? String(current) : "");

    (progress.recent || []).forEach(function (r) {
      if (!r || typeof r.seq !== "number" || r.seq <= tickerSeq) return;
      tickerSeq = r.seq;
      tickerQueue.push({ text: String(r.text || ""), stage: String(r.stage || "") });
    });
    // Keep up when a lot arrives at once: show the latest ones.
    if (tickerQueue.length > 30) tickerQueue = tickerQueue.slice(-30);
    drainTicker();

    if (job.state === "done" || job.state === "error" || job.state === "cancelled") {
      if (title) {
        title.textContent = job.state === "done" ? "Done" : job.state === "error" ? "Stopped: " + (job.error || "error") : "Cancelled";
      }
      if (bar) {
        bar.classList.remove("indeterminate");
        bar.style.width = "100%";
      }
    }
  }

  function watchJob(onDone) {
    const source = new EventSource("/api/jobs/events");
    let finished = false;

    function finish(job) {
      if (finished) return;
      finished = true;
      source.close();
      onDone(job || {});
    }

    source.onmessage = function (event) {
      try {
        const job = JSON.parse(event.data);
        updateProgress(job);
        if (["done", "error", "cancelled"].indexOf(job.state) >= 0) finish(job);
      } catch (err) {
        // ignore malformed event
      }
    };

    source.addEventListener("ping", function () {
      // no-op
    });

    source.onerror = function () {
      get("/api/jobs/current").then(finish).catch(function () { finish({}); });
    };
  }

  function buildFixOptions(action, tmdbEl, kindEl) {
    const options = {};
    if (action === "show") options.kind = "show";
    if (action === "movie") options.kind = "movie";
    if (action === "split") options.split = "true";
    if (action === "ignore") options.ignore = "true";
    if (action === "match") {
      if (tmdbEl) options.tmdb = tmdbEl.value;
      if (kindEl) options.kind = kindEl.value;
    }
    return options;
  }

  async function addFix() {
    const pattern = document.getElementById("new-fix-pattern");
    const action = document.getElementById("new-fix-action");
    const tmdb = document.getElementById("new-fix-tmdb");
    const kind = document.getElementById("new-fix-kind");
    const patternValue = pattern ? pattern.value.trim() : "";
    if (!patternValue) {
      announce("Enter a name first.");
      return;
    }
    const options = buildFixOptions(action ? action.value : "match", tmdb, kind);
    await post("/api/fixes", { pattern: patternValue, name: patternValue, options: options, scan: true });
    window.location.reload();
  }

  async function editFix(btn) {
    const details = btn.closest(".edit-form");
    const id = parseInt(btn.dataset.fixId || "0", 10);
    const pattern = details.querySelector("[data-edit-pattern]");
    const name = details.querySelector("[data-edit-name]");
    const action = details.querySelector("[data-edit-action]");
    const tmdb = details.querySelector("[data-edit-tmdb]");
    const kind = details.querySelector("[data-edit-kind]");
    const options = buildFixOptions(action ? action.value : "match", tmdb, kind);
    await api("PUT", "/api/fixes/" + id, {
      pattern: pattern ? pattern.value.trim() : "",
      name: name ? name.value.trim() : "",
      options: options,
      scan: true
    });
    window.location.reload();
  }

  async function forgetFixes(ids) {
    if (!ids.length) {
      announce("Nothing selected.");
      return;
    }
    for (const id of ids) {
      await api("DELETE", "/api/fixes/" + id);
    }
    window.location.reload();
  }

  function openConfirm(title, bodyHtml, okLabel) {
    return new Promise(function (resolve) {
      const dialog = document.getElementById("confirm-dialog");
      if (!dialog) {
        resolve(window.confirm(bodyHtml.replace(/<[^>]+>/g, "")));
        return;
      }
      const titleEl = dialog.querySelector("#confirm-title");
      const bodyEl = dialog.querySelector("#confirm-body");
      const okBtn = dialog.querySelector("#confirm-ok");
      const cancelBtn = dialog.querySelector("#confirm-cancel");
      if (titleEl) titleEl.textContent = title;
      if (bodyEl) bodyEl.innerHTML = bodyHtml;
      if (okBtn) okBtn.textContent = okLabel || "Confirm";

      let done = false;
      function finish(value) {
        if (done) return;
        done = true;
        dialog.close();
        resolve(value);
      }

      function onOk() { finish(true); }
      function onCancel() { finish(false); }
      function onClose() { finish(false); }

      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      dialog.addEventListener("close", onClose);
      dialog.showModal();
    });
  }

  function executeCheckedIds() {
    const box = document.getElementById("execute-groups");
    if (!box) return [];
    return Array.prototype.slice.call(box.querySelectorAll("[data-select-row]"))
      .filter(function (r) { return r.checked && counted(r); })
      .map(function (r) { return parseInt(r.dataset.queueId || "0", 10); })
      .filter(function (x) { return x > 0; });
  }

  function executeCounts() {
    const box = document.getElementById("execute-groups");
    const rows = box ? Array.prototype.slice.call(box.querySelectorAll("[data-select-row]")).filter(counted) : [];
    const checked = rows.filter(function (r) { return r.checked; });
    let files = 0;
    checked.forEach(function (r) { files += parseInt(r.dataset.files || "0", 10); });
    return { ids: checked.length, files: files };
  }

  async function openApplyDialog() {
    const ids = executeCheckedIds();
    if (!ids.length) {
      announce("Nothing selected.");
      return;
    }
    const counts = executeCounts();
    const ok = await openConfirm(
      "Apply changes",
      "Apply " + counts.ids + " changes (" + counts.files + " files and folders). Nothing on disk changes until you confirm.",
      "Apply now"
    );
    if (!ok) return;
    await post("/api/jobs/execute", { ids: ids });
    watchJob(function (finished) {
      const result = finished.result || {};
      if (result.run_id) {
        renderExecuteResult(result);
        updateExecuteBadge();
      } else {
        announce("Done.");
      }
    });
  }

  function renderExecuteResult(result) {
    const el = document.getElementById("execute-result");
    if (!el) return;
    const run = result.run || {};
    el.hidden = false;
    el.innerHTML =
      "<h2>Run finished</h2>" +
      "<p>Status: " + escapeHtml(run.status || "ok") + " · Done: " + escapeHtml(run.done || 0) +
      " · Failed: " + escapeHtml(run.failed || 0) + " · Skipped: " + escapeHtml(run.skipped || 0) + "</p>" +
      '<button type="button" class="btn danger" data-undo-run="' + escapeHtml(result.run_id || "") + '">Undo this run…</button>';
  }

  async function clearQueue() {
    const box = document.getElementById("execute-groups");
    if (!box) return;
    const ids = Array.prototype.slice.call(box.querySelectorAll("[data-select-row]"))
      .map(function (r) { return parseInt(r.dataset.queueId || "0", 10); })
      .filter(function (x) { return x > 0; });
    if (!ids.length) return;
    const ok = await openConfirm("Clear the list", "This removes everything waiting on Execute. It does not change files.", "Clear");
    if (!ok) return;
    await del("/api/queue", { ids: ids });
    window.location.reload();
  }

  async function forgetRuns(btn) {
    const one = btn.dataset.forgetRuns !== "all";
    const ok = await openConfirm(
      one ? "Remove this run from history" : "Clear all history",
      (one ? "This run" : "Every run") +
        " will be removed from History, so it can't be undone from here any more. Your files stay exactly as they are.",
      one ? "Remove" : "Clear history"
    );
    if (!ok) return;
    await api("DELETE", "/api/runs", one ? { ids: [btn.dataset.forgetRuns] } : {});
    window.location.reload();
  }

  async function undoRunId(runId) {
    if (!runId) return;
    const preview = await post("/api/runs/" + encodeURIComponent(runId) + "/undo-preview", {});
    const ok = await openConfirm(
      "Undo this run",
      "Undo " + escapeHtml(preview.done || 0) + " changes? Failed: " + escapeHtml(preview.failed || 0) + ". Nothing changes until you confirm.",
      "Undo now"
    );
    if (!ok) return;
    await post("/api/jobs/undo", { run_id: runId, apply: true });
    watchJob(function () { window.location.reload(); });
  }

  function matchState() {
    const page = document.querySelector("[data-match-key]");
    const picked = document.querySelector('input[name="pick"]:checked');
    const state = {
      kind: page ? page.dataset.currentKind || "movie" : "movie",
      title: "",
      year: "",
      tmdb_id: "",
      use_short_title: false
    };
    const manualTitle = document.getElementById("manual-title");
    const manualYear = document.getElementById("manual-year");
    const manualKind = document.getElementById("manual-kind");
    if (manualTitle) state.title = manualTitle.value;
    if (manualYear) state.year = manualYear.value;
    if (manualKind) state.kind = manualKind.value;
    if (picked) {
      state.tmdb_id = picked.dataset.tmdbId || "";
      state.kind = picked.dataset.kind || state.kind;
      state.title = picked.dataset.title || state.title;
      state.year = picked.dataset.year || state.year;
    }
    const short = document.getElementById("use-short-title");
    if (short) state.use_short_title = short.checked;
    return state;
  }

  async function updateMatchPreview() {
    const page = document.querySelector("[data-match-key]");
    if (!page) return;
    const key = page.dataset.matchKey;
    const state = matchState();
    const res = await post("/api/match/" + encodeURIComponent(key) + "/preview", state);
    const target = document.getElementById("match-target");
    if (target) target.textContent = res.target || "—";
  }

  document.addEventListener("change", function (e) {
    if (e.target.matches('input[name="pick"]') || e.target.matches("#use-short-title") || e.target.matches("#manual-title") || e.target.matches("#manual-year") || e.target.matches("#manual-kind")) {
      updateMatchPreview().catch(function () {});
    }
  });

  async function searchMatch() {
    const page = document.querySelector("[data-match-key]");
    if (!page) return;
    const key = page.dataset.matchKey;
    const query = document.getElementById("search-query");
    const year = document.getElementById("search-year");
    const state = matchState();
    const data = await post("/api/match/" + encodeURIComponent(key) + "/search", {
      query: query ? query.value : "",
      year: year ? year.value : "",
      kind: state.kind
    });
    const list = document.getElementById("candidate-list");
    if (!list) return;
    const candidates = data.candidates || [];
    if (!candidates.length) {
      list.innerHTML = '<p class="empty">No suggestions found.</p>';
      return;
    }
    list.innerHTML = candidates.map(function (c) {
      const rawTitle = c.tmdb_title || c.title || "";
      const safeTitle = c.title || c.tmdb_title || "";
      const poster = c.poster_url
        ? '<img loading="lazy" class="poster" src="' + escapeHtml(c.poster_url) + '" alt="Poster for ' + escapeHtml(safeTitle) + '">'
        : '<div role="img" aria-label="Poster placeholder" class="poster-box">poster</div>';
      const reasons = (c.reasons || []).map(function (r) {
        return '<span class="reason">' + escapeHtml(r) + "</span>";
      }).join("");
      return '<label class="candidate-card">' +
        '<input type="radio" name="pick" value="' + escapeHtml(c.tmdb_id) + '" data-tmdb-id="' + escapeHtml(c.tmdb_id) + '" data-kind="' + escapeHtml(c.kind) + '" data-title="' + escapeHtml(safeTitle) + '" data-year="' + escapeHtml(c.year || "") + '">' +
        poster +
        '<div class="candidate-copy"><div class="candidate-title" data-tmdb-title="true">' + escapeHtml(rawTitle) + "</div>" +
        '<div class="candidate-meta">' + escapeHtml(c.year || "—") + " · " + escapeHtml(c.runtime_min || "—") + " min · " + escapeHtml(c.kind === "show" ? "TV show" : "movie") + "</div>" +
        '<div class="chips reason-chips">' + reasons + "</div></div></label>";
    }).join("");
    updateMatchPreview().catch(function () {});
  }

  async function chooseMatch() {
    const page = document.querySelector("[data-match-key]");
    if (!page) return;
    const key = page.dataset.matchKey;
    const state = matchState();
    const remember = document.getElementById("remember-choice");
    const next = returnUrl("/execute");

    let message = "Added to Execute. Nothing has changed on disk yet.";
    if (state.tmdb_id) {
      const res = await post("/api/match/" + encodeURIComponent(key) + "/choose", {
        tmdb_id: parseInt(state.tmdb_id, 10),
        kind: state.kind,
        remember: remember ? remember.checked : false,
        use_short_title: state.use_short_title,
        source: "match"
      });
      if (res && res.message) message = res.message;
    } else {
      await post("/api/item/" + encodeURIComponent(key) + "/edit", {
        title: state.title || "Untitled",
        year: state.year || null,
        kind: state.kind,
        remember: remember ? remember.checked : false
      });
    }

    await updateExecuteBadge();
    announce(message);
    try { window.sessionStorage.setItem("mk-flash", message); } catch (err) { /* ignore */ }
    window.location.href = next;
  }

  async function skipMatch() {
    const page = document.querySelector("[data-match-key]");
    if (!page) return;
    const key = page.dataset.matchKey;
    await post("/api/item/" + encodeURIComponent(key) + "/skip", {});
    window.location.href = returnUrl("/inbox");
  }

  document.querySelectorAll(".poster").forEach(function (img) {
    img.addEventListener("error", function () {
      const box = document.createElement("div");
      box.className = "poster-box";
      box.setAttribute("role", "img");
      box.setAttribute("aria-label", "Poster placeholder");
      box.textContent = "poster";
      img.replaceWith(box);
    });
  });

  // Inbox filter chips. The choice is kept in the address (?show=needs) so coming back
  // from "Check the match" lands on the same filtered list.
  function applyInboxFilter(value) {
    const chip = document.querySelector('[data-filter="' + value + '"]');
    if (!chip) return false;
    document.querySelectorAll("[data-filter]").forEach(function (c) { c.classList.remove("active"); });
    chip.classList.add("active");
    let shown = 0;
    document.querySelectorAll("tr[data-status]").forEach(function (tr) {
      const show = value === "all" || tr.dataset.status === value;
      tr.style.display = show ? "" : "none";
      if (show) shown += 1;
    });
    const empty = document.querySelector("[data-filter-empty]");
    if (empty) {
      empty.hidden = shown > 0;
      const what = empty.querySelector("[data-filter-empty-what]");
      const words = { needs: "needs you any more", ready: "is ready", left: "was left alone", all: "to show" };
      if (what) what.textContent = words[value] || "matches this filter";
    }
    return true;
  }

  document.querySelectorAll("[data-filter]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      const value = chip.dataset.filter || "all";
      applyInboxFilter(value);
      try {
        const url = new URL(window.location.href);
        if (value === "all") url.searchParams.delete("show");
        else url.searchParams.set("show", value);
        window.history.replaceState(null, "", url.toString());
      } catch (err) {
        // keep the filter on screen even if the address can't change
      }
    });
  });

  (function showFlash() {
    try {
      const msg = window.sessionStorage.getItem("mk-flash");
      if (!msg) return;
      window.sessionStorage.removeItem("mk-flash");
      announce(msg);
    } catch (err) {
      // ignore
    }
  })();

  (function restoreInboxFilter() {
    try {
      const show = new URLSearchParams(window.location.search).get("show");
      if (show) applyInboxFilter(show);
    } catch (err) {
      // ignore
    }
  })();

  // The match page returns to the exact Inbox view you came from.
  function returnUrl(fallback) {
    const ref = document.referrer || "";
    if (ref.startsWith(window.location.origin + "/") && ref.indexOf("/match/") < 0) return ref;
    try {
      const saved = window.sessionStorage.getItem("mk-return");
      if (saved && saved.startsWith(window.location.origin + "/")) return saved;
    } catch (err) {
      // storage may be unavailable
    }
    return fallback;
  }

  document.addEventListener("click", function (e) {
    const link = e.target.closest('a[href^="/match/"]');
    if (!link) return;
    try { window.sessionStorage.setItem("mk-return", window.location.href); } catch (err) { /* ignore */ }
  }, true);

  (function wireMatchBackLink() {
    if (!document.querySelector("[data-match-key]")) return;
    const back = document.querySelector(".back-link");
    if (back) back.href = returnUrl(back.getAttribute("href") || "/inbox");
  })();

  updateAllCounts();
  updateExecuteBadge().catch(function () {});
})();
