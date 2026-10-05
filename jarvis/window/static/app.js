/*
  The HUD's one script. No framework, no build step, no modules: one file the
  server can hand over as it is, under a CSP that allows nothing else.

  THE WINDOW IS A VIEWER. It reads what other processes wrote to the database —
  the desk's heartbeat, the event log, the request rows — through one HTTP API,
  and it writes only by asking that API to do something. It never assumes it is
  the only window, and it never assumes the server outlived its last request.

  THE STREAM DRIVES EVERYTHING. One EventSource carries new feed items, process
  state changes, and a "refresh" nudge that re-reads the snapshot (debounced).
  Nothing polls on an interval: an idle desk costs this page nothing but the
  server's keep-alive comment every fifteen seconds.

  EVERY STRING FROM THE SERVER IS DATA. The feed holds whatever was said aloud,
  and a sentence is not markup, so all of it goes in through textContent and
  createElement — never through an HTML parser.

  UPDATES ARE KEYED. Lists are reconciled by id: a refresh touches the nodes
  whose text changed and leaves the rest alone, so a half-picked answer or an
  open tool form survives the snapshot arriving underneath it.

  INSIDE THE APP, NO TERMINAL. When the snapshot says this window runs inside
  the Jarvis app, the app starts and restarts every process itself, and the
  person looking at this page may never have opened a terminal. Then no line
  here tells anybody to type a command: every fix is a button. The commands
  live in one table (TERMINAL) that only answers outside the app, and text
  written by other processes for a terminal passes through withoutCommands().
*/
(() => {
  "use strict";

  // The whole surface this page may call. A test pins it to the server's contract.
  const API = Object.freeze({
    state: "/api/state",
    feed: "/api/feed",
    stream: "/api/stream",
    tool: "/api/tool",
    chat: "/api/chat",
    say: "/api/say",
    answer: "/api/answer",
    stop: "/api/stop",
    setup: "/api/setup",
    setupSecret: "/api/setup/secret",
    setupSetting: "/api/setup/setting",
    setupWake: "/api/setup/wake",
    setupPreview: "/api/setup/preview",
    setupClaude: "/api/setup/claude",
    setupPhone: "/api/setup/phone",
    appStatus: "/api/app",
    appRestart: "/api/app/restart",
    appQuit: "/api/app/quit",
  });

  // The only terminal commands on this page, and they answer only outside the
  // app. A test pins every command in this file to this block.
  const TERMINAL = Object.freeze({
    desk: "python -m jarvis desk",
    schedule: "python -m jarvis.schedule",
    telegram: "python -m jarvis.telegram",
    window: "python -m jarvis window",
  });

  const TOKEN_KEY = "jarvis.window.token";
  const SPEAK_KEY = "jarvis.window.speakReplies";
  const TAB_KEY = "jarvis.window.tab";
  const ONBOARD_KEY = "jarvis.window.onboarded";

  const FEED_CAP = 300;
  const FEED_FIRST_PAGE = 200;
  const REFRESH_DEBOUNCE_MS = 300;
  const HOLD_MS = 1000;
  const BACKOFF_FIRST_MS = 1000;
  const BACKOFF_MAX_MS = 15000;
  const TOAST_CAP = 4;
  const APP_REFRESH_MS = 400;

  const DESK_STATES = new Set(["asleep", "awake", "listening", "speaking"]);
  const ROLES = new Set(["user", "jarvis", "system", "tool"]);
  const TABS = ["tools", "reminders", "notes", "questions", "builds", "hearing", "settings"];
  const TAB_TITLES = {
    tools: "Tools",
    reminders: "Reminders",
    notes: "Notes",
    questions: "Questions",
    builds: "Builds",
    hearing: "Hearing",
    settings: "Settings",
  };
  // The tab matrix is four wide; the arrow keys move by its rows.
  const TAB_ROW = 4;

  const PROCS = [
    { name: "desk", label: "Desk", the: "the desk" },
    { name: "schedule", label: "Scheduler", the: "the scheduler" },
    { name: "telegram", label: "Telegram", the: "Telegram" },
  ];

  const DESK_WORDS = {
    unknown: "Connecting",
    offline: "Offline",
    starting: "Starting",
    waiting: "Waiting",
    asleep: "Asleep",
    awake: "Awake",
    listening: "Listening",
    speaking: "Speaking",
  };

  // Feed kinds that mean the app's processes changed: re-read what is wrong.
  const APP_KINDS = new Set(["desk.refused", "app.process_exited", "app.started"]);

  const ONBOARD_STEPS = ["key", "mic", "name", "city", "done"];

  const SECRET_INFO = {
    gemini_api_key: { label: "Gemini API key", purpose: "My voice and my thinking. Nothing works without it." },
    telegram_bot_token: { label: "Telegram bot token", purpose: "Questions and answers on your phone." },
    github_token: { label: "GitHub token", purpose: "Creating a repository before a build starts." },
    maxmind_license_key: { label: "MaxMind licence key", purpose: "Knowing roughly where you are, offline." },
  };

  const KIND_WORDS = {
    plan_question: "Plan question",
    exit_plan: "Plan approval",
    tool_permission: "Permission",
    confirm_effect: "Confirm",
    readback: "Read-back",
    briefing_gate: "Briefing",
    free_text: "Notice",
  };

  // Tones follow the job state machine in jarvis/jobs.py: amber for "waiting on
  // something", red for "ended badly", dim for "ended well", cyan for moving.
  const JOB_TONES = {
    blocked: "warn",
    deferred: "warn",
    parked: "warn",
    done: "done",
    failed: "bad",
    killed: "bad",
    orphaned: "bad",
  };

  // Quick actions are thin presets over real registry tools. A preset whose
  // tool is not offered to this window is disabled, never faked.
  const QUICK = {
    "weather-now": { tool: "weather", args: { when: "now" } },
    "weather-tomorrow": { tool: "weather", args: { when: "tomorrow" } },
    "weather-week": { tool: "weather", args: { when: "week" } },
    where: { tool: "where_am_i", args: {} },
    time: {
      tool: "local_time",
      submit: "Get the time",
      fields: {
        place: { type: "STRING", title: "Time in", example: "Tokyo", description: "Empty for here." },
      },
    },
    search: {
      tool: "web_search",
      submit: "Search",
      required: ["query"],
      fields: { query: { type: "STRING", title: "Search for", example: "opening hours of the post office" } },
    },
    remind: {
      tool: "remind_me",
      submit: "Set reminder",
      required: ["what", "when"],
      fields: {
        what: { type: "STRING", title: "Remind me to", example: "call mum" },
        when: { type: "STRING", title: "When", example: "in 20 minutes · at 6pm · tomorrow at 9" },
      },
    },
    remember: {
      tool: "remember",
      submit: "Remember",
      required: ["fact"],
      fields: { fact: { type: "STRING", title: "Remember", example: "my locker is 214" } },
    },
    say: {
      say: true,
      submit: "Say it",
      required: ["text"],
      fields: {
        text: {
          type: "STRING",
          title: "Say aloud",
          example: "Dinner is ready",
          description: "Through the desk's speaker when the desk is running, otherwise here.",
        },
      },
    },
  };

  // All mutable page state lives here, in this closure, and nowhere else.
  const app = {
    token: "",
    locked: false,
    snap: null,
    procs: {},
    lastSeq: null,
    highSeq: -1,
    stream: null,
    catchingUp: null,
    backoff: BACKOFF_FIRST_MS,
    reconnectTimer: 0,
    refreshTimer: 0,
    refreshing: false,
    refreshAgain: false,
    clockTimer: 0,
    tools: new Map(),
    toolsSig: "",
    speech: { available: false, via: null, why: "" },
    chatAvailable: false,
    chatBusy: false,
    palette: { all: [], shown: [], active: 0 },
    // Inside the Jarvis app (the snapshot says so), and what its supervisor says.
    inApp: false,
    appProcs: {},
    appLoaded: false,
    appTimer: 0,
    pairTimer: 0,
    setup: null,
    setupAvailable: null,
    tzFilled: false,
    ob: { open: false, step: "key", wake: "idle", wakeMsg: "", unapplied: false },
    orbAction: null,
    deskKnown: false,
  };

  const dom = {};

  // ───────────────────────────── small helpers ─────────────────────────────

  function byId(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // Writing an unchanged value still dirties the node, so every patch compares first.
  function setText(node, text) {
    const value = text === undefined || text === null ? "" : String(text);
    if (node.textContent !== value) node.textContent = value;
  }

  function setAttr(node, name, value) {
    const v = String(value);
    if (node.getAttribute(name) !== v) node.setAttribute(name, v);
  }

  function show(node, visible) {
    if (node.hidden === visible) node.hidden = !visible;
  }

  function fromTemplate(id) {
    return byId(id).content.firstElementChild.cloneNode(true);
  }

  function option(value, label) {
    const o = el("option", "", label);
    o.value = value;
    return o;
  }

  function humanize(name) {
    const words = String(name || "").replace(/_/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function firstSentence(text) {
    const s = String(text || "").trim();
    const m = /^(.{8,180}?[.!?])(\s|$)/.exec(s);
    if (m) return m[1];
    return s.length > 180 ? `${s.slice(0, 179)}…` : s;
  }

  function plural(n, one, many) {
    return `${n} ${n === 1 ? one : many}`;
  }

  // Storage can be missing or throw (a private window, blocked site data). It
  // only ever holds conveniences, so losing it costs a preference, not a feature.
  function readStore(area, key) {
    try {
      return window[area].getItem(key);
    } catch {
      return null;
    }
  }

  function writeStore(area, key, value) {
    try {
      if (value === null) window[area].removeItem(key);
      else window[area].setItem(key, value);
    } catch {
      /* a convenience lost, nothing more */
    }
  }

  function parseJson(text) {
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  // ───────────────────────────── text without commands ─────────────────────────────

  // Other processes write their refusals for a terminal ("store it with python
  // -m ..."). Inside the app those instructions are wrong — there is a button
  // for every one — so they are taken out, sentence by sentence. The same rule
  // as the server's snapshot.plain_sentence, kept in step by a test.
  const COMMANDISH =
    /`[^`]*`|\bpython3?(?:\.exe)?\s+-[mc]\b|\bpy\s+-m\b|\bpip\s+install\b|\buv\s+(?:pip|venv|run)\b|\bsudo\b|\bapt(?:-get)?\s+install\b|\bbrew\s+install\b|\.venv\b|\bexport\s+[A-Z_]+=|\bjarvis\s+(?:secrets|wake|desk|window|doctor)\b/i;
  const COMMAND_TAIL =
    /\s*(?:[:;\u2014\u2013]|\s-)\s*(?:run|try|use|start it with|store it with|with|or)?\s*:?\s*`?(?:python3?|py|pip|uv|sudo|apt)\b.*$/i;
  const STUMP = /\b(?:with|run|try|use|using|via|by|to|set|it|is)$/i;

  function lineWithoutCommands(line) {
    const unparened = line.replace(/\s*\([^()]*\)/g, (m) => (COMMANDISH.test(m) ? "" : m));
    const kept = [];
    for (const sentence of unparened.trim().split(/(?<=[.!?])\s+/)) {
      const cut = sentence.replace(COMMAND_TAIL, "").trim();
      if (cut !== sentence.trim() && (kept.length || STUMP.test(cut))) continue;
      if (cut && !COMMANDISH.test(cut)) kept.push(cut);
    }
    return kept.join(" ");
  }

  // Every line, each without its commands: text whose later lines matter (a
  // week of weather) keeps them, and a line that was only a command goes.
  function withoutCommands(text) {
    const out = [];
    for (const line of String(text || "").split(/\r?\n/)) {
      if (!COMMANDISH.test(line)) {
        out.push(line);
        continue;
      }
      const kept = lineWithoutCommands(line);
      if (kept) out.push(/[.!?…]$/.test(kept) ? kept : `${kept}.`);
    }
    return out.join("\n").trim();
  }

  // What the page shows for text another process wrote: as it is outside the
  // app, without its terminal instructions inside it.
  function appText(text, fallback) {
    const value = String(text || "");
    if (!app.inApp) return value || fallback || "";
    return withoutCommands(value) || fallback || "";
  }

  function terminal(name) {
    return app.inApp ? "" : TERMINAL[name] || "";
  }

  // ───────────────────────────── time ─────────────────────────────

  // The server speaks UTC; the person reading this lives in local time.
  const clockFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
  const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" });
  const dateFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "2-digit", month: "short" });

  function parseTs(ts) {
    if (!ts) return null;
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function sameDay(a, b) {
    return (
      a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
    );
  }

  function fmtClock(ts) {
    const d = parseTs(ts);
    return d ? clockFmt.format(d) : "";
  }

  function fmtWhen(ts) {
    const d = parseTs(ts);
    if (!d) return "";
    return sameDay(d, new Date()) ? clockFmt.format(d) : `${dayFmt.format(d)} ${clockFmt.format(d)}`;
  }

  // One timer, re-armed for the next minute boundary: the clock shows minutes,
  // so waking every second would be sixty wake-ups to change nothing.
  function tickClock() {
    clearTimeout(app.clockTimer);
    const now = new Date();
    setText(dom.clockTime, clockFmt.format(now));
    setText(dom.clockDate, dateFmt.format(now).toUpperCase());
    const wait = 60000 - (now.getSeconds() * 1000 + now.getMilliseconds()) + 25;
    app.clockTimer = setTimeout(tickClock, wait);
  }

  // ───────────────────────────── the API ─────────────────────────────

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, body) {
    const headers = { "X-Jarvis-Token": app.token };
    const init = { method: "GET", headers, cache: "no-store" };
    if (body !== undefined) {
      init.method = "POST";
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new ApiError(
        app.inApp
          ? "Jarvis isn't answering; it may have been closed. Open it again from its icon."
          : `The window server is not answering. Is ${terminal("window")} still running?`,
        0,
      );
    }
    if (res.status === 401) {
      lockOut();
      throw new ApiError("This window's key was refused.", 401);
    }
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok || !data || data.ok === false) {
      const why =
        data && typeof data.error === "string" && data.error
          ? data.error
          : `The window server answered ${res.status}.`;
      throw new ApiError(why, res.status);
    }
    return data;
  }

  // ───────────────────────────── the key ─────────────────────────────

  // The launcher puts the key in the FRAGMENT, which never reaches a server log
  // or a Referer. It moves to sessionStorage (this tab only) and leaves the
  // address bar at once, so a screenshot of the window does not leak it.
  function takeToken() {
    let token = "";
    const m = /(?:^#|&)t=([^&]*)/.exec(location.hash);
    if (m) {
      try {
        token = decodeURIComponent(m[1]);
      } catch {
        token = "";
      }
      if (token) writeStore("sessionStorage", TOKEN_KEY, token);
      try {
        history.replaceState(null, "", location.pathname + location.search);
      } catch {
        /* the key still works; it is only still visible */
      }
    }
    return token || readStore("sessionStorage", TOKEN_KEY) || "";
  }

  function lockOut() {
    if (app.locked) return;
    app.locked = true;
    closeStream();
    clearTimeout(app.reconnectTimer);
    clearTimeout(app.refreshTimer);
    app.refreshTimer = 0;
    writeStore("sessionStorage", TOKEN_KEY, null);
    setLink("off");
    if (dom.onboard) dom.onboard.hidden = true;
    dom.app.setAttribute("inert", "");
    dom.gate.hidden = false;
  }

  // ───────────────────────────── the stream ─────────────────────────────

  function bumpSeq(n) {
    if (typeof n === "number" && (app.lastSeq === null || n > app.lastSeq)) app.lastSeq = n;
  }

  function closeStream() {
    if (app.stream) {
      app.stream.close();
      app.stream = null;
    }
  }

  // EventSource retries by itself, but always with the URL it was born with —
  // a stale `after` that replays or skips. So on error it is closed and a new
  // one is made with the newest `after`, on a backoff.
  function openStream() {
    closeStream();
    if (app.locked) return;
    const q = new URLSearchParams({ t: app.token });
    if (app.lastSeq !== null) q.set("after", String(app.lastSeq));
    const es = new EventSource(`${API.stream}?${q}`);
    app.stream = es;
    es.addEventListener("open", () => {
      app.backoff = BACKOFF_FIRST_MS;
      setLink("live");
    });
    es.addEventListener("feed", (e) => {
      const d = parseJson(e.data);
      if (!d) return;
      appendFeed(d.items);
      bumpSeq(d.last_seq);
    });
    es.addEventListener("state", (e) => {
      const d = parseJson(e.data);
      if (d && d.processes) applyProcesses(d.processes);
    });
    es.addEventListener("refresh", scheduleRefresh);
    es.addEventListener("error", () => {
      if (app.stream !== es) return;
      closeStream();
      setLink("retry");
      scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    clearTimeout(app.reconnectTimer);
    if (app.locked) return;
    const wait = app.backoff;
    app.backoff = Math.min(app.backoff * 2, BACKOFF_MAX_MS);
    app.reconnectTimer = setTimeout(reconnect, wait);
  }

  // A plain GET first: an EventSource cannot say WHY it failed, and a 401 here
  // turns "retrying forever" into the locked notice the user can act on.
  async function reconnect() {
    if (app.locked) return;
    try {
      await loadState();
      await catchUpFeed();
      openStream();
    } catch {
      if (!app.locked) scheduleReconnect();
    }
  }

  function setLink(mode) {
    const live = mode === "live" ? "true" : mode === "retry" ? "retry" : "false";
    setAttr(dom.link, "data-live", live);
    const words = { live: "live", retry: "reconnecting", connecting: "connecting", off: "offline" };
    setText(dom.linkText, words[mode] || "offline");
    setAttr(dom.app, "data-link", mode);
    dom.netbar.classList.toggle("is-on", mode === "retry");
    // The words are written when the bar appears, so a screen reader hears it.
    setText(dom.netbarText, mode === "retry" ? "Reconnecting…" : "");
  }

  // The first nudge arms the timer and later ones ride along. Re-arming on every
  // nudge would starve the snapshot for as long as the desk keeps talking: the
  // server can send a refresh on every 250 ms poll, inside a 300 ms window.
  function scheduleRefresh() {
    if (app.refreshTimer || app.locked) return;
    app.refreshTimer = setTimeout(() => {
      app.refreshTimer = 0;
      runRefresh();
    }, REFRESH_DEBOUNCE_MS);
  }

  async function runRefresh() {
    if (app.refreshing) {
      app.refreshAgain = true;
      return;
    }
    app.refreshing = true;
    try {
      await loadState();
      await loadApp();
    } catch {
      /* a dead server is the stream's to notice; it already shows the bar */
    } finally {
      app.refreshing = false;
      if (app.refreshAgain) {
        app.refreshAgain = false;
        scheduleRefresh();
      }
    }
  }

  async function loadState() {
    const snap = await api(API.state);
    applySnapshot(snap);
    return snap;
  }

  async function loadFeed() {
    const data = await api(`${API.feed}?limit=${FEED_FIRST_PAGE}`);
    appendFeed(data.items);
    bumpSeq(data.last_seq);
  }

  // Single-flight: a reconnect and a chat reply can both ask at once, and two
  // reads racing the stream can land out of order.
  function catchUpFeed() {
    if (app.catchingUp) return app.catchingUp;
    app.catchingUp = (async () => {
      if (app.lastSeq === null) {
        await loadFeed();
        return;
      }
      const data = await api(`${API.feed}?after=${app.lastSeq}&limit=500`);
      appendFeed(data.items);
      bumpSeq(data.last_seq);
    })().finally(() => {
      app.catchingUp = null;
    });
    return app.catchingUp;
  }

  function streamIsLive() {
    return Boolean(app.stream) && app.stream.readyState === EventSource.OPEN;
  }

  // ───────────────────────────── keyed lists ─────────────────────────────

  // Reconcile `list`'s children against `items` by key: reuse, patch, reorder
  // and drop — never rebuild. `patch` must compare before it writes.
  function syncList(list, items, keyOf, make, patch) {
    const existing = new Map();
    for (const node of Array.from(list.children)) existing.set(node.dataset.key, node);
    let cursor = list.firstElementChild;
    for (const item of items) {
      const key = String(keyOf(item));
      let node = existing.get(key);
      if (node) {
        existing.delete(key);
      } else {
        node = make(item);
        node.dataset.key = key;
      }
      patch(node, item);
      if (node === cursor) cursor = cursor.nextElementSibling;
      else list.insertBefore(node, cursor);
    }
    for (const node of existing.values()) node.remove();
  }

  // ───────────────────────────── the snapshot ─────────────────────────────

  function applySnapshot(s) {
    app.snap = s;
    setInApp(s.app === true);
    app.speech = s.speech || { available: false, via: null, why: "" };
    applyProcesses(s.processes || {});
    renderReadouts(s);
    renderComposer(s.chat || {});
    renderTools(Array.isArray(s.tools) ? s.tools : []);
    renderReminders(Array.isArray(s.reminders) ? s.reminders : []);
    renderNotes(Array.isArray(s.notes) ? s.notes : []);
    renderQuestions(Array.isArray(s.pending) ? s.pending : []);
    renderBuilds(Array.isArray(s.jobs) ? s.jobs : [], (s.projects && s.projects.lines) || []);
    renderHearing(Array.isArray(s.hearing) ? s.hearing : []);
  }

  function isOnline(info) {
    return !!info && info.online === true && info.state !== "offline";
  }

  function applyProcesses(procs) {
    const deskWas = isOnline(app.procs.desk);
    app.procs = procs || {};
    for (const p of PROCS) {
      const info = app.procs[p.name] || null;
      const online = isOnline(info);
      const [chip, stateEl] = dom.chips[p.name];
      const sup = app.inApp ? app.appProcs[p.name] || null : null;
      let state = online ? String(info.state || "running") : "offline";
      let word = online ? state : "off";
      if (!online && sup && sup.held) [state, word] = ["held", "waiting"];
      else if (!online && sup && sup.running) [state, word] = ["starting", "starting"];
      setAttr(chip, "data-online", online);
      setAttr(chip, "data-state", state);
      setText(stateEl, word);
      const since = online && info.since ? ` since ${fmtWhen(info.since)}` : "";
      setAttr(chip, "title", online ? `${p.label} is running — ${state}${since}.` : offTitle(p, sup));
    }
    renderDesk(app.procs.desk || null);
    renderVoice();
    const scheduleDown = !!app.snap && !isOnline(app.procs.schedule);
    show(dom.scheduleWarn, scheduleDown);
    const sched = app.appProcs.schedule;
    show(dom.scheduleRestart, scheduleDown && app.inApp && !!sched && !sched.running);
    // The desk coming up or going down is when a problem appears or clears.
    const known = app.deskKnown;
    app.deskKnown = true;
    if (known && deskWas !== isOnline(app.procs.desk)) scheduleAppRefresh();
  }

  function offTitle(p, sup) {
    if (!app.inApp) return `${p.label} is not running. Start it with ${terminal(p.name)}.`;
    if (sup && sup.held) return `${p.label} is waiting: ${sup.reason || "see Settings."}`;
    if (sup && sup.running) return `${p.label} is starting.`;
    return `${p.label} is not running.`;
  }

  function wakeWord() {
    const w = app.snap && app.snap.wake ? app.snap.wake.word : "";
    return String(w || "").replace(/_/g, " ");
  }

  // Not beating, in the app: the supervisor says whether the desk is on its
  // way up ("starting"), waiting on a fix (a problem card says which), or
  // stopped. Outside the app, the terminal command is the only way to start it.
  function renderDesk(info) {
    const online = isOnline(info);
    let state = "offline";
    if (!app.snap && !info) state = "unknown";
    else if (online) state = DESK_STATES.has(info.state) ? info.state : "awake";
    const sup = app.inApp ? app.appProcs.desk || null : null;
    if (state === "offline" && app.inApp && !(sup && sup.held)) {
      if (!app.appLoaded || (sup && sup.running)) state = "starting";
    }
    const held = state === "offline" && !!sup && sup.held;
    setAttr(dom.orb, "data-state", state);
    setAttr(dom.status, "data-desk", held ? "held" : state);
    setText(dom.orbState, DESK_WORDS[held ? "waiting" : state]);
    let ro = "Not running";
    if (online) ro = `${DESK_WORDS[state]} since ${fmtWhen(info.since) || "just now"}`;
    else if (state === "starting") ro = "Starting";
    else if (held) ro = "Waiting for you";
    setText(dom.roDesk, ro);

    const key = `${state}|${held}|${wakeWord()}|${app.inApp}`;
    if (dom.orbNote.dataset.key === key) return;
    dom.orbNote.dataset.key = key;
    setOrbAction(null);
    if (state === "offline" && !app.inApp) {
      dom.orbNote.replaceChildren("The desk is not running — start it with ", el("code", "", terminal("desk")));
      return;
    }
    if (held) {
      setText(dom.orbNote, "I can't start until the problem above is seen to.");
      return;
    }
    if (state === "offline") {
      setText(dom.orbNote, "The desk has stopped.");
      setOrbAction("Restart the desk", (button) => restartProcess("desk", button));
      return;
    }
    const word = wakeWord();
    const notes = {
      unknown: "Reaching the window server…",
      starting: "Starting the desk. One moment.",
      asleep: word ? `Say “${word}” to wake it.` : "Asleep.",
      awake: "Ready. Speak whenever you like.",
      listening: "Hearing you…",
      speaking: "Talk over it to interrupt.",
    };
    setText(dom.orbNote, notes[state] || "");
  }

  function setOrbAction(label, run) {
    app.orbAction = run || null;
    setText(dom.orbAction, label || "");
    show(dom.orbAction, !!label);
  }

  function renderReadouts(s) {
    const presence = s.presence || {};
    const pState = humanize(presence.state || "unknown");
    setText(dom.roPresence, presence.reason ? `${pState} — ${presence.reason}` : pState);

    const wake = s.wake || {};
    const word = wakeWord();
    const threshold = typeof wake.threshold === "number" ? wake.threshold.toFixed(2) : "";
    setText(
      dom.roWake,
      word
        ? `“${word}”${threshold ? ` · threshold ${threshold}` : ""}`
        : "None — the desk is always listening",
    );

    setText(dom.roSpend, (s.spend && s.spend.line) || "Nothing recorded.");

    const n = Array.isArray(s.pending) ? s.pending.length : 0;
    show(dom.attention, n > 0);
    setText(dom.attention, n ? `${plural(n, "question", "questions")} waiting` : "");
  }

  // The route mirrors the server's rule — the desk's speaker when the desk is
  // up, this computer's otherwise — so it changes the moment the desk does,
  // without waiting for the next snapshot.
  function renderVoice() {
    const sp = app.speech || {};
    let line;
    if (!sp.available) line = appText(sp.why, "No voice is available.");
    else if (isOnline(app.procs.desk)) line = "Speaks through the desk";
    else line = "Speaks here, on this computer";
    setText(dom.roVoice, line);
    const can = !!sp.available;
    if (dom.speakReplies.disabled === can) dom.speakReplies.disabled = !can;
    setAttr(dom.feed, "data-speech", can ? "on" : "off");
    updateQuick();
  }

  function renderComposer(chat) {
    const can = chat.available === true;
    app.chatAvailable = can;
    if (dom.chatInput.disabled === can) dom.chatInput.disabled = !can;
    if (!app.chatBusy && dom.chatSend.disabled === can) dom.chatSend.disabled = !can;
    setAttr(dom.chatInput, "placeholder", can ? "Ask Jarvis anything…" : "Chat is unavailable");
    show(dom.chatWhy, !can);
    setText(dom.chatWhy, can ? "" : appText(chat.why, "Chat is not set up yet; see Settings."));
  }

  function setCount(name, n, attention) {
    setText(dom.counts[name], String(n).padStart(2, "0"));
    setAttr(dom.tabs[name], "data-zero", n === 0);
    setAttr(dom.tabs[name], "data-attention", !!attention);
  }

  // ───────────────────────────── the feed ─────────────────────────────

  function feedAtEnd() {
    const s = dom.feedScroll;
    return s.scrollHeight - s.scrollTop - s.clientHeight < 48;
  }

  function scrollFeedToEnd() {
    dom.feedScroll.scrollTop = dom.feedScroll.scrollHeight;
    show(dom.jump, false);
  }

  function makeFeedItem(item, role, text) {
    const li = el("li", `msg msg-${role}`);
    li.dataset.role = role;
    const time = el("time", "msg-time", fmtClock(item.ts));
    if (item.ts) time.dateTime = item.ts;
    if (role === "user" || role === "jarvis") {
      const head = el("div", "msg-head");
      head.append(el("span", "msg-who", role === "user" ? "You" : "Jarvis"), time);
      if (role === "jarvis") {
        const say = el("button", "msg-say");
        say.type = "button";
        say.setAttribute("aria-label", "Say this aloud");
        say.title = "Say this aloud";
        say.append(fromTemplate("tpl-speaker"));
        head.append(say);
      }
      li.append(head, el("p", "msg-text", text.trimStart()));
    } else {
      li.dataset.kind = String(item.kind || "");
      li.append(time, el("span", "msg-text", appText(text, "something needs attention")));
    }
    return li;
  }

  // Items arrive in seq order from the first page, from catch-ups and from the
  // stream, possibly overlapping; the high-water mark makes that harmless.
  function appendFeed(items) {
    if (!Array.isArray(items) || !items.length) return;
    const stick = feedAtEnd();
    let grew = false;
    for (const item of items) {
      if (!item || typeof item.seq !== "number" || item.seq <= app.highSeq) continue;
      app.highSeq = item.seq;
      if (APP_KINDS.has(item.kind)) scheduleAppRefresh();
      const role = ROLES.has(item.role) ? item.role : "system";
      const text = typeof item.text === "string" ? item.text : "";
      const last = dom.feed.lastElementChild;
      // Streamed transcript fragments carry their own spacing; they are joined
      // with nothing and land as one more text node, not a rewrite.
      if (item.merge && last && last.dataset.role === role && (role === "user" || role === "jarvis")) {
        last.querySelector(".msg-text").append(text);
        grew = true;
        continue;
      }
      dom.feed.append(makeFeedItem(item, role, text));
      grew = true;
    }
    while (dom.feed.childElementCount > FEED_CAP) dom.feed.firstElementChild.remove();
    const n = dom.feed.childElementCount;
    show(dom.feedEmpty, n === 0);
    setText(dom.feedMeta, n >= FEED_CAP ? `latest ${FEED_CAP}` : plural(n, "entry", "entries"));
    if (!grew) return;
    if (stick) scrollFeedToEnd();
    else show(dom.jump, true);
  }

  // ───────────────────────────── tools ─────────────────────────────

  function schemaFields(params) {
    const props = params && params.properties && typeof params.properties === "object" ? params.properties : {};
    const required = new Set(params && Array.isArray(params.required) ? params.required : []);
    return Object.entries(props).map(([name, schema]) => ({
      name,
      schema: schema && typeof schema === "object" ? schema : {},
      required: required.has(name),
    }));
  }

  // One field per JSON-schema property. Gemini writes types in capitals and
  // JSON Schema in lower case; both mean the same thing here.
  function buildField({ name, schema, required }) {
    const type = String(schema.type || "STRING").toUpperCase();
    const label = el("label", "field");
    const head = el("span", "field-label", schema.title || humanize(name));
    if (required) head.append(el("span", "req", " *"));
    label.append(head);

    let input;
    if (Array.isArray(schema.enum) && schema.enum.length) {
      input = el("select");
      if (!required) input.append(option("", "Default"));
      for (const v of schema.enum) input.append(option(String(v), String(v)));
    } else if (type === "BOOLEAN") {
      input = el("select");
      input.append(option("", required ? "Choose" : "Default"), option("true", "Yes"), option("false", "No"));
    } else if (type === "OBJECT") {
      input = el("textarea");
      input.rows = 3;
      input.placeholder = '{"key": "value"}';
    } else {
      input = el("input");
      input.type = type === "NUMBER" || type === "INTEGER" ? "number" : "text";
      if (type === "INTEGER") input.step = "1";
      if (type === "NUMBER") input.step = "any";
      if (type === "ARRAY") input.placeholder = "comma-separated";
      if (schema.example) input.placeholder = String(schema.example);
    }
    input.name = name;
    input.required = required;
    input.dataset.type = type;
    if (type === "ARRAY") {
      input.dataset.items = String((schema.items && schema.items.type) || "STRING").toUpperCase();
    }
    label.append(input);
    if (schema.description) label.append(el("span", "field-hint", schema.description));
    return label;
  }

  function convert(raw, type, items) {
    switch (type) {
      case "INTEGER": {
        const n = Number(raw);
        return Number.isInteger(n) ? n : undefined;
      }
      case "NUMBER": {
        const n = Number(raw);
        return Number.isFinite(n) ? n : undefined;
      }
      case "BOOLEAN":
        return raw === "true";
      case "ARRAY": {
        const out = [];
        for (const part of raw.split(",")) {
          const p = part.trim();
          if (!p) continue;
          const v = convert(p, items || "STRING");
          if (v === undefined) return undefined;
          out.push(v);
        }
        return out;
      }
      case "OBJECT": {
        const v = parseJson(raw);
        return v && typeof v === "object" && !Array.isArray(v) ? v : undefined;
      }
      default:
        return raw;
    }
  }

  // Empty fields are left out, so the tool's own default applies — a blank
  // "place" means "here", not "the empty string".
  function collectArgs(form) {
    const args = {};
    for (const input of form.querySelectorAll("input[name], select[name], textarea[name]")) {
      const raw = input.value.trim();
      if (raw === "") continue;
      const type = input.dataset.type || "STRING";
      const value = convert(raw, type, input.dataset.items);
      if (value === undefined) {
        const want = { INTEGER: "a whole number", NUMBER: "a number", OBJECT: "a JSON object" };
        toast(`${humanize(input.name)} needs ${want[type] || "a list"}.`, { tone: "warn", head: "Check the form" });
        input.focus();
        return null;
      }
      args[input.name] = value;
    }
    return args;
  }

  function buildSchemaForm(params, submitLabel) {
    const form = el("form", "form");
    form.autocomplete = "off";
    for (const f of schemaFields(params)) form.append(buildField(f));
    if (!form.childElementCount) form.append(el("p", "field-hint", "Nothing to fill in — it runs as it is."));
    const button = el("button", "btn btn-primary", submitLabel);
    button.type = "submit";
    form.append(button);
    return form;
  }

  function buildToolBody(tool) {
    const body = el("div", "tool-body");
    if (tool.description) body.append(el("p", "tool-full", tool.description));
    const form = buildSchemaForm(tool.parameters, `Run ${tool.name}`);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const args = collectArgs(form);
      if (!args) return;
      await withBusy(form.querySelector('button[type="submit"]'), () => runTool(tool.name, args));
    });
    body.append(form);
    return body;
  }

  // Forms are built the first time a tool is opened: eighteen forms nobody
  // looked at are eighteen forms the snapshot would otherwise have to diff.
  function makeToolEntry() {
    const details = el("details", "tool");
    const summary = el("summary");
    summary.append(el("span", "tool-name"), el("span", "tool-desc"));
    details.append(summary);
    details.addEventListener("toggle", () => {
      if (details.open) ensureToolBody(details);
    });
    return details;
  }

  function ensureToolBody(details) {
    if (details.querySelector(".tool-body")) return;
    const tool = app.tools.get(details.dataset.key);
    if (tool) details.append(buildToolBody(tool));
  }

  function patchToolEntry(details, tool) {
    setText(details.querySelector(".tool-name"), tool.name);
    setText(details.querySelector(".tool-desc"), firstSentence(tool.description));
    const sig = JSON.stringify([tool.description, tool.parameters]);
    if (details.dataset.sig === sig) return;
    details.dataset.sig = sig;
    const body = details.querySelector(".tool-body");
    if (body) {
      body.remove();
      if (details.open) details.append(buildToolBody(tool));
    }
  }

  function renderTools(tools) {
    const sig = JSON.stringify(tools.map((t) => [t.name, t.description, t.parameters]));
    if (sig === app.toolsSig) return;
    app.toolsSig = sig;
    app.tools = new Map(tools.map((t) => [t.name, t]));
    syncList(dom.toolList, tools, (t) => t.name, makeToolEntry, patchToolEntry);
    show(dom.toolsEmpty, tools.length === 0);
    setCount("tools", tools.length, false);
    updateQuick();
  }

  function updateQuick() {
    for (const button of dom.quickButtons) {
      const q = QUICK[button.dataset.quick];
      const ok = q.say ? !!(app.speech && app.speech.available) : app.tools.has(q.tool);
      if (button.disabled === ok && button.getAttribute("aria-busy") !== "true") button.disabled = !ok;
      setAttr(
        button,
        "title",
        ok ? "" : q.say ? appText(app.speech.why, "No voice is available.") : `${q.tool} is not offered to this window.`,
      );
    }
  }

  async function runTool(name, args) {
    const data = await api(API.tool, { name, args });
    const said = appText(typeof data.said === "string" ? data.said : "", "Done.");
    setText(dom.resultHead, `${name} · ${clockFmt.format(new Date())}`);
    setText(dom.resultText, said);
    show(dom.result, true);
    toast(said, { head: name });
    scheduleRefresh();
    return said;
  }

  async function sayAloud(text) {
    const data = await api(API.say, { text });
    toast(data.via === "desk" ? "Saying it through the desk." : "Saying it here, on this computer.", {
      head: "Say aloud",
    });
    return data.via || "local";
  }

  // Disables the button for the life of the call and turns any failure into a
  // toast. Resolves to the call's result, or undefined when it failed.
  async function withBusy(button, fn) {
    const wasDisabled = button ? button.disabled : false;
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    try {
      return await fn();
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        toast(appText(err && err.message, "That did not work."), { tone: "bad", head: "Failed" });
      }
      return undefined;
    } finally {
      if (button) {
        button.removeAttribute("aria-busy");
        button.disabled = wasDisabled;
      }
    }
  }

  function closeQuickForm() {
    dom.quickForm.hidden = true;
    dom.quickForm.replaceChildren();
    delete dom.quickForm.dataset.quick;
    for (const b of dom.quickButtons) if (b.hasAttribute("aria-expanded")) b.setAttribute("aria-expanded", "false");
  }

  function openQuickForm(button) {
    const q = QUICK[button.dataset.quick];
    const wasOpen = button.getAttribute("aria-expanded") === "true";
    closeQuickForm();
    if (wasOpen) return;
    button.setAttribute("aria-expanded", "true");
    const form = dom.quickForm;
    for (const f of schemaFields({ properties: q.fields, required: q.required || [] })) form.append(buildField(f));
    const submit = el("button", "btn btn-primary", q.submit);
    submit.type = "submit";
    form.append(submit);
    form.dataset.quick = button.dataset.quick;
    form.hidden = false;
    const first = form.querySelector("input, select, textarea");
    if (first) first.focus();
  }

  async function submitQuickForm(e) {
    e.preventDefault();
    const q = QUICK[dom.quickForm.dataset.quick];
    if (!q) return;
    const args = collectArgs(dom.quickForm);
    if (!args) return;
    const button = dom.quickForm.querySelector('button[type="submit"]');
    const done = await withBusy(button, () => (q.say ? sayAloud(args.text || "") : runTool(q.tool, args)));
    if (done !== undefined) dom.quickForm.reset();
  }

  function onQuickClick(e) {
    const button = e.target.closest("button[data-quick]");
    if (!button || button.disabled) return;
    const q = QUICK[button.dataset.quick];
    if (q.fields) openQuickForm(button);
    else withBusy(button, () => runTool(q.tool, { ...q.args }));
  }

  function revealTool(name) {
    selectTab("tools", false);
    const details = Array.from(dom.toolList.children).find((d) => d.dataset.key === name);
    if (!details) return;
    ensureToolBody(details);
    details.open = true;
    details.scrollIntoView({ block: "nearest" });
    const target = details.querySelector(".tool-body input, .tool-body select, .tool-body textarea, .tool-body button");
    if (target) target.focus();
  }

  // ───────────────────────────── lists ─────────────────────────────

  function makeRow(actionText) {
    return () => {
      const li = el("li", "row");
      li.append(el("span", "row-main"), el("span", "row-sub"));
      if (actionText) {
        const button = el("button", "btn btn-quiet btn-danger row-action", actionText);
        button.type = "button";
        li.append(button);
      }
      return li;
    };
  }

  function patchRow(li, main, sub, actionLabel) {
    setText(li.children[0], main);
    setText(li.children[1], sub);
    const action = li.querySelector(".row-action");
    if (action && actionLabel) setAttr(action, "aria-label", actionLabel);
  }

  function renderReminders(list) {
    syncList(dom.reminderList, list, (r) => r.id, makeRow("Cancel"), (li, r) =>
      patchRow(li, r.text, r.due_local || fmtWhen(r.due_at), `Cancel the reminder to ${r.text}`),
    );
    show(dom.remindersEmpty, list.length === 0);
    setCount("reminders", list.length, false);
  }

  function renderNotes(list) {
    syncList(dom.noteList, list, (n) => n.id, makeRow("Forget"), (li, n) =>
      patchRow(li, n.text, `noted ${fmtWhen(n.created_at)}`, `Forget: ${n.text}`),
    );
    show(dom.notesEmpty, list.length === 0);
    setCount("notes", list.length, false);
  }

  function makeJobRow() {
    const li = el("li", "row");
    li.append(el("span", "row-main"), el("span", "row-sub"), el("span", "pill row-action"));
    return li;
  }

  function patchJobRow(li, job) {
    setText(li.children[0], job.title || job.id);
    const kind = humanize(job.kind || "job");
    setText(li.children[1], `${kind} · updated ${fmtWhen(job.updated_at)}`);
    const pill = li.children[2];
    setText(pill, job.state || "unknown");
    setAttr(pill, "data-tone", JOB_TONES[job.state] || "live");
  }

  function renderBuilds(jobs, lines) {
    syncList(dom.jobList, jobs, (j) => j.id, makeJobRow, patchJobRow);
    show(dom.jobsEmpty, jobs.length === 0);
    const keyed = lines.map((text, i) => ({ text: String(text), i }));
    syncList(dom.projectLines, keyed, (x) => `${x.i}:${x.text}`, () => el("li"), (li, x) => setText(li, x.text));
    show(dom.projectsEmpty, keyed.length === 0);
    setCount("builds", jobs.length, jobs.some((j) => JOB_TONES[j.state] === "warn"));
  }

  function makeHearingRow() {
    const li = el("li", "row");
    li.append(el("span", "row-main"), el("div", "heard"), el("span", "pill row-action"));
    return li;
  }

  function patchHearingRow(li, h) {
    setText(li.children[0], h.term);
    const heard = Array.isArray(h.heard_as) ? h.heard_as.map(String) : [];
    const box = li.children[1];
    const sig = heard.join("\u0000");
    if (box.dataset.sig !== sig) {
      box.dataset.sig = sig;
      box.replaceChildren(...heard.map((w) => el("span", "", w)));
      box.setAttribute("aria-label", heard.length ? `heard as ${heard.join(", ")}` : "");
    }
    const pill = li.children[2];
    setText(pill, h.taught ? "taught" : "built in");
    setAttr(pill, "data-tone", h.taught ? "live" : "done");
  }

  function renderHearing(list) {
    syncList(dom.hearingList, list, (h) => h.term, makeHearingRow, patchHearingRow);
    show(dom.hearingEmpty, list.length === 0);
    setCount("hearing", list.length, false);
  }

  // ───────────────────────────── questions ─────────────────────────────

  function makeQuestion(q) {
    const card = el("article", "q");
    card.dataset.multi = q.multi ? "1" : "0";
    const label = [KIND_WORDS[q.kind] || humanize(q.kind || "question"), q.short_label].filter(Boolean);
    card.append(el("p", "q-eyebrow", label.join(" · ")));
    if (q.intro) card.append(el("p", "q-intro", q.intro));
    card.append(el("h4", "q-question", q.question || "Jarvis needs an answer."));

    const options = Array.isArray(q.options) ? q.options : [];
    if (options.length) {
      const list = el("ol", "q-options");
      list.setAttribute("aria-label", q.multi ? "Options — pick one or more" : "Options — pick one");
      for (const o of options) {
        const item = el("li");
        const button = el("button", "q-opt");
        button.type = "button";
        button.dataset.index = String(o.index);
        button.setAttribute("aria-pressed", "false");
        button.append(el("span", "q-num", o.index), el("span", "q-label", o.label || ""));
        if (o.description) button.append(el("span", "q-desc", o.description));
        item.append(button);
        list.append(item);
      }
      card.append(list);
    }

    if (q.allows_free_text) {
      const free = el("textarea", "q-free");
      free.rows = 2;
      free.placeholder = q.free_text_prompt || (options.length ? "Or answer in your own words" : "Your answer");
      free.setAttribute("aria-label", q.free_text_prompt || "Your own answer");
      card.append(free);
    }

    const foot = el("div", "q-foot");
    const meta = [`asked ${fmtWhen(q.created_at)}`];
    if (q.expires_at) meta.push(`expires ${fmtWhen(q.expires_at)}`);
    const send = el("button", "btn btn-primary q-send", "Send answer");
    send.type = "button";
    foot.append(el("span", "q-meta", meta.join(" · ")), send);
    card.append(foot);
    return card;
  }

  function renderQuestions(list) {
    // A question never changes once asked, so there is nothing to patch; the
    // keyed sync only adds new ones and drops answered ones, which keeps a
    // half-made choice intact while the snapshot refreshes underneath it.
    syncList(dom.questionList, list, (q) => q.id, makeQuestion, () => {});
    show(dom.questionsEmpty, list.length === 0);
    setCount("questions", list.length, list.length > 0);
  }

  function onQuestionClick(e) {
    const opt = e.target.closest(".q-opt");
    if (opt) {
      const card = opt.closest(".q");
      const pressed = opt.getAttribute("aria-pressed") === "true";
      if (card.dataset.multi !== "1") {
        for (const o of card.querySelectorAll(".q-opt")) setAttr(o, "aria-pressed", "false");
      }
      setAttr(opt, "aria-pressed", !pressed);
      return;
    }
    const send = e.target.closest(".q-send");
    if (send) submitAnswer(send.closest(".q"), send);
  }

  async function submitAnswer(card, button) {
    const picks = Array.from(card.querySelectorAll('.q-opt[aria-pressed="true"]')).map((b) =>
      Number(b.dataset.index),
    );
    const free = card.querySelector(".q-free");
    const text = free ? free.value.trim() : "";
    if (!picks.length && !text) {
      toast("Pick an option or write an answer first.", { tone: "warn", head: "Question" });
      return;
    }
    const data = await withBusy(button, () =>
      api(API.answer, { request_id: card.dataset.key, picks, text: text || null }),
    );
    if (!data) return;
    toast(data.message || (data.answered ? "Answered." : "Already answered."), {
      tone: data.answered ? "ok" : "warn",
      head: "Question",
    });
    scheduleRefresh();
  }

  // ───────────────────────────── tabs ─────────────────────────────

  function selectTab(name, focus) {
    if (!TABS.includes(name)) return;
    for (const tabName of TABS) {
      const tab = dom.tabs[tabName];
      const on = tabName === name;
      setAttr(tab, "aria-selected", on);
      tab.tabIndex = on ? 0 : -1;
      show(dom.panels[tabName], on);
    }
    if (focus) dom.tabs[name].focus();
    writeStore("localStorage", TAB_KEY, name);
    // Settings read the machine (microphones, the keyring), so only on demand.
    if (name === "settings" && app.token && !app.locked) loadSetup().catch(() => {});
  }

  function onTabKey(e) {
    const current = TABS.indexOf(e.target.dataset.tab);
    if (current < 0) return;
    const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: TAB_ROW, ArrowUp: -TAB_ROW }[e.key];
    let next = null;
    if (step !== undefined) next = (current + step + TABS.length) % TABS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = TABS.length - 1;
    if (next === null) return;
    e.preventDefault();
    selectTab(TABS[next], true);
  }

  // ───────────────────────────── chat ─────────────────────────────

  async function submitChat(e) {
    e.preventDefault();
    const text = dom.chatInput.value.trim();
    if (!text || app.chatBusy || !app.chatAvailable) return;
    app.chatBusy = true;
    dom.chatInput.value = "";
    dom.chatSend.disabled = true;
    show(dom.thinking, true);
    scrollFeedToEnd();
    try {
      const speak = dom.speakReplies.checked && !dom.speakReplies.disabled;
      await api(API.chat, { text, speak });
      // The reply is already in the log as window.reply. The stream will bring
      // it; this read makes sure it arrives even while the stream is down.
      if (!streamIsLive()) await catchUpFeed().catch(() => {});
    } catch (err) {
      if (!dom.chatInput.value) dom.chatInput.value = text;
      if (!(err instanceof ApiError && err.status === 401)) {
        toast(err.message, { tone: "bad", head: "Chat" });
      }
    } finally {
      app.chatBusy = false;
      dom.chatSend.disabled = !app.chatAvailable;
      show(dom.thinking, false);
      if (!app.locked) dom.chatInput.focus();
    }
  }

  // ───────────────────────────── STOP ─────────────────────────────

  // Hold, not click: STOP kills every job and call, and Quit ends Jarvis, and a
  // stray click must be able to do neither. Pointer and keyboard both.
  // `tooShort` is a sentence for a toast, or a function that says it nearer
  // the button (a toast would land on top of a button low on the screen).
  function armHold(button, fire, { head, tooShort }) {
    let timer = 0;
    let started = 0;

    const done = () => {
      timer = 0;
      button.classList.remove("is-holding");
      fire();
    };
    const start = () => {
      if (timer || button.disabled || app.locked) return;
      started = performance.now();
      button.classList.add("is-holding");
      timer = setTimeout(done, HOLD_MS);
    };
    const cancel = () => {
      if (!timer) return;
      clearTimeout(timer);
      timer = 0;
      button.classList.remove("is-holding");
      if (performance.now() - started >= HOLD_MS * 0.6) return;
      if (typeof tooShort === "function") tooShort();
      else toast(tooShort, { tone: "warn", head });
    };

    // No pointer capture: a captured pointer never "leaves", and sliding off
    // the button is how a person changes their mind halfway through a hold.
    button.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      start();
    });
    button.addEventListener("pointerup", cancel);
    button.addEventListener("pointercancel", cancel);
    button.addEventListener("pointerleave", cancel);
    button.addEventListener("keydown", (e) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) {
        e.preventDefault();
        start();
      }
    });
    button.addEventListener("keyup", (e) => {
      if (e.key === " " || e.key === "Enter") cancel();
    });
    button.addEventListener("blur", cancel);
    button.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  function armStop() {
    armHold(
      dom.stop,
      async () => {
        const data = await withBusy(dom.stop, () => api(API.stop, {}));
        if (!data) return;
        toast(data.message || "Stopped everything.", { tone: "warn", head: "Stop" });
        scheduleRefresh();
      },
      { head: "Stop", tooShort: "Hold Stop for a full second to stop everything." },
    );
  }

  function armQuit() {
    armHold(
      dom.quit,
      async () => {
        const data = await withBusy(dom.quit, () => api(API.appQuit, {}));
        if (data) farewell();
      },
      { head: "Quit", tooShort: () => nudge(dom.quit.querySelector(".hold-hint"), "keep holding", "hold 1 s") },
    );
  }

  // Says something in place for a moment, then puts the old words back.
  function nudge(node, said, after) {
    setText(node, said);
    node.classList.add("is-nudged");
    clearTimeout(node.nudgeTimer);
    node.nudgeTimer = setTimeout(() => {
      setText(node, after);
      node.classList.remove("is-nudged");
    }, 1800);
  }

  // ───────────────────────────── palette ─────────────────────────────

  function paletteEntries() {
    const entries = [];
    for (const t of app.tools.values()) {
      entries.push({ kind: "tool", label: t.name, hint: firstSentence(t.description), run: () => revealTool(t.name) });
    }
    entries.push({
      kind: "action",
      label: "Say aloud…",
      hint: "Type something for Jarvis to say out loud",
      run: () => openQuickByName("say"),
    });
    entries.push({ kind: "action", label: "Message Jarvis", hint: "Jump to the chat box", run: () => dom.chatInput.focus() });
    if (app.setup) {
      entries.push({
        kind: "action",
        label: "Run first-time setup",
        hint: "The Gemini key, the microphone, how I address you",
        run: () => openOnboarding("key"),
      });
    }
    for (const name of TABS) {
      entries.push({ kind: "section", label: `Open ${TAB_TITLES[name]}`, hint: "", run: () => selectTab(name, true) });
    }
    return entries;
  }

  function openQuickByName(key) {
    selectTab("tools", false);
    const button = dom.quickButtons.find((b) => b.dataset.quick === key);
    if (!button || button.disabled) return;
    if (button.getAttribute("aria-expanded") !== "true") openQuickForm(button);
    else dom.quickForm.querySelector("input, textarea")?.focus();
  }

  function openPalette() {
    if (app.locked || dom.palette.open) return;
    app.palette.all = paletteEntries();
    dom.paletteInput.value = "";
    filterPalette();
    dom.palette.showModal();
    dom.paletteInput.focus();
  }

  function closePalette() {
    if (dom.palette.open) dom.palette.close();
  }

  function filterPalette() {
    const q = dom.paletteInput.value.trim().toLowerCase();
    const scored = [];
    for (const entry of app.palette.all) {
      if (!q) {
        scored.push([0, entry]);
        continue;
      }
      const at = entry.label.toLowerCase().indexOf(q);
      if (at === 0) scored.push([0, entry]);
      else if (at > 0) scored.push([1, entry]);
      else if (entry.hint.toLowerCase().includes(q)) scored.push([2, entry]);
    }
    scored.sort((a, b) => a[0] - b[0]);
    app.palette.shown = scored.map((s) => s[1]);
    app.palette.active = 0;
    const items = app.palette.shown.map((entry, i) => {
      const li = el("li", "palette-item");
      li.id = `palette-opt-${i}`;
      li.setAttribute("role", "option");
      li.dataset.i = String(i);
      li.append(el("span", "palette-label", entry.label), el("span", "palette-kind", entry.kind));
      if (entry.hint) li.append(el("span", "palette-hint-text", entry.hint));
      return li;
    });
    if (!items.length) {
      const none = el("li", "palette-item", "Nothing matches. Try a tool name, like weather.");
      none.setAttribute("aria-disabled", "true");
      items.push(none);
    }
    dom.paletteList.replaceChildren(...items);
    markPaletteActive();
  }

  function markPaletteActive() {
    const items = dom.paletteList.querySelectorAll('[role="option"]');
    items.forEach((li, i) => setAttr(li, "aria-selected", i === app.palette.active));
    const active = items[app.palette.active];
    if (active) {
      dom.paletteInput.setAttribute("aria-activedescendant", active.id);
      active.scrollIntoView({ block: "nearest" });
    } else {
      dom.paletteInput.removeAttribute("aria-activedescendant");
    }
  }

  function runPaletteEntry(i) {
    const entry = app.palette.shown[i];
    if (!entry) return;
    closePalette();
    entry.run();
  }

  function onPaletteKey(e) {
    const n = app.palette.shown.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!n) return;
      app.palette.active = (app.palette.active + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
      markPaletteActive();
    } else if (e.key === "Enter") {
      e.preventDefault();
      runPaletteEntry(app.palette.active);
    }
  }

  // ───────────────────────────── toasts ─────────────────────────────

  function toast(text, { tone = "ok", head = "" } = {}) {
    const t = el("div", "toast");
    t.dataset.tone = tone;
    if (head) t.append(el("span", "toast-head", head));
    t.append(document.createTextNode(String(text)));
    dom.toasts.append(t);
    while (dom.toasts.childElementCount > TOAST_CAP) dom.toasts.firstElementChild.remove();
    requestAnimationFrame(() => t.classList.add("is-in"));
    const dismiss = () => {
      t.classList.remove("is-in");
      setTimeout(() => t.remove(), 260);
    };
    t.addEventListener("click", dismiss);
    setTimeout(dismiss, tone === "bad" ? 9000 : 6000);
  }

  // ───────────────────────────── the app ─────────────────────────────

  function setInApp(on) {
    if (app.inApp === on && document.documentElement.dataset.app !== "unknown") return;
    app.inApp = on;
    // CSS hides every [data-terminal] hint while this says "true".
    document.documentElement.dataset.app = on ? "true" : "false";
    show(dom.quit, on);
    // Text already on screen was written for the other mode; redraw it.
    dom.orbNote.dataset.key = "";
    if (on && !app.appLoaded) scheduleAppRefresh();
  }

  // The supervisor's view and what is wrong, read together and debounced: a
  // refusal arrives as two events (desk.refused, app.process_exited) a few
  // milliseconds apart, and one read answers both.
  function scheduleAppRefresh() {
    if (app.appTimer || app.locked) return;
    app.appTimer = setTimeout(() => {
      app.appTimer = 0;
      loadApp()
        .then(() => (app.setupAvailable === false ? null : loadSetup()))
        .catch(() => {});
    }, APP_REFRESH_MS);
  }

  async function loadApp() {
    if (!app.inApp) return;
    let data;
    try {
      data = await api(API.appStatus);
    } catch (err) {
      if (err instanceof ApiError && err.status === 503) setInApp(false);
      return;
    }
    app.appProcs = data.processes && typeof data.processes === "object" ? data.processes : {};
    app.appLoaded = true;
    applyProcesses(app.procs);
    renderProcesses();
  }

  async function loadSetup({ rescan = false } = {}) {
    let data;
    try {
      data = await api(rescan ? `${API.setup}?rescan=1` : API.setup);
    } catch (err) {
      // 503: this window runs without the app. 404: a server older than this page.
      if (err instanceof ApiError && (err.status === 503 || err.status === 404)) {
        app.setupAvailable = false;
        app.setup = null;
        renderSetup();
      }
      return null;
    }
    app.setupAvailable = true;
    app.setup = data;
    renderSetup();
    return data;
  }

  async function restartProcess(name, button) {
    const data = await withBusy(button, () => api(API.appRestart, { process: name }));
    if (!data) return;
    toast(data.message || "Restarting.", { head: "Jarvis" });
    await loadApp();
    scheduleAppRefresh();
  }

  function procLabel(name) {
    const p = PROCS.find((x) => x.name === name);
    return p ? p.label : humanize(name);
  }

  function procThe(name) {
    const p = PROCS.find((x) => x.name === name);
    return p ? p.the : humanize(name);
  }

  function renderSetup() {
    const s = app.setup;
    show(dom.settingsOff, app.setupAvailable === false);
    show(dom.settingsBody, !!s);
    const problems = s && Array.isArray(s.problems) ? s.problems : [];
    renderProblems(problems);
    setAttr(dom.tabs.settings, "data-attention", problems.length > 0);
    if (!s) return;
    renderSettings(s);
    if (app.ob.open) renderOnboarding();
  }

  // ───────────────────────────── problems ─────────────────────────────

  // Each problem's button does the fixing; the sentence never says how.
  function fixFor(action) {
    const a = String(action || "");
    if (a === "secret:gemini_api_key") return "Add the Gemini key";
    if (a.startsWith("secret:")) return `Add the ${(SECRET_INFO[a.slice(7)] || { label: "key" }).label}`;
    if (a === "wake") return "Download the wake model";
    if (a === "device") return "Choose a microphone";
    if (a === "voice") return "Voice settings";
    if (a.startsWith("restart:")) return `Restart ${procThe(a.slice(8))}`;
    return "Open settings";
  }

  function makeProblem() {
    const card = el("article", "problem");
    const fix = el("button", "btn problem-fix");
    fix.type = "button";
    card.append(el("p", "problem-head"), el("p", "problem-text"), fix);
    return card;
  }

  function patchProblem(card, p) {
    setText(card.children[0], `${procLabel(p.process)} · needs you`);
    setText(card.children[1], appText(p.sentence, "Something needs your attention."));
    setText(card.children[2], fixFor(p.action));
    card.children[2].dataset.action = p.action || "";
  }

  function renderProblems(list) {
    const shown = list.slice(0, 3);
    syncList(dom.problems, shown, (p) => `${p.process}|${p.action}|${p.sentence}`, makeProblem, patchProblem);
    setAttr(dom.status, "data-problems", shown.length > 0);
  }

  function runFix(action, button) {
    const a = String(action || "");
    if (a.startsWith("secret:")) focusSecret(a.slice(7));
    else if (a === "wake") downloadWake(button);
    else if (a === "device") openSettings("set-mic");
    else if (a === "voice") openSettings("set-gemini-voice");
    else if (a.startsWith("restart:")) restartProcess(a.slice(8), button);
    else openSettings();
  }

  function openSettings(focusId) {
    selectTab("settings", !focusId);
    if (!focusId) return;
    // The settings arrive with the read selectTab starts; focus once they have.
    const target = byId(focusId);
    if (target) {
      target.scrollIntoView({ block: "center" });
      target.focus();
    }
  }

  function focusSecret(name) {
    if (name === "gemini_api_key" && app.setup && !app.setup.secrets.gemini_api_key) {
      openOnboarding("key");
      return;
    }
    selectTab("settings", false);
    const row = Array.from(dom.secretList.children).find((r) => r.dataset.key === name);
    if (!row) return;
    openSecretForm(row);
    row.scrollIntoView({ block: "center" });
  }

  async function downloadWake(button) {
    const data = await withBusy(button, () => api(API.setupWake, {}));
    if (!data) return;
    toast(data.message || "The wake-word model is in place.", { head: "Wake word" });
    scheduleAppRefresh();
  }

  // ───────────────────────────── settings ─────────────────────────────

  function asPairs(options) {
    return options.map((o) => (Array.isArray(o) ? o : [String(o), String(o)]));
  }

  // Rebuilt only when the options change, and never under the user's hand.
  function fillSelect(select, options, value) {
    const pairs = asPairs(options);
    const want = value === undefined || value === null ? "" : String(value);
    if (!pairs.some(([v]) => v === want)) pairs.unshift([want, want || "—"]);
    const sig = JSON.stringify(pairs);
    if (select.dataset.sig !== sig) {
      select.dataset.sig = sig;
      select.replaceChildren(...pairs.map(([v, label]) => option(v, label)));
    }
    if (document.activeElement !== select && select.value !== want) select.value = want;
  }

  function setField(input, value) {
    if (document.activeElement === input) return;
    if (input.type === "checkbox") {
      if (input.checked !== !!value) input.checked = !!value;
      return;
    }
    const v = Array.isArray(value)
      ? value.join(input.tagName === "TEXTAREA" ? "\n" : ", ")
      : value === undefined || value === null
        ? ""
        : String(value);
    if (input.value !== v) input.value = v;
  }

  function wakeLabel(word) {
    if (!word) return "None — always listening";
    const said = String(word).replace(/_/g, " ").replace(/\bjarvis\b/i, "Jarvis");
    return `“${said.charAt(0).toUpperCase()}${said.slice(1)}”`;
  }

  const REGIONS = { GB: "British", US: "American", IE: "Irish", AU: "Australian", CA: "Canadian", IN: "Indian", TR: "Turkish" };

  // "en-GB-RyanNeural" -> "Ryan · British". Anything else is shown as it is.
  function readerLabel(name) {
    const m = /^([a-z]{2})-([A-Z]{2})-([A-Za-z]+?)(?:Multilingual)?Neural$/.exec(String(name));
    if (!m) return String(name);
    return `${m[3]} · ${REGIONS[m[2]] || `${m[1]}-${m[2]}`}`;
  }

  function makeSegment(value, label) {
    const b = el("button", "seg", label);
    b.type = "button";
    b.setAttribute("role", "radio");
    b.dataset.value = value;
    return b;
  }

  function fillSegments(box, pairs, value) {
    const sig = JSON.stringify(pairs);
    if (box.dataset.sig !== sig) {
      box.dataset.sig = sig;
      box.replaceChildren(...pairs.map(([v, label]) => makeSegment(v, label)));
    }
    for (const b of box.children) setAttr(b, "aria-checked", b.dataset.value === value);
  }

  function addressPairs(choices) {
    return choices.map((a) => [a, a.charAt(0).toUpperCase() + a.slice(1)]);
  }

  function renderSettings(s) {
    const v = s.settings || {};
    const c = s.choices || {};
    const can = s.can || {};
    fillSelect(dom.setGeminiVoice, c["voice.gemini_voice"] || [], v["voice.gemini_voice"]);
    fillSelect(
      dom.setReaderVoice,
      (c["voice.reader_voice"] || []).map((n) => [n, readerLabel(n)]),
      v["voice.reader_voice"],
    );
    const devices = Array.isArray(s.devices) ? s.devices : [];
    const mic = String(v["voice.input_device"] || "");
    const mics = [["", "System default"], ...devices.map((d) => [d.label, d.label])];
    if (mic && !devices.some((d) => d.label === mic)) mics.push([mic, `${mic} (not connected)`]);
    fillSelect(dom.setMic, mics, mic);
    setText(
      dom.setMicHint,
      s.devices_why
        ? appText(s.devices_why, "I couldn't list the microphones.")
        : "Only devices that can both listen and speak are listed. A headset is ideal.",
    );
    const words = Array.isArray(c["voice.wake_word"]) ? c["voice.wake_word"] : ["hey_jarvis", ""];
    fillSelect(dom.setWakeWord, words.map((w) => [w, wakeLabel(w)]), v["voice.wake_word"]);
    const wake = s.wake || {};
    setText(dom.wakePill, !wake.word ? "not needed" : wake.ready ? "model ready" : "model missing");
    setAttr(dom.wakePill, "data-tone", !wake.word || wake.ready ? "done" : "warn");
    show(dom.wakeDownload, !!wake.word && !wake.ready);
    setText(dom.wakeLicence, wake.word ? wake.licence || "" : "");
    setField(dom.setWakeThreshold, v["voice.wake_threshold"]);
    setText(dom.setWakeOut, Number(dom.setWakeThreshold.value).toFixed(2));

    const addresses = Array.isArray(c["persona.address"]) ? c["persona.address"] : ["sir", "ma'am", "boss"];
    const address = String(v["persona.address"] || "");
    fillSegments(dom.setAddress, addressPairs(addresses.filter((a) => ["sir", "ma'am", "boss"].includes(a))), address);
    setField(dom.setAddressOther, ["sir", "ma'am", "boss"].includes(address) ? "" : address);
    setField(dom.setName, v["persona.name"]);
    setField(dom.setCity, v["location.city"]);
    fillSelect(dom.setUnits, [["metric", "Metric (°C)"], ["imperial", "Imperial (°F)"]], v["location.units"]);
    setField(dom.setTz, v.tz);
    setField(dom.setVocab, v["voice.vocabulary"]);
    setField(dom.setLangs, v["voice.languages"]);
    setField(dom.setAutostart, v["app.start_with_windows"]);
    setField(dom.setTelegram, v["app.start_telegram"]);
    setField(dom.setOpenWindow, v["app.open_window_on_start"]);
    if (dom.setAutostart.disabled === !!can.autostart) dom.setAutostart.disabled = !can.autostart;
    setAttr(
      dom.setAutostart.closest("label"),
      "title",
      can.autostart ? "" : "Starting with Windows isn't available on this computer.",
    );
    if (dom.previewVoice.disabled === !!can.preview) dom.previewVoice.disabled = !can.preview;
    if (dom.claudeSignin.disabled === !!can.claude) dom.claudeSignin.disabled = !can.claude;
    setAttr(dom.previewVoice, "title", can.preview ? "Play a sample" : "Samples need the Gemini key");
    renderSecrets(s.secrets || {});
    renderProcesses();
    fillTimeZones();
  }

  // The browser already knows every zone name; the list costs no request.
  function fillTimeZones() {
    if (app.tzFilled) return;
    app.tzFilled = true;
    let zones = [];
    try {
      zones = Intl.supportedValuesOf("timeZone");
    } catch {
      zones = [];
    }
    dom.tzList.replaceChildren(...zones.map((z) => option(z, z)));
  }

  function readField(input) {
    const kind = input.dataset.kind;
    if (kind === "bool") return input.checked;
    if (kind === "number") return Number(input.value);
    if (kind === "list") {
      return input.value
        .split(/[\n,]/)
        .map((x) => x.trim())
        .filter(Boolean);
    }
    return input.value.trim();
  }

  function savedLine(data) {
    if (data.note) return `Saved. ${data.note}`;
    const r = Array.isArray(data.restarted) ? data.restarted : [];
    if (r.length) return `Saved. Restarting ${r.map(procThe).join(" and ")} to use it.`;
    return "Saved.";
  }

  // A plain save is marked on its row; a toast only when something else
  // happens because of it (a restart, a "next time").
  async function saveSetting(key, value, { restart = true, quiet = false } = {}) {
    const data = await api(API.setupSetting, { key, value, restart });
    // Onboarding saves without restarting and restarts once at the end; a
    // dialog closed before the end must still restart, or nothing is used.
    if (!restart) app.ob.unapplied = true;
    const line = savedLine(data);
    if (!quiet && line !== "Saved.") toast(line, { head: "Settings" });
    return data;
  }

  async function saveField(input) {
    const key = input.dataset.key;
    if (!key) return;
    const value = readField(input);
    if (key === "persona.address" && !value) return; // the segments hold the choice
    const row = input.closest(".set-row, .set-switch");
    input.setAttribute("aria-busy", "true");
    try {
      await saveSetting(key, value);
      if (row) flash(row);
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        toast(appText(err.message, "That was not saved."), { tone: "bad", head: "Not saved" });
      }
    } finally {
      input.removeAttribute("aria-busy");
    }
    // Re-read either way: on success it shows what was stored, on failure it
    // puts back what is really there.
    await loadSetup();
  }

  // A brief mark on the row that changed, by opacity alone.
  function flash(node) {
    node.classList.remove("is-saved");
    requestAnimationFrame(() => node.classList.add("is-saved"));
    setTimeout(() => node.classList.remove("is-saved"), 1400);
  }

  function makeSecretRow(item) {
    const li = el("li", "secret");
    const head = el("div", "secret-head");
    const toggle = el("button", "btn btn-quiet secret-toggle");
    toggle.type = "button";
    head.append(el("span", "secret-name", item.label), el("span", "pill secret-state"), toggle);
    const form = el("form", "secret-form");
    form.autocomplete = "off";
    form.hidden = true;
    const input = el("input", "set-input");
    input.type = "password";
    input.id = `secret-input-${item.name}`;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.maxLength = 4096;
    input.placeholder = "paste it here";
    input.setAttribute("aria-label", item.label);
    const save = el("button", "btn btn-primary", "Store");
    save.type = "submit";
    const cancel = el("button", "btn btn-quiet secret-cancel", "Cancel");
    cancel.type = "button";
    form.append(input, save, cancel);
    li.append(head, el("span", "set-hint", item.purpose), form);
    if (item.name === "telegram_bot_token") li.append(makePairBlock());
    return li;
  }

  // A bot with a token and no paired chat ignores everybody; pairing is a
  // one-time code sent to the bot, shown here once and never kept by the page.
  function makePairBlock() {
    const block = el("div", "secret-pair");
    const start = el("button", "btn btn-quiet pair-start", "Pair my phone");
    start.type = "button";
    const forget = el("button", "btn btn-quiet pair-forget", "Unpair");
    forget.type = "button";
    const code = el("output", "pair-code");
    code.hidden = true;
    const say = el("span", "set-hint pair-say");
    say.hidden = true;
    block.append(start, forget, code, say);
    return block;
  }

  function patchSecretRow(li, item) {
    const state = li.querySelector(".secret-state");
    setText(state, item.present ? "stored" : "not set");
    setAttr(state, "data-tone", item.present ? "live" : item.name === "gemini_api_key" ? "warn" : "done");
    setText(li.querySelector(".secret-toggle"), item.present ? "Replace" : "Add");
    const pair = li.querySelector(".secret-pair");
    if (pair) {
      show(pair, item.present);
      setText(pair.querySelector(".pair-start"), item.paired ? "Pair another phone" : "Pair my phone");
      show(pair.querySelector(".pair-forget"), item.paired);
    }
  }

  function renderSecrets(present) {
    const items = Object.keys(SECRET_INFO).map((name) => ({
      name,
      label: SECRET_INFO[name].label,
      purpose: SECRET_INFO[name].purpose,
      present: present[name] === true,
      paired: Boolean(app.setup && app.setup.phone && app.setup.phone.paired),
    }));
    syncList(dom.secretList, items, (x) => x.name, makeSecretRow, patchSecretRow);
  }

  function openSecretForm(li) {
    const form = li.querySelector(".secret-form");
    form.hidden = false;
    li.querySelector(".secret-toggle").hidden = true;
    form.querySelector("input").focus();
  }

  function closeSecretForm(li) {
    const form = li.querySelector(".secret-form");
    form.querySelector("input").value = "";
    form.hidden = true;
    li.querySelector(".secret-toggle").hidden = false;
  }

  // The value is read once, cleared from the field at once, and sent. It is
  // never kept, logged or written anywhere by this page.
  async function storeSecret(name, input, button, { restart = true } = {}) {
    const value = input.value.trim();
    input.value = "";
    if (!value) {
      toast("Paste the key first.", { tone: "warn", head: "Keys" });
      return false;
    }
    const data = await withBusy(button, () => api(API.setupSecret, { name, value, restart }));
    if (!data) return false;
    if (!restart) app.ob.unapplied = true;
    const r = Array.isArray(data.restarted) && data.restarted.length;
    toast(r ? "Stored in the keyring. Restarting what uses it." : "Stored in the keyring.", { head: "Keys" });
    await loadSetup();
    return true;
  }

  function onSecretClick(e) {
    const li = e.target.closest(".secret");
    if (!li) return;
    if (e.target.closest(".secret-toggle")) openSecretForm(li);
    else if (e.target.closest(".secret-cancel")) closeSecretForm(li);
    else if (e.target.closest(".pair-start")) pairPhone(li, e.target.closest(".pair-start"));
    else if (e.target.closest(".pair-forget")) unpairPhone(e.target.closest(".pair-forget"));
  }

  async function pairPhone(li, button) {
    const data = await withBusy(button, () => api(API.setupPhone, { action: "pair" }));
    if (!data || typeof data.code !== "string") return;
    const code = li.querySelector(".pair-code");
    const say = li.querySelector(".pair-say");
    setText(code, data.code);
    setText(say, appText(data.message, "Send this code to your bot."));
    show(code, true);
    show(say, true);
    // Gone when it expires: a dead code left on screen is one somebody tries.
    clearTimeout(app.pairTimer);
    app.pairTimer = setTimeout(() => {
      setText(code, "");
      show(code, false);
      show(say, false);
      loadSetup().catch(() => {});
    }, Math.max(1, Number(data.minutes) || 10) * 60000);
  }

  async function unpairPhone(button) {
    const data = await withBusy(button, () => api(API.setupPhone, { action: "unpair" }));
    if (!data) return;
    toast(appText(data.message, "Unpaired."), { head: "Telegram" });
    await loadSetup();
  }

  async function onSecretSubmit(e) {
    e.preventDefault();
    const li = e.target.closest(".secret");
    if (!li) return;
    const ok = await storeSecret(li.dataset.key, li.querySelector("input"), e.target.querySelector('[type="submit"]'));
    if (ok) closeSecretForm(li);
  }

  function makeProcRow() {
    const li = el("li", "proc");
    const restart = el("button", "btn btn-quiet proc-restart", "Restart");
    restart.type = "button";
    li.append(
      el("span", "proc-dot"),
      el("span", "proc-name"),
      el("span", "proc-state"),
      restart,
      el("span", "proc-reason"),
      el("span", "proc-log"),
    );
    return li;
  }

  function patchProcRow(li, x) {
    const beat = x.beat;
    const info = x.info;
    let state = "stopped";
    let words = info.last_exit !== null && info.last_exit !== undefined ? `stopped (exit ${info.last_exit})` : "stopped";
    if (isOnline(beat)) [state, words] = ["online", String(beat.state || "running")];
    else if (info.held) [state, words] = ["held", "waiting for you"];
    else if (info.running) [state, words] = ["starting", "starting"];
    const extra = [];
    if (info.pid) extra.push(`pid ${info.pid}`);
    if (info.restarts) extra.push(`restarted ${info.restarts}×`);
    setAttr(li, "data-state", state);
    setText(li.children[1], x.label);
    setText(li.children[2], [words, ...extra].join(" · "));
    setAttr(li.children[3], "aria-label", `Restart ${procThe(x.name)}`);
    setText(li.children[4], info.reason ? appText(info.reason, "") : "");
    show(li.children[4], !!info.reason);
    setText(li.children[5], info.log ? `log: ${info.log}` : "");
    show(li.children[5], !!info.log);
  }

  function renderProcesses() {
    show(dom.setProcesses, app.inApp && app.appLoaded);
    if (!app.inApp) return;
    const items = PROCS.filter((p) => app.appProcs[p.name]).map((p) => ({
      name: p.name,
      label: p.label,
      info: app.appProcs[p.name],
      beat: app.procs[p.name] || null,
    }));
    syncList(dom.procList, items, (x) => x.name, makeProcRow, patchProcRow);
  }

  async function previewVoice() {
    const voice = dom.setGeminiVoice.value;
    const data = await withBusy(dom.previewVoice, () => api(API.setupPreview, { voice }));
    if (data) toast(data.message || `Playing ${voice}.`, { head: "Voice" });
  }

  async function signInClaude() {
    const data = await withBusy(dom.claudeSignin, () => api(API.setupClaude, {}));
    if (data) toast(data.message || "The sign-in is open.", { head: "Claude Code" });
  }

  function onAddressClick(e) {
    const b = e.target.closest(".seg");
    if (!b || b.getAttribute("aria-checked") === "true") return;
    for (const x of dom.setAddress.children) setAttr(x, "aria-checked", x === b);
    dom.setAddressOther.value = "";
    saveSetting("persona.address", b.dataset.value)
      .then(() => loadSetup())
      .catch((err) => toast(appText(err.message, "That was not saved."), { tone: "bad", head: "Not saved" }));
  }

  // ───────────────────────────── onboarding ─────────────────────────────

  function greeting() {
    const h = new Date().getHours();
    return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  }

  function chosenAddress() {
    const b = dom.obAddress.querySelector('[aria-checked="true"]');
    return b ? b.dataset.value : "sir";
  }

  function openOnboarding(at) {
    const s = app.setup;
    if (!s) return;
    if (!app.ob.open) {
      app.ob.open = true;
      dom.app.setAttribute("inert", "");
      dom.onboard.hidden = false;
      setText(dom.obGreet, `${greeting()}.`);
      const v = s.settings || {};
      fillSegments(dom.obAddress, addressPairs(["sir", "ma'am", "boss"]), String(v["persona.address"] || "sir"));
      fillSegments(dom.obUnits, [["metric", "Metric"], ["imperial", "Imperial"]], String(v["location.units"] || "metric"));
      dom.obName.value = String(v["persona.name"] || "");
      dom.obCity.value = String(v["location.city"] || "");
      startWakeDownload();
    }
    goStep(ONBOARD_STEPS.includes(at) ? at : "key");
  }

  function closeOnboarding() {
    if (!app.ob.open) return;
    app.ob.open = false;
    dom.onboard.hidden = true;
    dom.obKey.value = "";
    dom.app.removeAttribute("inert");
    writeStore("sessionStorage", ONBOARD_KEY, "1");
    // "Later" and Escape end here too, after a key or a microphone may have
    // been saved: without a restart the voice keeps refusing for a reason
    // the user has already fixed.
    if (app.ob.unapplied) restartVoice();
  }

  async function restartVoice() {
    app.ob.unapplied = false;
    if (!app.inApp) return;
    try {
      await api(API.appRestart, { process: "desk" });
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        toast(appText(err && err.message, "I couldn't restart my voice."), { tone: "bad", head: "Voice" });
      }
    }
    await loadApp().catch(() => {});
    scheduleAppRefresh();
  }

  function goStep(step) {
    // A fresh look at the hardware each time the question is asked: the
    // headset may have been plugged in after the app started.
    if (step === "mic" && app.ob.step !== "mic") loadSetup({ rescan: true }).catch(() => {});
    app.ob.step = step;
    const at = ONBOARD_STEPS.indexOf(step);
    for (const section of dom.onboard.querySelectorAll(".onboard-step")) {
      show(section, section.dataset.step === step);
    }
    for (const li of dom.obSteps.children) {
      const i = ONBOARD_STEPS.indexOf(li.dataset.step);
      setAttr(li, "data-state", i < at ? "done" : i === at ? "current" : "todo");
      if (i === at) li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
    }
    // The greeting belongs to the first step; after that, the question is the page.
    show(dom.obGreet, at === 0);
    show(dom.obIntro, at === 0);
    dom.obBack.disabled = at === 0;
    renderOnboarding();
    const first = dom.onboard.querySelector(
      `.onboard-step[data-step="${step}"] input, .onboard-step[data-step="${step}"] [aria-checked="true"]`,
    );
    (first || dom.obNext).focus();
  }

  function renderOnboarding() {
    const s = app.setup;
    if (!s) return;
    const hasKey = !!(s.secrets && s.secrets.gemini_api_key);
    setText(
      dom.obKeyState,
      hasKey ? "A key is stored. Paste another only to replace it." : "",
    );
    setAttr(dom.obKeyState, "data-tone", hasKey ? "ok" : "");
    const step = app.ob.step;
    let next = "Continue";
    if (step === "key" && !hasKey && !dom.obKey.value.trim()) next = "Skip for now";
    if (step === "done") next = app.inApp ? "Start listening" : "Finish";
    setText(dom.obNext, next);
    if (step === "mic") renderObMics(s);
    if (step === "done") renderObDone(s);
    renderObWake();
  }

  function renderObMics(s) {
    const devices = Array.isArray(s.devices) ? s.devices : [];
    const current = String((s.settings || {})["voice.input_device"] || "");
    const items = [{ value: "", label: "System default", hint: "Whatever Windows is set to use" }].concat(
      devices.map((d) => ({ value: d.label, label: d.label, hint: "Listens and speaks" })),
    );
    syncList(
      dom.obMics,
      items,
      (x) => `m:${x.value}`,
      (x) => {
        const b = el("button", "choice");
        b.type = "button";
        b.setAttribute("role", "radio");
        b.dataset.value = x.value;
        b.append(el("span", "choice-dot"), el("span", "choice-label"), el("span", "choice-hint"));
        return b;
      },
      (b, x) => {
        setText(b.children[1], x.label);
        setText(b.children[2], x.hint);
        setAttr(b, "aria-checked", x.value === current);
      },
    );
    setText(
      dom.obMicWhy,
      s.devices_why
        ? appText(s.devices_why, "")
        : devices.length
          ? ""
          : "I can't find a microphone that can also play sound. Plug in a headset and press Look again.",
    );
  }

  function renderObDone(s) {
    const v = s.settings || {};
    const address = String(v["persona.address"] || "sir");
    setText(dom.obDoneH, `Very good, ${address}.`);
    const phrase = s.wake && s.wake.word ? wakeLabel(s.wake.word) : "";
    let say = phrase
      ? `I'll be listening for ${phrase}. Say it whenever you need me.`
      : "I'll be listening all the time; just speak.";
    if (app.inApp) say = `I'm starting the desk now. ${say}`;
    else say = `That's everything saved. Restart the desk to use it.`;
    setText(dom.obDoneSay, say);
    const mic = String(v["voice.input_device"] || "");
    const city = String(v["location.city"] || "");
    const lines = [
      ["Gemini key", s.secrets && s.secrets.gemini_api_key ? "stored in the keyring" : "not yet — add it in Settings"],
      ["Microphone", mic || "the system default"],
      ["Place", city || "worked out from your connection"],
      ["Wake word", !s.wake || !s.wake.word ? "none — always listening" : s.wake.ready || app.ob.wake === "ready" ? "ready" : "still fetching"],
    ];
    syncList(
      dom.obSummary,
      lines,
      (x) => x[0],
      () => {
        const li = el("li");
        li.append(el("span", "sum-k"), el("span", "sum-v"));
        return li;
      },
      (li, x) => {
        setText(li.children[0], x[0]);
        setText(li.children[1], x[1]);
      },
    );
  }

  // The wake model is fetched while the questions are answered, so "Ready"
  // rarely waits for it. Its licence is shown beside it, every time.
  async function startWakeDownload() {
    const w = app.setup && app.setup.wake;
    if (!w || !w.word) app.ob.wake = "none";
    else if (w.ready) app.ob.wake = "ready";
    if (app.ob.wake === "none" || app.ob.wake === "ready" || app.ob.wake === "fetching") {
      renderObWake();
      return;
    }
    app.ob.wake = "fetching";
    renderObWake();
    try {
      const data = await api(API.setupWake, { restart: false });
      app.ob.wake = "ready";
      app.ob.wakeMsg = data.message || "";
      // Finished after the dialog did: the voice was restarted without the
      // model and is waiting for it, so it is restarted again now.
      if (!app.ob.open) restartVoice();
      else app.ob.unapplied = true;
    } catch (err) {
      app.ob.wake = "failed";
      app.ob.wakeMsg = appText(err && err.message, "I couldn't fetch it.");
    }
    renderObWake();
    if (app.ob.open && app.ob.step === "done" && app.setup) renderObDone(app.setup);
  }

  function renderObWake() {
    const w = (app.setup && app.setup.wake) || {};
    const state = app.ob.wake;
    const said = app.ob.step === "done" && state === "ready";
    show(dom.obWake, state !== "none" && state !== "idle" && !said);
    const words = {
      fetching: ["fetching", "live", `Fetching the model that lets me hear ${wakeLabel(w.word || "hey_jarvis")}…`],
      ready: ["ready", "done", `The wake-word model is in place.`],
      failed: ["failed", "warn", app.ob.wakeMsg || "I couldn't fetch the wake-word model."],
    }[state] || ["—", "done", ""];
    setText(dom.obWakePill, words[0]);
    setAttr(dom.obWakePill, "data-tone", words[1]);
    setAttr(dom.obWakePill, "data-busy", state === "fetching");
    setText(dom.obWakeText, words[2]);
    show(dom.obWakeRetry, state === "failed");
    setText(dom.obWakeLicence, w.licence || "");
  }

  async function obNext() {
    const step = app.ob.step;
    const go = (to) => goStep(to);
    try {
      if (step === "key") {
        if (dom.obKey.value.trim()) {
          const ok = await storeSecret("gemini_api_key", dom.obKey, dom.obNext, { restart: false });
          if (!ok) return;
        }
        go("mic");
      } else if (step === "mic") {
        go("name");
      } else if (step === "name") {
        await withBusy(dom.obNext, async () => {
          await saveSetting("persona.address", chosenAddress(), { restart: false, quiet: true });
          await saveSetting("persona.name", dom.obName.value.trim(), { restart: false, quiet: true });
        });
        await loadSetup();
        go("city");
      } else if (step === "city") {
        const units = dom.obUnits.querySelector('[aria-checked="true"]');
        await withBusy(dom.obNext, async () => {
          await saveSetting("location.city", dom.obCity.value.trim(), { restart: false, quiet: true });
          await saveSetting("location.units", units ? units.dataset.value : "metric", { restart: false, quiet: true });
        });
        await loadSetup();
        go("done");
      } else if (step === "done") {
        await finishOnboarding();
      }
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        toast(appText(err.message, "That was not saved."), { tone: "bad", head: "Setup" });
      }
    }
  }

  async function finishOnboarding() {
    if (app.inApp) {
      const data = await withBusy(dom.obNext, () => api(API.appRestart, { process: "desk" }));
      if (!data) return;
      app.ob.unapplied = false;
      toast(`At your service, ${chosenAddress()}.`, { head: "Jarvis" });
    } else {
      toast("Saved. Restart the desk to use it.", { head: "Setup" });
    }
    closeOnboarding();
    await loadApp();
    scheduleAppRefresh();
  }

  function obBack() {
    const at = ONBOARD_STEPS.indexOf(app.ob.step);
    if (at > 0) goStep(ONBOARD_STEPS[at - 1]);
  }

  async function onObMicClick(e) {
    const b = e.target.closest(".choice");
    if (!b) return;
    for (const x of dom.obMics.children) setAttr(x, "aria-checked", x === b);
    try {
      await saveSetting("voice.input_device", b.dataset.value, { restart: false, quiet: true });
      await loadSetup();
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        toast(appText(err.message, "That was not saved."), { tone: "bad", head: "Microphone" });
      }
    }
  }

  function onSegmentClick(e) {
    const b = e.target.closest(".seg");
    if (!b) return;
    for (const x of b.parentElement.children) setAttr(x, "aria-checked", x === b);
  }

  // ───────────────────────────── goodbye ─────────────────────────────

  // The page outlives the server it came from. After Quit it says so, and
  // stops trying to reconnect to something that was asked to go away.
  function farewell() {
    app.locked = true;
    closeStream();
    clearTimeout(app.reconnectTimer);
    clearTimeout(app.refreshTimer);
    clearTimeout(app.appTimer);
    setLink("off");
    closeOnboarding();
    dom.app.setAttribute("inert", "");
    dom.farewell.hidden = false;
  }

  // ───────────────────────────── wiring ─────────────────────────────

  function cacheDom() {
    dom.app = byId("app");
    dom.gate = byId("gate");
    dom.netbar = byId("netbar");
    dom.netbarText = byId("netbar-text");
    dom.link = byId("link");
    dom.linkText = byId("link-text");
    dom.clockTime = byId("clock-time");
    dom.clockDate = byId("clock-date");
    dom.stop = byId("stop");
    dom.chips = {
      desk: [byId("chip-desk"), byId("chip-desk-state")],
      schedule: [byId("chip-schedule"), byId("chip-schedule-state")],
      telegram: [byId("chip-telegram"), byId("chip-telegram-state")],
    };

    dom.status = document.querySelector(".status");
    dom.orb = byId("orb");
    dom.orbState = byId("orb-state");
    dom.orbNote = byId("orb-note");
    dom.attention = byId("attention");
    dom.orbAction = byId("orb-action");
    dom.problems = byId("problems");
    dom.roDesk = byId("ro-desk");
    dom.roPresence = byId("ro-presence");
    dom.roWake = byId("ro-wake");
    dom.roVoice = byId("ro-voice");
    dom.roSpend = byId("ro-spend");

    dom.feedScroll = byId("feed-scroll");
    dom.feed = byId("feed");
    dom.feedEmpty = byId("feed-empty");
    dom.feedMeta = byId("feed-meta");
    dom.thinking = byId("thinking");
    dom.jump = byId("jump");
    dom.composer = byId("composer");
    dom.chatInput = byId("chat-input");
    dom.chatSend = byId("chat-send");
    dom.chatWhy = byId("chat-why");
    dom.speakReplies = byId("speak-replies");

    dom.tabList = byId("tabs");
    dom.tabs = {
      tools: byId("tab-tools"),
      reminders: byId("tab-reminders"),
      notes: byId("tab-notes"),
      questions: byId("tab-questions"),
      builds: byId("tab-builds"),
      hearing: byId("tab-hearing"),
      settings: byId("tab-settings"),
    };
    dom.panels = {
      tools: byId("panel-tools"),
      reminders: byId("panel-reminders"),
      notes: byId("panel-notes"),
      questions: byId("panel-questions"),
      builds: byId("panel-builds"),
      hearing: byId("panel-hearing"),
      settings: byId("panel-settings"),
    };
    dom.counts = {
      tools: byId("count-tools"),
      reminders: byId("count-reminders"),
      notes: byId("count-notes"),
      questions: byId("count-questions"),
      builds: byId("count-builds"),
      hearing: byId("count-hearing"),
    };

    dom.quick = byId("quick");
    dom.quickButtons = Array.from(dom.quick.querySelectorAll("button[data-quick]"));
    dom.quickForm = byId("quick-form");
    dom.result = byId("tool-result");
    dom.resultHead = byId("tool-result-head");
    dom.resultText = byId("tool-result-text");
    dom.openPalette = byId("open-palette");
    dom.toolList = byId("tool-list");
    dom.toolsEmpty = byId("tools-empty");

    dom.scheduleWarn = byId("schedule-warn");
    dom.scheduleRestart = byId("schedule-restart");
    dom.remindForm = byId("remind-form");
    dom.reminderList = byId("reminder-list");
    dom.remindersEmpty = byId("reminders-empty");
    dom.noteForm = byId("note-form");
    dom.noteList = byId("note-list");
    dom.notesEmpty = byId("notes-empty");
    dom.questionList = byId("question-list");
    dom.questionsEmpty = byId("questions-empty");
    dom.jobList = byId("job-list");
    dom.jobsEmpty = byId("jobs-empty");
    dom.projectLines = byId("project-lines");
    dom.projectsEmpty = byId("projects-empty");
    dom.hearingForm = byId("hearing-form");
    dom.hearingList = byId("hearing-list");
    dom.hearingEmpty = byId("hearing-empty");

    dom.toasts = byId("toasts");
    dom.palette = byId("palette");
    dom.paletteInput = byId("palette-input");
    dom.paletteList = byId("palette-list");

    dom.settingsOff = byId("settings-off");
    dom.settingsBody = byId("settings-body");
    dom.setGeminiVoice = byId("set-gemini-voice");
    dom.previewVoice = byId("preview-voice");
    dom.setReaderVoice = byId("set-reader-voice");
    dom.setMic = byId("set-mic");
    dom.setMicHint = byId("set-mic-hint");
    dom.setWakeWord = byId("set-wake-word");
    dom.wakePill = byId("wake-pill");
    dom.wakeDownload = byId("wake-download");
    dom.wakeLicence = byId("wake-licence");
    dom.setWakeThreshold = byId("set-wake-threshold");
    dom.setWakeOut = byId("set-wake-out");
    dom.setAddress = byId("set-address");
    dom.setAddressOther = byId("set-address-other");
    dom.setName = byId("set-name");
    dom.setCity = byId("set-city");
    dom.setUnits = byId("set-units");
    dom.setTz = byId("set-tz");
    dom.tzList = byId("tz-list");
    dom.setVocab = byId("set-vocab");
    dom.setLangs = byId("set-langs");
    dom.secretList = byId("secret-list");
    dom.claudeSignin = byId("claude-signin");
    dom.setAutostart = byId("set-autostart");
    dom.setTelegram = byId("set-telegram");
    dom.setOpenWindow = byId("set-open-window");
    dom.setProcesses = byId("set-processes");
    dom.procList = byId("proc-list");
    dom.rerunSetup = byId("rerun-setup");
    dom.quit = byId("quit");
    dom.farewell = byId("farewell");

    dom.onboard = byId("onboard");
    dom.obSteps = byId("onboard-steps");
    dom.obGreet = byId("onboard-greet");
    dom.obIntro = byId("onboard-intro");
    dom.obKeyForm = byId("ob-key-form");
    dom.obKey = byId("ob-key");
    dom.obKeyState = byId("ob-key-state");
    dom.obMics = byId("ob-mics");
    dom.micRescans = [byId("ob-mic-rescan"), byId("set-mic-rescan")];
    dom.obMicWhy = byId("ob-mic-why");
    dom.obAddress = byId("ob-address");
    dom.obName = byId("ob-name");
    dom.obCity = byId("ob-city");
    dom.obUnits = byId("ob-units");
    dom.obDoneH = byId("ob-h-done");
    dom.obDoneSay = byId("ob-done-say");
    dom.obSummary = byId("ob-summary");
    dom.obWake = byId("ob-wake");
    dom.obWakePill = byId("ob-wake-pill");
    dom.obWakeText = byId("ob-wake-text");
    dom.obWakeRetry = byId("ob-wake-retry");
    dom.obWakeLicence = byId("ob-wake-licence");
    dom.obLater = byId("ob-later");
    dom.obBack = byId("ob-back");
    dom.obNext = byId("ob-next");
  }

  // A fixed form that calls one tool with whatever its named inputs hold.
  function wireToolForm(form, tool) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const args = collectArgs(form);
      if (!args) return;
      const done = await withBusy(form.querySelector('button[type="submit"]'), () => runTool(tool, args));
      if (done !== undefined) form.reset();
    });
  }

  // A row's action button names the row by its id, which the memory tools
  // accept as an exact match — "cancel call mum" by words would also cancel
  // the other reminder that happens to share them.
  function wireRowAction(list, tool) {
    list.addEventListener("click", (e) => {
      const button = e.target.closest(".row-action");
      if (!button || button.tagName !== "BUTTON") return;
      const key = button.closest(".row").dataset.key;
      withBusy(button, () => runTool(tool, { about: key }));
    });
  }

  function wire() {
    document.documentElement.classList.toggle("is-hidden", document.hidden);
    document.addEventListener("visibilitychange", () => {
      document.documentElement.classList.toggle("is-hidden", document.hidden);
      if (!document.hidden) tickClock();
    });

    dom.composer.addEventListener("submit", submitChat);
    // On unless the user turned it off: an assistant with a voice should use it.
    dom.speakReplies.checked = readStore("localStorage", SPEAK_KEY) !== "0";
    dom.speakReplies.addEventListener("change", () =>
      writeStore("localStorage", SPEAK_KEY, dom.speakReplies.checked ? "1" : "0"),
    );

    dom.feed.addEventListener("click", (e) => {
      const button = e.target.closest(".msg-say");
      if (!button) return;
      const text = button.closest(".msg").querySelector(".msg-text").textContent.trim();
      if (text) withBusy(button, () => sayAloud(text));
    });
    dom.feedScroll.addEventListener(
      "scroll",
      () => {
        if (!dom.jump.hidden && feedAtEnd()) show(dom.jump, false);
      },
      { passive: true },
    );
    dom.jump.addEventListener("click", scrollFeedToEnd);

    for (const name of TABS) dom.tabs[name].addEventListener("click", () => selectTab(name, false));
    dom.tabList.addEventListener("keydown", onTabKey);
    dom.attention.addEventListener("click", () => selectTab("questions", true));

    for (const b of dom.quickButtons) if (QUICK[b.dataset.quick].fields) b.setAttribute("aria-expanded", "false");
    dom.quick.addEventListener("click", onQuickClick);
    dom.quickForm.addEventListener("submit", submitQuickForm);
    dom.quickForm.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeQuickForm();
    });

    wireToolForm(dom.remindForm, "remind_me");
    wireToolForm(dom.noteForm, "remember");
    wireToolForm(dom.hearingForm, "correct_hearing");
    wireRowAction(dom.reminderList, "cancel_reminder");
    wireRowAction(dom.noteList, "forget_note");
    dom.questionList.addEventListener("click", onQuestionClick);

    dom.openPalette.addEventListener("click", openPalette);
    dom.paletteInput.addEventListener("input", filterPalette);
    dom.paletteInput.addEventListener("keydown", onPaletteKey);
    dom.paletteList.addEventListener("click", (e) => {
      const li = e.target.closest('[role="option"]');
      if (li) runPaletteEntry(Number(li.dataset.i));
    });
    dom.palette.addEventListener("click", (e) => {
      if (e.target === dom.palette) closePalette();
    });

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (dom.palette.open) closePalette();
        else openPalette();
        return;
      }
      const typing = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
      if (e.key === "/" && !typing && !dom.palette.open && !app.locked) {
        e.preventDefault();
        dom.chatInput.focus();
      }
    });

    armStop();
    armQuit();

    dom.orbAction.addEventListener("click", () => {
      if (app.orbAction) app.orbAction(dom.orbAction);
    });
    dom.problems.addEventListener("click", (e) => {
      const b = e.target.closest(".problem-fix");
      if (b) runFix(b.dataset.action, b);
    });
    dom.scheduleRestart.addEventListener("click", () => restartProcess("schedule", dom.scheduleRestart));

    dom.settingsBody.addEventListener("change", (e) => {
      if (e.target.dataset && e.target.dataset.key) saveField(e.target);
    });
    dom.setWakeThreshold.addEventListener("input", () =>
      setText(dom.setWakeOut, Number(dom.setWakeThreshold.value).toFixed(2)),
    );
    dom.setAddress.addEventListener("click", onAddressClick);
    dom.previewVoice.addEventListener("click", previewVoice);
    dom.wakeDownload.addEventListener("click", () => downloadWake(dom.wakeDownload));
    dom.claudeSignin.addEventListener("click", signInClaude);
    dom.secretList.addEventListener("click", onSecretClick);
    dom.secretList.addEventListener("submit", onSecretSubmit);
    dom.procList.addEventListener("click", (e) => {
      const b = e.target.closest(".proc-restart");
      if (b) restartProcess(b.closest(".proc").dataset.key, b);
    });
    dom.rerunSetup.addEventListener("click", () => openOnboarding("key"));

    dom.obKeyForm.addEventListener("submit", (e) => {
      e.preventDefault();
      obNext();
    });
    dom.obKey.addEventListener("input", renderOnboarding);
    dom.obNext.addEventListener("click", obNext);
    dom.obBack.addEventListener("click", obBack);
    dom.obLater.addEventListener("click", closeOnboarding);
    dom.obMics.addEventListener("click", onObMicClick);
    for (const b of dom.micRescans) {
      b.addEventListener("click", () => withBusy(b, () => loadSetup({ rescan: true })));
    }
    dom.obAddress.addEventListener("click", onSegmentClick);
    dom.obUnits.addEventListener("click", onSegmentClick);
    dom.obWakeRetry.addEventListener("click", () => {
      app.ob.wake = "idle";
      startWakeDownload();
    });
    dom.onboard.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeOnboarding();
      } else if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target !== dom.obKey) {
        e.preventDefault();
        obNext();
      }
    });

    const remembered = readStore("localStorage", TAB_KEY);
    selectTab(TABS.includes(remembered) ? remembered : "tools", false);
  }

  async function boot() {
    cacheDom();
    wire();
    tickClock();
    renderDesk(null);
    app.token = takeToken();
    if (!app.token) {
      lockOut();
      return;
    }
    setLink("connecting");
    try {
      await loadState();
      await loadFeed();
      openStream();
    } catch (err) {
      if (app.locked) return;
      toast(err.message, { tone: "bad", head: "Connection" });
      setLink("retry");
      scheduleReconnect();
      return;
    }
    await loadApp();
    const setup = await loadSetup();
    // First run: no key and nothing saved yet. Once dismissed, not again in
    // this tab; Settings has the way back in.
    if (setup && setup.first_run && readStore("sessionStorage", ONBOARD_KEY) !== "1") openOnboarding("key");
  }

  boot();
})();
