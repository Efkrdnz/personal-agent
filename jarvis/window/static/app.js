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
  });

  const TOKEN_KEY = "jarvis.window.token";
  const SPEAK_KEY = "jarvis.window.speakReplies";
  const TAB_KEY = "jarvis.window.tab";

  const FEED_CAP = 300;
  const FEED_FIRST_PAGE = 200;
  const REFRESH_DEBOUNCE_MS = 300;
  const HOLD_MS = 1000;
  const BACKOFF_FIRST_MS = 1000;
  const BACKOFF_MAX_MS = 15000;
  const TOAST_CAP = 4;

  const DESK_STATES = new Set(["asleep", "awake", "listening", "speaking"]);
  const ROLES = new Set(["user", "jarvis", "system", "tool"]);
  const TABS = ["tools", "reminders", "notes", "questions", "builds", "hearing"];
  const TAB_TITLES = {
    tools: "Tools",
    reminders: "Reminders",
    notes: "Notes",
    questions: "Questions",
    builds: "Builds",
    hearing: "Hearing",
  };

  const PROCS = [
    { name: "desk", label: "Desk", start: "python -m jarvis desk" },
    { name: "schedule", label: "Scheduler", start: "python -m jarvis.schedule" },
    { name: "telegram", label: "Telegram", start: "python -m jarvis.telegram" },
  ];

  const DESK_WORDS = {
    unknown: "Connecting",
    offline: "Offline",
    asleep: "Asleep",
    awake: "Awake",
    listening: "Listening",
    speaking: "Speaking",
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
        "The window server is not answering. Is python -m jarvis window still running?",
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

  async function catchUpFeed() {
    if (app.lastSeq === null) {
      await loadFeed();
      return;
    }
    const data = await api(`${API.feed}?after=${app.lastSeq}&limit=500`);
    appendFeed(data.items);
    bumpSeq(data.last_seq);
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
    app.procs = procs || {};
    for (const p of PROCS) {
      const info = app.procs[p.name] || null;
      const online = isOnline(info);
      const [chip, stateEl] = dom.chips[p.name];
      const state = online ? String(info.state || "running") : "offline";
      setAttr(chip, "data-online", online);
      setAttr(chip, "data-state", state);
      setText(stateEl, online ? state : "off");
      const since = online && info.since ? ` since ${fmtWhen(info.since)}` : "";
      setAttr(
        chip,
        "title",
        online
          ? `${p.label} is running — ${state}${since}.`
          : `${p.label} is not running. Start it with ${p.start}.`,
      );
    }
    renderDesk(app.procs.desk || null);
    renderVoice();
    show(dom.scheduleWarn, !!app.snap && !isOnline(app.procs.schedule));
  }

  function wakeWord() {
    const w = app.snap && app.snap.wake ? app.snap.wake.word : "";
    return String(w || "").replace(/_/g, " ");
  }

  function renderDesk(info) {
    const online = isOnline(info);
    let state = "offline";
    if (!app.snap && !info) state = "unknown";
    else if (online) state = DESK_STATES.has(info.state) ? info.state : "awake";
    setAttr(dom.orb, "data-state", state);
    setAttr(dom.status, "data-desk", state);
    setText(dom.orbState, DESK_WORDS[state]);
    setText(
      dom.roDesk,
      online ? `${DESK_WORDS[state]} since ${fmtWhen(info.since) || "just now"}` : "Not running",
    );

    const key = `${state}|${wakeWord()}`;
    if (dom.orbNote.dataset.key === key) return;
    dom.orbNote.dataset.key = key;
    if (state === "offline") {
      dom.orbNote.replaceChildren(
        "The desk is not running — start it with ",
        el("code", "", "python -m jarvis desk"),
      );
      return;
    }
    const word = wakeWord();
    const notes = {
      unknown: "Reaching the window server…",
      asleep: word ? `Say “${word}” to wake it.` : "Asleep.",
      awake: "Ready. Speak whenever you like.",
      listening: "Hearing you…",
      speaking: "Talk over it to interrupt.",
    };
    setText(dom.orbNote, notes[state] || "");
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
    if (!sp.available) line = sp.why || "No voice is available.";
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
    setText(dom.chatWhy, can ? "" : chat.why || "Chat is not set up for this window.");
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
      li.append(time, el("span", "msg-text", text));
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
      setAttr(button, "title", ok ? "" : q.say ? app.speech.why || "No voice is available." : `${q.tool} is not offered to this window.`);
    }
  }

  async function runTool(name, args) {
    const data = await api(API.tool, { name, args });
    const said = typeof data.said === "string" && data.said ? data.said : "Done.";
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
        toast(err && err.message ? err.message : "That did not work.", { tone: "bad", head: "Failed" });
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
  }

  function onTabKey(e) {
    const current = TABS.indexOf(e.target.dataset.tab);
    if (current < 0) return;
    const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 3, ArrowUp: -3 }[e.key];
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
      await catchUpFeed().catch(() => {});
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

  // Hold, not click: the button kills every job and call, and a stray click on
  // a big red button must not be able to do that. Pointer and keyboard both.
  function armStop() {
    let timer = 0;
    let started = 0;

    const fire = async () => {
      timer = 0;
      dom.stop.classList.remove("is-holding");
      const data = await withBusy(dom.stop, () => api(API.stop, {}));
      if (!data) return;
      toast(data.message || "Stopped everything.", { tone: "warn", head: "Stop" });
      scheduleRefresh();
    };
    const start = () => {
      if (timer || dom.stop.disabled || app.locked) return;
      started = performance.now();
      dom.stop.classList.add("is-holding");
      timer = setTimeout(fire, HOLD_MS);
    };
    const cancel = () => {
      if (!timer) return;
      clearTimeout(timer);
      timer = 0;
      dom.stop.classList.remove("is-holding");
      if (performance.now() - started < HOLD_MS * 0.6) {
        toast("Hold Stop for a full second to stop everything.", { tone: "warn", head: "Stop" });
      }
    };

    // No pointer capture: a captured pointer never "leaves", and sliding off
    // the button is how a person changes their mind halfway through a hold.
    dom.stop.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      start();
    });
    dom.stop.addEventListener("pointerup", cancel);
    dom.stop.addEventListener("pointercancel", cancel);
    dom.stop.addEventListener("pointerleave", cancel);
    dom.stop.addEventListener("keydown", (e) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) {
        e.preventDefault();
        start();
      }
    });
    dom.stop.addEventListener("keyup", (e) => {
      if (e.key === " " || e.key === "Enter") cancel();
    });
    dom.stop.addEventListener("blur", cancel);
    dom.stop.addEventListener("contextmenu", (e) => e.preventDefault());
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
    };
    dom.panels = {
      tools: byId("panel-tools"),
      reminders: byId("panel-reminders"),
      notes: byId("panel-notes"),
      questions: byId("panel-questions"),
      builds: byId("panel-builds"),
      hearing: byId("panel-hearing"),
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
    dom.speakReplies.checked = readStore("localStorage", SPEAK_KEY) === "1";
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
    }
  }

  boot();
})();
