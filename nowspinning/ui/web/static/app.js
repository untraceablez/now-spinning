/* Live now-playing client.
 *
 * Everything visual is CSS; this only swaps text, points the <img> at the cached
 * cover, and sets data-status. The record's rotation is a CSS animation, so an
 * idle browser on a Pi is not repainting anything from JavaScript. */

const el = {
  stage: document.getElementById("stage"),
  heading: document.getElementById("heading"),
  title: document.getElementById("title"),
  artist: document.getElementById("artist"),
  album: document.getElementById("album"),
  note: document.getElementById("note"),
  art: document.getElementById("art"),
  connection: document.getElementById("connection"),
};

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

let currentKey = null;

function trackKey(track) {
  if (!track) return null;
  return track.provider_id || `${track.artist} ${track.title}`;
}

function applyTheme(theme) {
  const root = document.documentElement.style;
  if (theme.background) root.setProperty("--bg", theme.background);
  if (theme.foreground) root.setProperty("--fg", theme.foreground);
  if (theme.accent) root.setProperty("--accent", theme.accent);
  if (theme.rpm) root.setProperty("--spin-duration", `${60 / theme.rpm}s`);
  if (theme.background) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme.background);
  }
}

function render(state) {
  const track = state.track;
  const key = trackKey(track);
  const changed = key !== currentKey;
  currentKey = key;

  el.stage.dataset.status = state.status;
  el.heading.textContent = HEADINGS[state.status] || "Ready";

  if (track) {
    el.title.textContent = track.title;
    el.artist.textContent = track.artist;
    el.album.textContent = track.album || "";
    el.note.textContent = "";
    document.title = `${track.artist} - ${track.title}`;
  } else {
    el.title.textContent = IDLE_TEXT[state.status] || IDLE_TEXT.idle;
    el.artist.textContent = "";
    el.album.textContent = "";
    el.note.textContent = state.message || "";
    document.title = "now spinning";
  }

  if (state.artwork) {
    if (changed || el.art.getAttribute("src") !== state.artwork) {
      el.art.src = state.artwork;
    }
    el.art.hidden = false;
  } else {
    el.art.hidden = true;
    el.art.removeAttribute("src");
  }
}

function connect() {
  const source = new EventSource("/api/stream");

  source.onopen = () => {
    el.connection.hidden = true;
  };

  source.onmessage = (event) => {
    try {
      render(JSON.parse(event.data));
    } catch (err) {
      console.error("bad state payload", err);
    }
  };

  source.onerror = () => {
    // EventSource retries on its own; the banner just tells whoever is looking
    // at the wall display that the screen has gone stale.
    el.connection.hidden = false;
  };
}

async function start() {
  try {
    const response = await fetch("/api/theme");
    applyTheme(await response.json());
  } catch (err) {
    console.warn("using default theme", err);
  }
  try {
    const response = await fetch("/api/now-playing");
    render(await response.json());
  } catch (err) {
    console.warn("no initial state", err);
  }
  connect();
}

start();
