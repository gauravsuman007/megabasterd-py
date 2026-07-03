const transferList = document.getElementById("transfer-list");
const transferItems = new Map();
// id -> { kind, terminal, bytesDone, total, speedText, etaText }, tracked to
// aggregate the global speed / downloaded-of-total / ETA display next to the
// TRANSFERS header. speedText/etaText are the *frozen* per-file readouts,
// only recomputed on the smoothing tick (see below) so they don't flicker.
const transferStats = new Map();
const uploadAccountSelect = document.getElementById("upload-account");
const globalSpeedEl = document.getElementById("global-speed");

// Speed/ETA smoothing. Raw progress arrives many times a second, which made
// the speed and time-left readouts jitter wildly. Instead of showing an
// instantaneous rate, we recompute every SPEED_WINDOW_MS as a true average
// over the elapsed window -- (bytes now - bytes at last tick) / elapsed --
// and hold both the speed and ETA text steady in between. Byte counts and
// progress bars still update live on every message; only the speed/ETA text
// is throttled to this cadence.
const SPEED_WINDOW_MS = 3000;
// id -> { baseBytes, baseTs }: the window baseline each tick measures from.
const speedTrack = new Map();
// Frozen global readouts, recomputed on the same tick as the per-file ones.
let globalDownSpeedText = "";
let globalUpSpeedText = "";
let globalEtaText = "";

function formatBytes(n) {
    if (!n && n !== 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
        n /= 1024;
        i++;
    }
    return `${n.toFixed(1)} ${units[i]}`;
}

function formatSpeed(bytesPerSec) {
    if (!bytesPerSec || bytesPerSec < 1) return "";
    return `${formatBytes(bytesPerSec)}/s`;
}

function formatDuration(seconds) {
    if (!isFinite(seconds) || seconds <= 0) return "";
    seconds = Math.round(seconds);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
}

const TERMINAL_STATUSES = ["done", "cancelled", "mac_mismatch", "error"];
const RESTARTABLE_DOWNLOAD_STATUSES = ["cancelled", "mac_mismatch", "error"];

// The downloaded-of-total figure is live (every message); the global speed
// and ETA come from the frozen texts recomputed on the smoothing tick.
function updateGlobalStats() {
    let downDone = 0;
    let downTotal = 0;
    for (const { kind, bytesDone, total } of transferStats.values()) {
        if (kind === "upload") continue;
        // The whole queue counts toward downloaded-of-total, so a finished
        // item still contributes its full size.
        downDone += bytesDone || 0;
        downTotal += total || 0;
    }
    const parts = [];
    if (globalDownSpeedText) parts.push(globalDownSpeedText);
    if (downTotal > 0) parts.push(`${formatBytes(downDone)} / ${formatBytes(downTotal)}`);
    if (globalEtaText) parts.push(globalEtaText);
    if (globalUpSpeedText) parts.push(globalUpSpeedText);
    globalSpeedEl.textContent = parts.join(" · ");
}

// Per-file stats line: "downloaded / total · speed · ETA left". The
// downloaded/total is live; speed/ETA are the frozen texts from the tick.
function renderFileStats(el, stat) {
    const parts = [];
    if (stat.total) parts.push(`${formatBytes(stat.bytesDone)} / ${formatBytes(stat.total)}`);
    else if (stat.bytesDone) parts.push(formatBytes(stat.bytesDone));
    if (stat.speedText) parts.push(stat.speedText);
    if (stat.etaText) parts.push(`${stat.etaText} left`);
    el.querySelector(".file-stats").textContent = parts.join(" · ");
}

