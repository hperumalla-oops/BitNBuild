/* Chat tab — threads in localStorage, agent on /api/chat.
 *
 * The whole thread is posted every turn, so the model keeps the conversation;
 * the server stores nothing. Model output is untrusted text, so it is rendered
 * by building DOM nodes, never with innerHTML.
 */
(function () {
  "use strict";

  var KEY = "wardatlas.chats.v1";
  var state = { threads: [], current: null, mode: "map", busy: false };

  var $ = function (id) { return document.getElementById(id); };
  var messagesEl = $("messages");
  var listEl = $("thread-list");
  var inputEl = $("chat-input");
  var sendEl = $("chat-send");

  // ---- storage ------------------------------------------------------------
  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (raw) state.threads = JSON.parse(raw) || [];
    } catch (e) { state.threads = []; }        // private mode, blocked storage
    if (!state.threads.length) newThread(true);
    state.current = state.threads[0].id;
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state.threads.slice(0, 40))); }
    catch (e) { /* over quota or blocked — the session still works in memory */ }
  }
  function thread() {
    return state.threads.filter(function (t) { return t.id === state.current; })[0];
  }
  function newThread(quiet) {
    var t = { id: String(Date.now()) + Math.random().toString(36).slice(2, 6),
              title: "New chat", mode: state.mode, messages: [] };
    state.threads.unshift(t);
    state.current = t.id;
    save();
    if (!quiet) { renderThreads(); renderMessages(); inputEl.focus(); }
    return t;
  }

  // ---- tiny markdown ------------------------------------------------------
  // Handles what the model actually emits: links, bold, inline code, bullets.
  function inline(parent, text) {
    var re = /(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*([^*]+)\*\*)|(`([^`]+)`)/g;
    var last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1]) {
        var href = m[3], a = document.createElement("a");
        a.textContent = m[2];
        if (/^https?:\/\//i.test(href)) {
          a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
        } else if (href.charAt(0) === "?" || href.charAt(0) === "#") {
          a.href = href;                       // in-page map deep link
          a.className = "maplink";
          a.addEventListener("click", function (ev) {
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return;  // let it open a tab
            ev.preventDefault();
            window.openMapLink(href);
          });
        } else {
          a.textContent = m[2] + " (" + href + ")";
        }
        parent.appendChild(a);
      } else if (m[4]) {
        var b = document.createElement("strong"); b.textContent = m[5]; parent.appendChild(b);
      } else if (m[6]) {
        var c = document.createElement("code"); c.textContent = m[7]; parent.appendChild(c);
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function markdown(container, text) {
    var lines = String(text == null ? "" : text).split("\n");
    var list = null, para = [];
    function flushPara() {
      if (!para.length) return;
      var p = document.createElement("p");
      inline(p, para.join(" "));
      container.appendChild(p);
      para = [];
    }
    lines.forEach(function (raw) {
      var line = raw.replace(/\s+$/, "");
      var bullet = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(line);
      if (bullet) {
        flushPara();
        if (!list) { list = document.createElement("ul"); container.appendChild(list); }
        var li = document.createElement("li");
        inline(li, bullet[2]);
        list.appendChild(li);
        return;
      }
      list = null;
      if (!line.trim()) { flushPara(); return; }
      para.push(line.trim());
    });
    flushPara();
  }

  // ---- rendering ----------------------------------------------------------
  function renderThreads() {
    listEl.textContent = "";
    state.threads.forEach(function (t) {
      var li = document.createElement("li");
      if (t.id === state.current) li.className = "on";
      var pick = document.createElement("button");
      pick.className = "pick";
      pick.textContent = t.title;
      pick.title = t.title;
      pick.onclick = function () {
        state.current = t.id;
        if (t.mode) setMode(t.mode, true);
        renderThreads(); renderMessages();
      };
      var del = document.createElement("button");
      del.className = "del";
      del.textContent = "×";
      del.title = "Delete chat";
      del.setAttribute("aria-label", "Delete chat");
      del.onclick = function (e) {
        e.stopPropagation();
        state.threads = state.threads.filter(function (x) { return x.id !== t.id; });
        if (!state.threads.length) newThread(true);
        if (state.current === t.id) state.current = state.threads[0].id;
        save(); renderThreads(); renderMessages();
      };
      li.appendChild(pick); li.appendChild(del);
      listEl.appendChild(li);
    });
  }

  function bubble(role, build) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    var who = document.createElement("div");
    who.className = "who";
    who.textContent = role === "user" ? "You" : "AI";
    var body = document.createElement("div");
    body.className = "body";
    build(body);
    wrap.appendChild(who); wrap.appendChild(body);
    messagesEl.appendChild(wrap);
    return body;
  }

  function renderMessages() {
    messagesEl.textContent = "";
    var t = thread();
    if (!t || !t.messages.length) { renderEmpty(); return; }
    t.messages.forEach(function (m) {
      bubble(m.role, function (body) {
        if (m.role === "assistant" && m.tools && m.tools.length) {
          var row = document.createElement("div");
          row.className = "toolrow";
          m.tools.forEach(function (x) {
            var chip = document.createElement("span");
            chip.className = "toolchip" + (x.ok ? "" : " bad");
            chip.textContent = x.tool;
            row.appendChild(chip);
          });
          body.appendChild(row);
        }
        markdown(body, m.content);
      });
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  var EXAMPLES = {
    map: ["Which 5 wards have the most reports per sq km?",
          "Tell me about Sunkenahalli",
          "Which metro stations are within 2km of Majestic?",
          "What road projects run through Hebbal?"],
    zoning: ["What is the maximum FAR for a residential plot?",
             "What setbacks apply to a 300 sqm plot?",
             "Is a nursing home permitted in a residential zone?",
             "What are the parking requirements for offices?"]
  };

  function renderEmpty() {
    var d = document.createElement("div");
    d.className = "empty";
    var h = document.createElement("h3");
    h.textContent = state.mode === "zoning"
      ? "Ask about Bengaluru zoning regulation"
      : "Ask about the ward map";
    d.appendChild(h);
    var p = document.createElement("p");
    p.textContent = state.mode === "zoning"
      ? "2,629 chunks of regulation text plus the structured rule tables."
      : "369 wards, 126 metro stations and 16 road projects.";
    d.appendChild(p);
    var ex = document.createElement("div");
    ex.className = "examples";
    EXAMPLES[state.mode].forEach(function (q) {
      var b = document.createElement("button");
      b.textContent = q;
      b.onclick = function () { inputEl.value = q; send(); };
      ex.appendChild(b);
    });
    d.appendChild(ex);
    messagesEl.appendChild(d);
  }

  // ---- sending ------------------------------------------------------------
  function send() {
    if (state.busy) return;
    var text = inputEl.value.trim();
    if (!text) return;
    var t = thread();
    if (t.title === "New chat") {
      t.title = text.length > 34 ? text.slice(0, 34) + "…" : text;
      t.mode = state.mode;
      renderThreads();
    }
    t.messages.push({ role: "user", content: text });
    inputEl.value = "";
    inputEl.style.height = "auto";
    save();
    renderMessages();

    state.busy = true;
    sendEl.disabled = true;
    var pending = bubble("assistant", function (body) {
      var w = document.createElement("span");
      w.className = "typing";
      w.appendChild(document.createElement("i"));
      w.appendChild(document.createElement("i"));
      w.appendChild(document.createElement("i"));
      body.appendChild(w);
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: t.messages.map(function (m) {
          return { role: m.role, content: m.content };
        }),
        mode: state.mode
      })
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      if (!res.ok) throw new Error(res.d && res.d.error ? res.d.error : "request failed");
      t.messages.push({ role: "assistant", content: res.d.reply,
                        tools: res.d.tools_used || [] });
      save(); renderMessages();
    }).catch(function (e) {
      pending.textContent = "";
      markdown(pending, "**Could not answer.** " + e.message +
        "\n\nIs the server running? `python -m uvicorn server.app:app --port 8000`");
    }).then(function () {
      state.busy = false;
      sendEl.disabled = false;
      inputEl.focus();
    });
  }

  // ---- mode + tabs --------------------------------------------------------
  function setMode(mode, quiet) {
    state.mode = mode === "zoning" ? "zoning" : "map";
    document.querySelectorAll(".seg button").forEach(function (b) {
      b.setAttribute("aria-checked", String(b.dataset.mode === state.mode));
    });
    inputEl.placeholder = state.mode === "zoning"
      ? "Ask about FAR, setbacks, permitted uses…"
      : "Ask about wards, reports, stations…";
    if (!quiet) {
      var t = thread();
      if (t && !t.messages.length) { t.mode = state.mode; renderMessages(); }
    }
  }

  function setTab(tab) {
    var chat = tab === "chat";
    document.querySelectorAll(".tabs button").forEach(function (b) {
      b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    });
    $("chatview").hidden = !chat;
    document.querySelector(".mapctrls").hidden = chat;
    document.querySelector(".chatctrls").hidden = !chat;
    $("btn-table").hidden = chat;
    if (chat) inputEl.focus();
  }

  // Called by a map_link in an answer: switch to the map and apply the params.
  window.openMapLink = function (href) {
    var qs = new URLSearchParams(href.replace(/^[?#]/, ""));
    setTab("map");
    if (window.applyMapParams) window.applyMapParams(qs);
  };

  // ---- wiring -------------------------------------------------------------
  document.querySelectorAll(".tabs button").forEach(function (b) {
    b.onclick = function () { setTab(b.dataset.tab); };
  });
  document.querySelectorAll(".seg button").forEach(function (b) {
    b.onclick = function () { setMode(b.dataset.mode); };
  });
  $("new-chat").onclick = function () { newThread(); };
  $("composer").onsubmit = function (e) { e.preventDefault(); send(); };
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + "px";
  });

  fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
    var el = $("chat-health");
    if (h.ok) {
      el.textContent = h.chat_model + (h.tavily ? " · web search on" : " · no web search");
    } else {
      el.className = "health bad";
      el.textContent = "missing: " + (h.missing || []).join(", ");
    }
  }).catch(function () {
    var el = $("chat-health");
    el.className = "health bad";
    el.textContent = "server not reachable — run uvicorn";
  });

  load();
  setMode("map", true);
  renderThreads();
  renderMessages();

  if (new URLSearchParams(location.search).get("tab") === "chat") setTab("chat");
})();
