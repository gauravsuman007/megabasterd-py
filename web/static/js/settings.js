
// ---------------------------------------------------------------- tabs ----
document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    });
});

// ------------------------------------------------------------- accounts ----
const accountList = document.getElementById("account-list");
const loginForm = document.getElementById("login-form");
const pincodeInput = loginForm.querySelector(".pincode-input");

const masterStatus = document.getElementById("master-password-status");
const setForm = document.getElementById("master-password-set-form");
const unlockForm = document.getElementById("master-password-unlock-form");
const manageDiv = document.getElementById("master-password-manage");
const rotateForm = document.getElementById("master-password-rotate-form");

async function refreshAccounts() {
    const resp = await fetch("/api/accounts");
    const data = await resp.json();
    accountList.innerHTML = "";

    setForm.style.display = "none";
    unlockForm.style.display = "none";
    manageDiv.style.display = "none";

    if (data.locked) {
        accountList.innerHTML = "<li>Account store is locked. Unlock it below to manage accounts.</li>";
        masterStatus.textContent = "(locked)";
        unlockForm.style.display = "flex";
        return;
    }

    masterStatus.textContent = data.encrypted ? "(enabled)" : "(disabled)";
    if (data.encrypted) {
        manageDiv.style.display = "flex";
    } else {
        setForm.style.display = "flex";
    }

    if (data.accounts.length === 0) {
        accountList.innerHTML = "<li>No accounts saved yet.</li>";
    }

    for (const acc of data.accounts) {
        const li = document.createElement("li");
        li.className = "download-item";
        li.innerHTML = `<div class="name"><span class="filename">${acc.email}</span><span class="status ${acc.active ? 'done' : ''}">${acc.active ? "active" : ""}</span><button type="button" class="cancel-btn">Remove</button></div>`;
        li.querySelector(".cancel-btn").addEventListener("click", async () => {
            if (!confirm(`Remove account ${acc.email}?`)) return;
            await fetch(`/api/accounts/${encodeURIComponent(acc.email)}`, { method: "DELETE" });
            refreshAccounts();
        });
        accountList.appendChild(li);
    }
}
refreshAccounts();

loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const body = new URLSearchParams({ email: form.email.value, password: form.password.value });
    if (form.pincode.value) body.set("pincode", form.pincode.value);

    const resp = await fetch("/api/accounts/login", { method: "POST", body });
    if (resp.status === 400) {
        const detail = (await resp.json()).detail || "";
        if (detail.includes("Two-factor")) {
            pincodeInput.style.display = "inline-block";
            alert("Enter your 2FA code and submit again.");
            return;
        }
        alert(detail);
        return;
    }
    if (!resp.ok) { alert(await resp.text()); return; }

    form.reset();
    pincodeInput.style.display = "none";
    refreshAccounts();
});

setForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new URLSearchParams({ password: e.target.new_password.value });
    const resp = await fetch("/api/accounts/master-password", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    setForm.reset();
    refreshAccounts();
});

rotateForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new URLSearchParams({ password: e.target.new_password.value });
    const resp = await fetch("/api/accounts/master-password", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    rotateForm.reset();
    refreshAccounts();
});

document.getElementById("master-password-remove").addEventListener("click", async () => {
    if (!confirm("Remove the master password? Stored accounts will go back to being unencrypted on disk.")) return;
    const resp = await fetch("/api/accounts/master-password", { method: "POST", body: new URLSearchParams() });
    if (!resp.ok) { alert(await resp.text()); return; }
    refreshAccounts();
});

unlockForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new URLSearchParams({ password: e.target.password.value });
    const resp = await fetch("/api/accounts/master-password/unlock", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    unlockForm.reset();
    refreshAccounts();
});

