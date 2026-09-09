/* Live now-playing client.
 *
 * This theme uses the window.nowSpinning API initialized by the bootstrap page.
 * The server config (geometry, display, fonts) is passed via window.nowSpinning.config,
 * and state updates arrive via window.nowSpinning.onStateChange().
 *
 * Everything visual is CSS. This only swaps text, points images at URLs, and
 * sets custom properties, so an idle browser on a Pi is not repainting from
 * JavaScript. */

const el = (id) => document.getElementById(id);

const ui = {
  body: document.body,
  stage: el("stage"),
  wash: el("wash"),
  artwork: el("artwork"),
  cover: el("cover"),
  jacketImg: el("jacketImg"),
  discImg: el("discImg"),
  discLayer: el("discLayer"),
  labelArt: el("labelArt"),
  heading: el("heading"),
  title: el("title"),
  artist: el("artist"),
  album: el("album"),
  note: el("note"),
  connection: el("connection"),
};

const ROLES = ["heading", "title", "artist", "album"];

const HEADINGS = {
  idle: "Ready",
  listening: "Listening",
  identifying: "Identifying",
  playing: "Now spinning",
};

const IDLE_TEXT = {
  idle: "Drop the needle",
  listening: "Music is playing",
  identifying: "Working it out",
};

let theme = null;
let currentKey = null;

const pct = (value) => `${value * 100}%`;

function trackKey(track) {
  if (!track) return null;
  return track.provider_id || `${track.artist} ${track.title}`;
}

/* ---- theme ---- */

function applyFonts(fonts) {
  // Point at the server's cached copies rather than a CDN: the browser on a
  // wall-mounted tablet may have no route out, and these are the very files the
  // panel is using. A missing one 404s and the CSS fallback stack takes over.
  const faces = ROLES.map((role) => {
    const spec = fonts[role] || {};
    return `@font-face {
      font-family: "ns-${role}";
      src: url("/api/font/${role}") format("truetype");
      font-display: swap;
      font-weight: ${spec.weight || 400};
      font-style: ${spec.italic ? "italic" : "normal"};
    }`;
  }).join("\n");

  const style = document.createElement("style");
  style.textContent = faces;
  document.head.appendChild(style);

  const root = document.documentElement.style;
  for (const role of ROLES) {
    const spec = fonts[role] || {};
    const stack = [`"ns-${role}"`, spec.family ? `"${spec.family}"` : null, "Georgia", "serif"]
      .filter(Boolean)
      .join(", ");
    root.setProperty(`--font-${role}`, stack);
    root.setProperty(`--weight-${role}`, String(spec.weight || 400));
    root.setProperty(`--style-${role}`, spec.italic ? "italic" : "normal");
    if (spec.color) root.setProperty(`--color-${role}`, spec.color);
  }
}

function applyGeometry(geometry, display) {
  const root = document.documentElement.style;
  const g = geometry;
  const [compW, compH] = g.image_size;

  root.setProperty("--art-aspect", String(compW / compH));

  const artW = g.art_window[2];
  const artH = g.art_window[3];
  root.setProperty("--cover-width", pct(artW / compW));
  root.setProperty("--cover-clip", pct(1.0));
  root.setProperty("--split", pct(0));

  root.setProperty("--sleeve-width", pct(1.0));
  root.setProperty("--sleeve-left", pct(0));
  root.setProperty("--sleeve-height", pct(1.0));
  root.setProperty("--sleeve-top", pct(0));

  // Vinyl record positioning from geometry
  const discCentre = g.disc_centre;
  const discRadius = g.disc_radius;
  const discLeft = Math.max(0, discCentre[0] - discRadius);
  const discSize = discRadius * 2;
  const discTop = Math.max(0, discCentre[1] - discRadius);

  root.setProperty("--disc-left", pct(discLeft));
  root.setProperty("--disc-size", pct(discSize));
  root.setProperty("--disc-top", pct(discTop));
  root.setProperty("--crescent", pct(1.0));

  console.log("Geometry applied:", { discCentre, discRadius, discLeft, discSize, discTop });
}

function applyDisplay(display) {
  const root = document.documentElement.style;
  if (display.background) {
    root.setProperty("--bg", display.background);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", display.background);
  }
  if (display.foreground) root.setProperty("--fg", display.foreground);
  if (display.accent) root.setProperty("--accent", display.accent);
  if (display.rpm) root.setProperty("--spin-duration", `${60 / display.rpm}s`);

  ui.stage.dataset.style = display.style || "sleeve";
  const vinylSetting = display.show_vinyl ? "on" : "off";
  const oldVinyl = ui.body.dataset.vinyl;
  ui.body.dataset.vinyl = vinylSetting;
  if (oldVinyl !== vinylSetting) {
    console.log("applyDisplay - vinyl visibility changed:", oldVinyl, "->", vinylSetting, "show_vinyl:", display.show_vinyl);
  } else {
    console.log("applyDisplay - vinyl already", vinylSetting);
  }
  ui.body.dataset.gloss = display.show_gloss ? "on" : "off";
  ui.body.dataset.shadow = display.show_shadow ? "on" : "off";
  ui.body.dataset.background = display.background_mode || "solid";

  // Offsets are fractions of the cover, so percentages carry them across sizes.
  root.setProperty("--shadow-x", pct(display.shadow_offset_x));
  root.setProperty("--shadow-y", pct(display.shadow_offset_y));
  root.setProperty("--shadow-blur", pct(display.shadow_blur * 0.06));
  root.setProperty(
    "--shadow-color",
    hexToRgba(display.shadow_color || "#000000", display.shadow_opacity),
  );

  root.setProperty("--wash-blur", `${8 + display.background_blur * 56}px`);
  root.setProperty("--wash-dim", String(display.background_dim));

  root.setProperty("--text-shadow", outlineShadow(display));

  const anyText =
    display.show_heading || display.show_title || display.show_artist || display.show_album;
  ui.stage.dataset.text = anyText ? "some" : "none";
  ui.heading.hidden = !display.show_heading;
  ui.title.hidden = !display.show_title;
  ui.artist.hidden = !display.show_artist;
  ui.album.hidden = !display.show_album;
}