// Recompute every speed/ETA readout as an average over the last window, then
// freeze the resulting text until the next tick.
function tickSmoothing() {
    const now = performance.now();
    let downSpeed = 0;
    let upSpeed = 0;
    let downRemaining = 0;
    for (const [id, stat] of transferStats) {
        const track = speedTrack.get(id);
        let speed = 0;
        if (track && !stat.terminal) {
            const dt = (now - track.baseTs) / 1000;
            if (dt > 0) speed = Math.max(0, (stat.bytesDone - track.baseBytes) / dt);
        }
        // Reset the window baseline for the next interval.
        speedTrack.set(id, { baseBytes: stat.bytesDone, baseTs: now });

        stat.speedText = !stat.terminal && speed >= 1 ? formatSpeed(speed) : "";
        stat.etaText =
            !stat.terminal && speed >= 1 && stat.total > stat.bytesDone
                ? formatDuration((stat.total - stat.bytesDone) / speed)
                : "";

        if (stat.kind === "upload") {
            if (!stat.terminal) upSpeed += speed;
        } else if (!stat.terminal) {
            downSpeed += speed;
            downRemaining += Math.max(0, (stat.total || 0) - (stat.bytesDone || 0));
        }

        const el = transferItems.get(id);
        if (el) renderFileStats(el, stat);
    }

    globalDownSpeedText = downSpeed >= 1 ? `↓ ${formatBytes(downSpeed)}/s` : "";
    globalUpSpeedText = upSpeed >= 1 ? `↑ ${formatBytes(upSpeed)}/s` : "";
    globalEtaText = downSpeed >= 1 && downRemaining > 0 ? `${formatDuration(downRemaining / downSpeed)} left` : "";
    updateGlobalStats();
}
setInterval(tickSmoothing, SPEED_WINDOW_MS);

function renderTransfer(data) {
    let el = transferItems.get(data.id);
    if (!el) {
        el = document.createElement("li");
        el.className = "download-item";
        el.innerHTML = `
            <div class="name">
                <span class="filename"></span>
                <span class="status"></span>
                <button type="button" class="pause-btn"></button>
                <button type="button" class="cancel-btn">Cancel</button>
            </div>
            <div class="file-stats"></div>
            <div class="progress-track"><div class="progress-fill"></div></div>
        `;
        el.querySelector(".cancel-btn").addEventListener("click", () => cancelTransfer(data.id));
        el.querySelector(".pause-btn").addEventListener("click", (e) => {
            const action = e.target.dataset.action;
            if (action) fetch(`/api/transfers/${data.id}/${action}`, { method: "POST" });
        });
        // Append (not prepend) so rows appear in the order they were queued,
        // which is the order they'll actually download (FIFO through the slots).
        transferList.appendChild(el);
        transferItems.set(data.id, el);
        // Seed the smoothing baseline so the first tick has a window to
        // measure against instead of throwing away the first few seconds.
        speedTrack.set(data.id, { baseBytes: data.bytes_done || 0, baseTs: performance.now() });
    }

    const kindLabel = data.kind === "upload" ? "⬆" : "⬇";
    el.querySelector(".filename").textContent = `${kindLabel} ${data.name || data.link || ""}`;
    const statusKey = data.status.split(":")[0].trim();
    const statusEl = el.querySelector(".status");
    statusEl.textContent = data.status;
    statusEl.className = "status " + statusKey;

    const pct = data.total ? Math.min(100, (data.bytes_done / data.total) * 100) : 0;
    el.querySelector(".progress-fill").style.width = pct.toFixed(1) + "%";

    const terminal = TERMINAL_STATUSES.includes(statusKey);
    el.querySelector(".cancel-btn").style.display = terminal ? "none" : "inline-block";

    const pauseBtn = el.querySelector(".pause-btn");
    if (statusKey === "downloading" || statusKey === "uploading") {
        pauseBtn.textContent = "Pause";
        pauseBtn.dataset.action = "pause";
        pauseBtn.style.display = "inline-block";
    } else if (statusKey === "paused") {
        pauseBtn.textContent = "Resume";
        pauseBtn.dataset.action = "resume";
        pauseBtn.style.display = "inline-block";
    } else if (data.kind === "download" && RESTARTABLE_DOWNLOAD_STATUSES.includes(statusKey)) {
        pauseBtn.textContent = "Retry";
        pauseBtn.dataset.action = "resume";
        pauseBtn.style.display = "inline-block";
    } else {
        pauseBtn.style.display = "none";
    }

    const prev = transferStats.get(data.id);
    const stat = {
        kind: data.kind,
        terminal,
        bytesDone: data.bytes_done || 0,
        total: data.total || 0,
        // Carry the frozen speed/ETA text across live updates; the tick owns
        // them. A terminal transfer shows neither.
        speedText: terminal ? "" : prev?.speedText || "",
        etaText: terminal ? "" : prev?.etaText || "",
    };
    transferStats.set(data.id, stat);
    renderFileStats(el, stat);
    updateGlobalStats();
}

