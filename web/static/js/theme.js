// Light/dark theme toggle + accent color picker.
//
// The initial theme AND the cached accent variables are applied before paint
// by a small inline script in each template's <head> (it reads mb-theme and
// mb-accent-vars from localStorage) to avoid a flash. This file handles the
// interactive parts: flipping the theme, and the Windows-accent-palette
// popover that derives a full light+dark variable set from one base color.
//
// localStorage keys:
//   mb-theme        "light" | "dark"
//   mb-accent       the chosen base hex (absent = default teal from the CSS)
//   mb-accent-vars  cached {dark: {--var: value}, light: {...}} for the head
//                   script, so derivation only runs when picking a color.
(function () {
    var DEFAULT_ACCENT = "#14b8a6"; // matches the handcrafted teal CSS

    // The Windows 11 accent palette ("Windows colors" grid), preceded by the
    // app's default teal so there's always a way back to stock.
    var PALETTE = [
        ["Teal (default)", DEFAULT_ACCENT],
        ["Yellow gold", "#FFB900"], ["Gold", "#FF8C00"], ["Orange bright", "#F7630C"],
        ["Orange dark", "#CA5010"], ["Rust", "#DA3B01"], ["Pale rust", "#EF6950"],
        ["Brick red", "#D13438"], ["Mod red", "#FF4343"], ["Pale red", "#E74856"],
        ["Red", "#E81123"], ["Rose bright", "#EA005E"], ["Rose", "#C30052"],
        ["Plum light", "#E3008C"], ["Plum", "#BF0077"], ["Orchid light", "#C239B3"],
        ["Orchid", "#9A0089"], ["Default blue", "#0078D7"], ["Navy blue", "#0063B1"],
        ["Purple shadow", "#8E8CD8"], ["Purple shadow dark", "#6B69D6"], ["Iris pastel", "#8764B8"],
        ["Iris spring", "#744DA9"], ["Violet red light", "#B146C2"], ["Violet red", "#881798"],
        ["Cool blue bright", "#0099BC"], ["Cool blue", "#2D7D9A"], ["Seafoam", "#00B7C3"],
        ["Seafoam teal", "#038387"], ["Mint light", "#00B294"], ["Mint dark", "#018574"],
        ["Turf green", "#00CC6A"], ["Sport green", "#10893E"], ["Gray", "#7A7574"],
        ["Gray brown", "#5D5A58"], ["Steel blue", "#68768A"], ["Metal blue", "#515C6B"],
        ["Pale moss", "#567C73"], ["Moss", "#486860"], ["Meadow green", "#498205"],
        ["Green", "#107C10"], ["Overcast", "#767676"], ["Storm", "#4C4A48"],
        ["Blue gray", "#69797E"], ["Gray dark", "#4A5459"], ["Liddy green", "#647C64"],
        ["Sage", "#525E54"], ["Camouflage desert", "#847545"], ["Camouflage", "#7E735F"]
    ];

    // -- color math -------------------------------------------------------

    function hexToRgb(hex) {
        return {
            r: parseInt(hex.slice(1, 3), 16),
            g: parseInt(hex.slice(3, 5), 16),
            b: parseInt(hex.slice(5, 7), 16),
        };
    }

    function rgbToHex(c) {
        function h(v) { return ("0" + Math.round(v).toString(16)).slice(-2); }
        return "#" + h(c.r) + h(c.g) + h(c.b);
    }

    // Weighted blend: t is the share of the accent, (1-t) the share of `into`.
    function mix(accentHex, intoHex, t) {
        var a = hexToRgb(accentHex), b = hexToRgb(intoHex);
        return rgbToHex({
            r: a.r * t + b.r * (1 - t),
            g: a.g * t + b.g * (1 - t),
            b: a.b * t + b.b * (1 - t),
        });
    }

    function relLuminance(hex) {
        var c = hexToRgb(hex);
        function lin(v) {
            v /= 255;
            return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
        }
        return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
    }

    // -- theme variable derivation -----------------------------------------

    // Everything is a small dose of the accent mixed into a neutral base;
    // the fractions are tuned so the default teal reproduces the handcrafted
    // stylesheet values closely.
    function derive(base) {
        var accentDark = mix(base, "#ffffff", 0.78);
        var accentLight = mix(base, "#000000", 0.88);
        return {
            dark: {
                "--bg": mix(base, "#0a0b0c", 0.07),
                "--header-bg": mix(base, "#0f1213", 0.09),
                "--panel": mix(base, "#101314", 0.09),
                "--border": mix(base, "#212729", 0.10),
                "--text": mix(base, "#e8ecec", 0.08),
                "--muted": mix(base, "#8a9496", 0.15),
                "--accent": accentDark,
                "--accent-strong": base,
                "--on-accent": relLuminance(accentDark) > 0.35 ? mix(base, "#000000", 0.25) : "#ffffff",
                "--input-bg": mix(base, "#0d1011", 0.06),
                "--input-bg-deep": mix(base, "#090b0c", 0.05),
                "--track": mix(base, "#161b1c", 0.09),
                "--tint-bg": mix(base, "#10171a", 0.18),
                "--tint-bg-hover": mix(base, "#131c1f", 0.24),
                "--tint-text": mix(base, "#ffffff", 0.62),
            },
            light: {
                "--bg": mix(base, "#f2f5f5", 0.05),
                "--header-bg": "#ffffff",
                "--panel": "#ffffff",
                "--border": mix(base, "#dfe6e6", 0.09),
                "--text": mix(base, "#101617", 0.15),
                "--muted": mix(base, "#606e6e", 0.12),
                "--accent": accentLight,
                "--accent-strong": mix(base, "#000000", 0.78),
                "--on-accent": relLuminance(accentLight) > 0.42 ? mix(base, "#000000", 0.22) : "#ffffff",
                "--input-bg": mix(base, "#f6f9f9", 0.03),
                "--input-bg-deep": mix(base, "#f0f4f4", 0.03),
                "--track": mix(base, "#e1e9e9", 0.07),
                "--tint-bg": mix(base, "#e7efee", 0.13),
                "--tint-bg-hover": mix(base, "#dde7e6", 0.17),
                "--tint-text": mix(base, "#000000", 0.42),
            },
        };
    }

    var VAR_NAMES = Object.keys(derive(DEFAULT_ACCENT).dark);

    // -- state ---------------------------------------------------------------

    function currentTheme() {
        return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    function currentAccent() {
        try {
            return localStorage.getItem("mb-accent") || DEFAULT_ACCENT;
        } catch (e) {
            return DEFAULT_ACCENT;
        }
    }

    function applyVars(mode) {
        var root = document.documentElement;
        var accent = currentAccent();
        if (accent === DEFAULT_ACCENT) {
            // Stock teal: fall back to the handcrafted stylesheet values.
            VAR_NAMES.forEach(function (name) { root.style.removeProperty(name); });
            return;
        }
        var vars = derive(accent)[mode];
        VAR_NAMES.forEach(function (name) { root.style.setProperty(name, vars[name]); });
    }

    function setAccent(hex) {
        try {
            if (hex === DEFAULT_ACCENT) {
                localStorage.removeItem("mb-accent");
                localStorage.removeItem("mb-accent-vars");
            } else {
                localStorage.setItem("mb-accent", hex);
                localStorage.setItem("mb-accent-vars", JSON.stringify(derive(hex)));
            }
        } catch (e) {
            // private mode: no persistence, still applies to this page
        }
        applyVars(currentTheme());
    }

    function toggleTheme() {
        var next = currentTheme() === "light" ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", next);
        try {
            localStorage.setItem("mb-theme", next);
        } catch (e) {
            // localStorage can throw in private mode; the toggle still works
            // for the current page, it just won't persist.
        }
        applyVars(next);
    }

    // -- accent popover -------------------------------------------------------

    var popover = null;

    function markSelected() {
        if (!popover) return;
        var accent = currentAccent();
        popover.querySelectorAll(".accent-swatch").forEach(function (btn) {
            btn.classList.toggle("selected", btn.dataset.accent === accent);
        });
    }

    function buildPopover(anchorNav) {
        popover = document.createElement("div");
        popover.className = "accent-popover hidden";
        popover.setAttribute("role", "listbox");
        popover.setAttribute("aria-label", "Accent color");
        PALETTE.forEach(function (entry) {
            var name = entry[0], hex = entry[1];
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "accent-swatch";
            btn.style.background = hex;
            btn.title = name;
            btn.setAttribute("aria-label", name);
            btn.dataset.accent = hex;
            btn.addEventListener("click", function () {
                setAccent(hex);
                markSelected();
            });
            popover.appendChild(btn);
        });
        anchorNav.appendChild(popover);
    }

    document.addEventListener("click", function (e) {
        if (e.target.closest("[data-theme-toggle]")) {
            toggleTheme();
            return;
        }
        var pickerBtn = e.target.closest("[data-accent-picker]");
        if (pickerBtn) {
            if (!popover) buildPopover(pickerBtn.closest("nav"));
            markSelected();
            popover.classList.toggle("hidden");
            return;
        }
        // Any click outside the popover closes it (swatch clicks keep it open
        // so several colors can be compared quickly).
        if (popover && !e.target.closest(".accent-popover")) {
            popover.classList.add("hidden");
        }
    });
})();