// ------------------------------------------------------------ smartproxy ----
const smartproxyList = document.getElementById("smartproxy-list");
const smartproxyEnabled = document.getElementById("smartproxy-enabled");
const smartproxyRandom = document.getElementById("smartproxy-random");
const smartproxyForce = document.getElementById("smartproxy-force");
const smartproxyStatus = document.getElementById("smartproxy-status");
const smartproxyBanTime = document.getElementById("smartproxy-ban-time");
const smartproxyTimeout = document.getElementById("smartproxy-timeout");

async function refreshSmartProxyPanel() {
    const resp = await fetch("/api/settings/smartproxy");
    const data = await resp.json();
    smartproxyEnabled.checked = data.enabled;
    smartproxyRandom.checked = data.random_select;
    smartproxyForce.checked = data.force_smart_proxy;
    smartproxyList.value = data.custom_proxy_list;
    smartproxyBanTime.value = data.ban_time;
    smartproxyTimeout.value = data.proxy_timeout;
    smartproxyStatus.textContent = data.enabled
        ? `ON (${data.proxy_count - data.blocked_count}/${data.proxy_count} available)`
        : "OFF";
}
refreshSmartProxyPanel();

document.getElementById("smartproxy-save").addEventListener("click", async () => {
    const body = new URLSearchParams({
        enabled: smartproxyEnabled.checked ? "true" : "false",
        random_select: smartproxyRandom.checked ? "true" : "false",
        force_smart_proxy: smartproxyForce.checked ? "true" : "false",
        custom_proxy_list: smartproxyList.value,
        ban_time: smartproxyBanTime.value,
        proxy_timeout: smartproxyTimeout.value,
    });
    const resp = await fetch("/api/settings/smartproxy", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    refreshSmartProxyPanel();
});

document.getElementById("smartproxy-refresh").addEventListener("click", async () => {
    await fetch("/api/settings/smartproxy/refresh", { method: "POST" });
    refreshSmartProxyPanel();
});



// ------------------------------------------------------------- downloads ----
const downloadsForm = document.getElementById("downloads-settings-form");

async function loadDownloadSettings() {
    const resp = await fetch("/api/settings/downloads");
    const data = await resp.json();
    downloadsForm.default_dir.value = data.default_dir;
    downloadsForm.max_concurrent.value = data.max_concurrent;
    downloadsForm.default_slots.value = data.default_slots;
    downloadsForm.verify_mac.checked = data.verify_mac;
}
loadDownloadSettings();

downloadsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new URLSearchParams({
        default_dir: downloadsForm.default_dir.value,
        max_concurrent: downloadsForm.max_concurrent.value,
        default_slots: downloadsForm.default_slots.value,
        verify_mac: downloadsForm.verify_mac.checked ? "true" : "false",
    });
    const resp = await fetch("/api/settings/downloads", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    alert("Saved.");
});

// --------------------------------------------------------------- uploads ----
const uploadsForm = document.getElementById("uploads-settings-form");

async function loadUploadSettings() {
    const resp = await fetch("/api/settings/uploads");
    const data = await resp.json();
    uploadsForm.max_concurrent.value = data.max_concurrent;
    uploadsForm.default_slots.value = data.default_slots;
}
loadUploadSettings();

uploadsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new URLSearchParams({
        max_concurrent: uploadsForm.max_concurrent.value,
        default_slots: uploadsForm.default_slots.value,
    });
    const resp = await fetch("/api/settings/uploads", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    alert("Saved.");
});

// -------------------------------------------------------------- advanced ----
const advancedForm = document.getElementById("advanced-settings-form");

async function loadAdvancedSettings() {
    const resp = await fetch("/api/settings/advanced");
    const data = await resp.json();
    advancedForm.mega_api_key.value = data.mega_api_key;
    advancedForm.ram_saver.checked = data.ram_saver;
}
loadAdvancedSettings();

advancedForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new URLSearchParams({
        mega_api_key: advancedForm.mega_api_key.value,
        ram_saver: advancedForm.ram_saver.checked ? "true" : "false",
    });
    const resp = await fetch("/api/settings/advanced", { method: "POST", body });
    if (!resp.ok) { alert(await resp.text()); return; }
    alert("Saved.");
});