async function cancelTransfer(id) {
    await fetch(`/api/transfers/${id}`, { method: "DELETE" });
}

function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onmessage = (event) => renderTransfer(JSON.parse(event.data));
    ws.onclose = () => setTimeout(connectWebSocket, 1000);
}
connectWebSocket();

async function loadExistingTransfers() {
    const resp = await fetch("/api/transfers");
    const transfers = await resp.json();
    for (const t of transfers) renderTransfer(t);
}
loadExistingTransfers();

document.getElementById("pause-all-btn").addEventListener("click", async () => {
    await fetch("/api/transfers/pause-all", { method: "POST" });
});

document.getElementById("resume-all-btn").addEventListener("click", async () => {
    await fetch("/api/transfers/resume-all", { method: "POST" });
});

document.getElementById("stop-all-btn").addEventListener("click", async () => {
    if (!confirm("Stop all active and queued transfers?")) return;
    await fetch("/api/transfers/stop-all", { method: "POST" });
});

document.getElementById("clear-finished-btn").addEventListener("click", async () => {
    const resp = await fetch("/api/transfers/clear-finished", { method: "POST" });
    const data = await resp.json();
    for (const id of data.removed) {
        const el = transferItems.get(id);
        if (el) el.remove();
        transferItems.delete(id);
        transferStats.delete(id);
        speedTrack.delete(id);
    }
    updateGlobalStats();
});

async function queueDownload(link, fallbackName, fallbackSize) {
    const resp = await fetch("/api/downloads", { method: "POST", body: new URLSearchParams({ link }) });
    if (!resp.ok) return { ok: false, link, error: await resp.text() };
    const data = await resp.json();
    renderTransfer({ id: data.id, kind: "download", link, name: fallbackName || null, status: "starting", bytes_done: 0, total: fallbackSize || 0 });
    return { ok: true };
}

document.getElementById("download-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const linksText = form.links.value;

    const classifyResp = await fetch("/api/links/classify", { method: "POST", body: new URLSearchParams({ links: linksText }) });
    if (!classifyResp.ok) { alert(await classifyResp.text()); return; }
    const { results } = await classifyResp.json();

    const fileLinks = results.filter((r) => r.kind === "file").map((r) => r.link);
    const folderLinks = results.filter((r) => r.kind === "folder").map((r) => r.link);
    const failures = results.filter((r) => r.kind === "invalid").map((r) => `${r.link} (not a recognizable MEGA link)`);

    for (const link of fileLinks) {
        const result = await queueDownload(link);
        if (!result.ok) failures.push(`${result.link} (${result.error})`);
    }

    form.reset();

    if (failures.length > 0) {
        alert(`Couldn't queue ${failures.length} link(s):\n${failures.join("\n")}`);
    }

    if (folderLinks.length > 0) {
        await openFolderPicker(folderLinks);
    }
});

// -------------------------------------------------------- folder picker ----
const folderPickerOverlay = document.getElementById("folder-picker-overlay");
const folderPickerBody = document.getElementById("folder-picker-body");
const folderPickerSummary = document.getElementById("folder-picker-summary");

// A folder node's own `size` from the API is always 0 (MEGA doesn't report
// folder sizes) -- the picker needs the sum of everything nested under it.
function nodeTotalSize(node) {
    if (!node.is_folder) return node.size;
    return node.children.reduce((sum, child) => sum + nodeTotalSize(child), 0);
}