function hexToRgba(hex, alpha) {
  const text = hex.replace("#", "");
  const full = text.length === 3 ? text.split("").map((c) => c + c).join("") : text;
  const n = parseInt(full, 16);
  if (Number.isNaN(n) || full.length !== 6) return `rgba(0, 0, 0, ${alpha})`;
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function outlineShadow(display) {
  if (!display.text_outline) return "none";
  // A ring of offsets, like the panel: the corners of a filled square thicken
  // the diagonals and make small text look furry.
  const w = display.text_outline_width || 2;
  const c = display.text_outline_color || "#000000";
  const ring = [
    [-1, 0], [1, 0], [0, -1], [0, 1],
    [-1, -1], [1, -1], [-1, 1], [1, 1],
  ];
  return ring.map(([dx, dy]) => `${dx * w}px ${dy * w}px 0 ${c}`).join(", ");
}

/* ---- state ---- */

function render(state) {
  const track = state.track;
  const key = trackKey(track);
  const changed = key !== currentKey;
  currentKey = key;

  ui.stage.dataset.status = state.status;

  const display = (theme && theme.display) || {};
  console.log("render() called with state:", { status: state.status, hasTrack: !!track, display });

  const heading =
    track && display.heading_text ? display.heading_text : HEADINGS[state.status] || "Ready";
  ui.heading.textContent = heading;

  if (track) {
    ui.title.textContent = track.title;
    ui.artist.textContent = track.artist;
    ui.album.textContent = track.album || "";
    ui.note.textContent = "";
    document.title = `${track.artist} - ${track.title}`;
  } else {
    ui.title.textContent = IDLE_TEXT[state.status] || IDLE_TEXT.idle;
    ui.artist.textContent = "";
    ui.album.textContent = "";
    ui.note.textContent = state.message || "";
    document.title = "now spinning";
  }

  const art = state.artwork || "/api/asset/sleeve-noart.png";
  if (changed || ui.cover.getAttribute("src") !== art) {
    ui.cover.src = art;
    ui.labelArt.src = art;
  }
  ui.labelArt.hidden = !state.artwork;

  if (display.background_mode === "artwork") {
    document.documentElement.style.setProperty(
      "--wash-image",
      state.artwork ? `url("${state.artwork}")` : "none",
    );
  }
}

function connect() {
  const source = new EventSource("/api/stream");
  source.onopen = () => {
    ui.connection.hidden = true;
  };
  source.onmessage = (event) => {
    try {
      const state = JSON.parse(event.data);
      // Update both the theme and the nowSpinning API
      window.nowSpinning._fireStateChange(state);
      render(state);
    } catch (err) {
      console.error("bad state payload", err);
    }
  };
  source.onerror = () => {
    ui.connection.hidden = false;
  };
}

function start() {
  // Config is already loaded by bootstrap and available in window.nowSpinning.config
  const config = window.nowSpinning.config;

  // Apply theme configuration
  if (config.fonts) applyFonts(config.fonts);
  if (config.geometry) applyGeometry(config.geometry, config.display);
  if (config.display) applyDisplay(config.display);

  // Load component assets (case and vinyl)
  ui.jacketImg.src = "/api/asset/sleeve.png";
  ui.discImg.src = "/api/asset/vinyl.png";

  console.log("Start function:", {
    discImgElement: ui.discImg,
    discImgSrc: ui.discImg?.src,
    discImgDisplay: ui.discImg ? window.getComputedStyle(ui.discImg).display : "N/A",
    discImgWidth: ui.discImg ? window.getComputedStyle(ui.discImg).width : "N/A",
    discImgHeight: ui.discImg ? window.getComputedStyle(ui.discImg).height : "N/A",
    discLayerDisplay: ui.discLayer ? window.getComputedStyle(ui.discLayer).display : "N/A",
    bodyDataVinyl: document.body.dataset.vinyl,
  });

  // Subscribe to state changes via the nowSpinning API
  window.nowSpinning.onStateChange((state) => {
    render(state);
  });

  // Connect to SSE stream for live updates
  connect();
}

// Start when the DOM is ready
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