function updateFolderPickerSelection() {
    const checked = Array.from(folderPickerBody.querySelectorAll('input[type="checkbox"][data-link]:checked'));
    if (checked.length === 0) {
        folderPickerSummary.textContent = "Nothing selected";
        return;
    }
    const totalBytes = checked.reduce((sum, cb) => sum + (Number(cb.dataset.size) || 0), 0);
    folderPickerSummary.textContent = `${checked.length} file${checked.length === 1 ? "" : "s"} selected -- ${formatBytes(totalBytes)} total`;
}

function renderPickerNode(node) {
    const li = document.createElement("li");
    const row = document.createElement("div");
    row.className = "name";
    const label = document.createElement("label");
    label.className = "checkbox-row";

    if (node.is_folder) {
        const selectAll = document.createElement("input");
        selectAll.type = "checkbox";
        const nameSpan = document.createElement("span");
        nameSpan.className = "filename";
        nameSpan.textContent = `\u{1F4C1} ${node.name}`;

        // Children start collapsed so opening the picker shows only the
        // direct entries at each level -- an expand toggle reveals them.
        const sublist = document.createElement("ul");
        sublist.className = "folder-children collapsed";
        for (const child of node.children) sublist.appendChild(renderPickerNode(child));

        if (node.children.length === 0) {
            selectAll.disabled = true; // nothing under it to select
            li.classList.add("is-empty");
            const emptyBadge = document.createElement("span");
            emptyBadge.className = "empty-badge";
            emptyBadge.textContent = "empty";
            nameSpan.appendChild(emptyBadge);
            row.appendChild(spacerToggle());
            label.append(selectAll, nameSpan);
            row.appendChild(label);
        } else {
            selectAll.checked = true; // everything is selected by default
            const sizeSpan = document.createElement("span");
            sizeSpan.className = "node-size";
            sizeSpan.textContent = ` (${formatBytes(nodeTotalSize(node))})`;
            nameSpan.appendChild(sizeSpan);

            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = "tree-toggle";
            toggle.setAttribute("aria-expanded", "false");
            toggle.textContent = "▸"; // ▸
            toggle.addEventListener("click", () => {
                const collapsed = sublist.classList.toggle("collapsed");
                toggle.textContent = collapsed ? "▸" : "▾"; // ▸ / ▾
                toggle.setAttribute("aria-expanded", String(!collapsed));
            });

            label.append(selectAll, nameSpan);
            row.append(toggle, label);
        }

        li.appendChild(row);
        li.appendChild(sublist);

        selectAll.addEventListener("change", () => {
            sublist.querySelectorAll('input[type="checkbox"][data-link]').forEach((cb) => { cb.checked = selectAll.checked; });
            updateFolderPickerSelection();
        });
    } else {
        li.className = "download-item";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = true; // everything is selected by default
        cb.dataset.link = node.download_link;
        cb.dataset.name = node.name;
        cb.dataset.size = node.size;
        const nameSpan = document.createElement("span");
        nameSpan.className = "filename";
        nameSpan.textContent = node.name;
        const sizeSpan = document.createElement("span");
        sizeSpan.className = "node-size";
        sizeSpan.textContent = ` (${formatBytes(node.size)})`;
        nameSpan.appendChild(sizeSpan);
        // Spacer keeps file checkboxes aligned with folders that have a toggle.
        row.appendChild(spacerToggle());
        label.append(cb, nameSpan);
        row.appendChild(label);
        li.appendChild(row);

        cb.addEventListener("change", updateFolderPickerSelection);
    }
    return li;
}

// A non-interactive placeholder the same width as .tree-toggle, so rows
// without an expand control (files, empty folders) still line up.
function spacerToggle() {
    const span = document.createElement("span");
    span.className = "tree-toggle-spacer";
    return span;
}

async function openFolderPicker(folderLinks) {
    folderPickerBody.innerHTML = "";
    folderPickerOverlay.style.display = "flex";
    updateFolderPickerSelection();

    for (const link of folderLinks) {
        const section = document.createElement("div");
        section.className = "folder-picker-section";
        const heading = document.createElement("h3");
        heading.textContent = "Loading…";
        const list = document.createElement("ul");
        section.append(heading, list);
        folderPickerBody.appendChild(section);

        let resp;
        try {
            resp = await fetch(`/api/folder?link=${encodeURIComponent(link)}`);
        } catch (err) {
            heading.textContent = link;
            list.outerHTML = `<p class="hint">${String(err)}</p>`;
            continue;
        }

        if (!resp.ok) {
            heading.textContent = link;
            const errText = await resp.text();
            const errEl = document.createElement("p");
            errEl.className = "hint";
            errEl.textContent = errText;
            section.appendChild(errEl);
            continue;
        }

        const data = await resp.json();
        heading.textContent = `\u{1F4C1} ${link}`;
        if (data.tree.length === 0) {
            const empty = document.createElement("p");
            empty.className = "hint";
            empty.textContent = "(empty folder -- no files or subfolders in it right now)";
            section.appendChild(empty);
            continue;
        }
        for (const node of data.tree) list.appendChild(renderPickerNode(node));
    }

    updateFolderPickerSelection(); // reflect the default-all-selected state once everything's loaded
}

function closeFolderPicker() {
    folderPickerOverlay.style.display = "none";
    folderPickerBody.innerHTML = "";
}

document.getElementById("folder-picker-close").addEventListener("click", closeFolderPicker);
document.getElementById("folder-picker-cancel-btn").addEventListener("click", closeFolderPicker);
folderPickerOverlay.addEventListener("click", (e) => {
    if (e.target === folderPickerOverlay) closeFolderPicker(); // click landed on the backdrop, not the dialog
});

document.getElementById("folder-picker-select-all").addEventListener("click", () => {
    folderPickerBody.querySelectorAll('input[type="checkbox"]').forEach((cb) => { if (!cb.disabled) cb.checked = true; });
    updateFolderPickerSelection();
});

document.getElementById("folder-picker-select-none").addEventListener("click", () => {
    folderPickerBody.querySelectorAll('input[type="checkbox"]').forEach((cb) => { cb.checked = false; });
    updateFolderPickerSelection();
});

document.getElementById("folder-picker-download-btn").addEventListener("click", async () => {
    const checked = Array.from(folderPickerBody.querySelectorAll('input[type="checkbox"][data-link]:checked'));
    if (checked.length === 0) {
        alert("No files selected.");
        return;
    }
    const failures = [];
    for (const cb of checked) {
        const result = await queueDownload(cb.dataset.link, cb.dataset.name, Number(cb.dataset.size) || 0);
        if (!result.ok) failures.push(`${cb.dataset.name} (${result.error})`);
    }
    closeFolderPicker();
    if (failures.length > 0) {
        alert(`Couldn't queue ${failures.length} file(s):\n${failures.join("\n")}`);
    }
});

document.getElementById("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const body = new URLSearchParams({ email: form.email.value, file_path: form.file_path.value });
    const resp = await fetch("/api/uploads", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    const data = await resp.json();
    renderTransfer({ id: data.id, kind: "upload", name: form.file_path.value.split("/").pop(), status: "starting", bytes_done: 0, total: 0 });
    form.reset();
});

// Read-only: just fills the "which account" dropdown for uploads. Adding,
// removing, and unlocking accounts lives on the Settings page now.
async function populateUploadAccountSelect() {
    const resp = await fetch("/api/accounts");
    const data = await resp.json();
    uploadAccountSelect.innerHTML = '<option value="">Select account...</option>';
    if (data.locked) return;
    for (const acc of data.accounts) {
        const opt = document.createElement("option");
        opt.value = acc.email;
        opt.textContent = acc.email;
        uploadAccountSelect.appendChild(opt);
    }
}
populateUploadAccountSelect();

