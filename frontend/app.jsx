/* Arrivo — AI settlement copilot powered entirely by Foundry agents.
   Layout: chat on the left, a persistent widget grid on the right.
   All copy, suggestions, plans, tips and map data come from /api/chat. */
const { useState, useEffect, useRef, useMemo } = React;

async function api(path, body) {
  const r = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : undefined);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function Icon({ name, size = 18 }) {
  const paths = {
    send: <g><path d="M22 2 11 13" /><path d="M22 2 15 22l-4-9-9-4z" /></g>,
    plus: <g><path d="M12 5v14M5 12h14" /></g>,
    check: <path d="M20 6 9 17l-5-5" />,
    spark: <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />,
    bot: <g><rect x="4" y="8" width="16" height="11" rx="3" /><path d="M12 8V4M9 4h6" /><circle cx="9" cy="13" r="1" fill="currentColor" stroke="none" /><circle cx="15" cy="13" r="1" fill="currentColor" stroke="none" /></g>,
    paperclip: <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />,
  }[name];
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths}</svg>
  );
}

const domain = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } };
const agentShort = (n) => (n || "").replace(/^arrivo-/, "").replace(/-v2$/, "");

const GROUP_COLORS = {
  accommodation: "#3a86ff", groceries: "#ffbe0b", food: "#fb5607",
  outdoors: "#ff006e", essentials: "#8338ec",
};

/* ---------- state merging: the right panel must never lose content unless reset ---------- */
function mergeFields(prev, next) {
  const out = { ...(prev || {}) };
  Object.entries(next || {}).forEach(([k, v]) => {
    if (v === null || v === undefined) return;
    if (v === "" && out[k]) return;
    if (Array.isArray(v) && v.length === 0 && Array.isArray(out[k]) && out[k].length) return;
    out[k] = v;
  });
  return out;
}

function mergeJourney(prev, next) {
  if (!next) return prev;
  const p = prev || {};
  const keepArr = (a, b) => (Array.isArray(b) && b.length ? b : (a || []));
  const keepObj = (a, b, has) => (b && has(b) ? b : (a || {}));
  return {
    profile: keepObj(p.profile, next.profile, (o) => !!o.summary) || { summary: "" },
    steps: keepArr(p.steps, next.steps),
    suburbs: keepArr(p.suburbs, next.suburbs),
    places: keepArr(p.places, next.places),
    tips: keepArr(p.tips, next.tips),
    catalog: { ...(p.catalog || {}), ...(next.catalog || {}) },
    highlight_steps: keepArr(p.highlight_steps, next.highlight_steps),
    stats: (next.stats && Object.keys(next.stats).length) ? { ...(p.stats || {}), ...next.stats } : (p.stats || {}),
    welcome: keepObj(p.welcome, next.welcome, (o) => !!(o.title || o.body)),
    provider: keepObj(p.provider, next.provider, (o) => !!o.name),
  };
}

function App() {
  const [resetKey, setResetKey] = useState(0);
  return <Studio key={resetKey} onReset={() => setResetKey((k) => k + 1)} />;
}

function Studio({ onReset }) {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(true);
  const [bootErr, setBootErr] = useState("");
  const [err, setErr] = useState("");
  const [fields, setFields] = useState({});
  const [ready, setReady] = useState(false);
  const [showMap, setShowMap] = useState(false);
  const [journey, setJourney] = useState(null);
  const [done, setDone] = useState([]);
  const [citations, setCitations] = useState([]);
  const booted = useRef(false);

  function applyAgentResponse(res) {
    setFields((f) => mergeFields(f, res.fields));
    setReady((r) => !!res.ready || r);          // once ready, stay ready
    if (res.show_map) setShowMap(true);          // sticky: map only revealed, never hidden
    setJourney((j) => mergeJourney(j, res.journey));
    if (res.citations && res.citations.length) setCitations(res.citations);
    return {
      role: "assistant",
      content: res.reply || "",
      suggestions: res.suggestions || [],
      agents: res.agents_used || [],
    };
  }

  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    (async () => {
      setBusy(true);
      setBootErr("");
      try {
        const res = await api("/api/chat", { messages: [] });
        setMessages([applyAgentResponse(res)]);
      } catch (e) {
        setBootErr(e.message);
      } finally {
        setBusy(false);
      }
    })();
  }, []);

  async function send(q) {
    const content = (q ?? text).trim();
    if (!content || busy) return;
    const next = [...messages, { role: "user", content }];
    setMessages(next);
    setText("");
    setBusy(true);
    setErr("");
    try {
      const res = await api("/api/chat", { messages: next });
      setMessages((m) => [...m, applyAgentResponse(res)]);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const toggleDone = (id) =>
    setDone((d) => (d.includes(id) ? d.filter((x) => x !== id) : [...d, id]));

  const inlineWidgets = useMemo(() => {
    if (!journey) return [];
    const w = [];
    if (fields.weekly_budget && journey.suburbs?.length) {
      w.push({
        type: "budget", key: "budget",
        data: { budget: fields.weekly_budget, suburbs: journey.suburbs.slice(0, 3) },
      });
    }
    (journey.highlight_steps || []).forEach((sid) => {
      const step = journey.catalog?.[sid];
      if (step) w.push({ type: "step", key: sid, data: step });
    });
    return w;
  }, [fields, journey]);

  const started = messages.some((m) => m.role === "user");

  if (!started) {
    return (
      <div className="intro-screen">
        <div className="intro-brand">
          <div className="brand-mark">A</div>
          <div><h1>Arrivo</h1></div>
        </div>
        <div className="intro-card">
          <ChatPane messages={messages} busy={busy} text={text} setText={setText}
            onSend={send} error={err || bootErr} inlineWidgets={[]} booting={busy && !messages.length} />
        </div>
      </div>
    );
  }

  return (
    <div className="studio">
      <main className="studio-chat">
        <ChatPane messages={messages} busy={busy} text={text} setText={setText}
          onSend={send} error={err} inlineWidgets={inlineWidgets} onReset={onReset} />
      </main>
      <aside className="studio-right">
        <ContextPanel journey={journey} fields={fields} ready={ready}
          showMap={showMap} done={done} toggleDone={toggleDone} busy={busy}
          citations={citations} onReset={onReset} />
      </aside>
    </div>
  );
}

/* ---------------- Chat (left) ---------------- */
function ChatPane({ messages, busy, text, setText, onSend, error, inlineWidgets, booting, onReset }) {
  const scrollRef = useRef(null);
  const lastBot = messages.reduce((i, m, idx) => m.role === "assistant" ? idx : i, -1);
  const activeSuggestions = !busy && lastBot >= 0 ? (messages[lastBot].suggestions || []) : [];
  useEffect(() => { scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight); },
    [messages, busy, inlineWidgets, activeSuggestions.length]);

  return (
    <div className="chat-shell card">
      <div className="chat-top">
        <div className="chat-avatar-sm">A</div>
        <div>
          <div className="chat-title">Arrivo Chat</div>
          <div className="chat-status">{busy ? "Agents working…" : "Online · Foundry agents"}</div>
        </div>
        {onReset &&
          <button className="chat-reset" onClick={onReset} title="Start a new plan">
            <Icon name="plus" size={15} /> New plan
          </button>}
      </div>

      <div className="chat-scroll" ref={scrollRef}>
        {booting && !messages.length &&
          <div className="bub bot"><div className="typing"><span /><span /><span /></div></div>}
        {messages.map((m, i) => (
          <React.Fragment key={i}>
            <div className={"bub " + (m.role === "user" ? "user" : "bot")}>{m.content}</div>
            {m.role === "assistant" && m.agents?.length > 0 &&
              <div className="agent-tags">
                {m.agents.map((a, j) =>
                  <span key={j} className="agent-tag"><Icon name="bot" size={11} /> {agentShort(a)}</span>)}
              </div>}
            {m.role === "assistant" && i === lastBot && inlineWidgets.length > 0 &&
              <div className="inline-widgets">
                {inlineWidgets.map((w) =>
                  w.type === "budget"
                    ? <InlineBudget key={w.key} budget={w.data.budget} suburbs={w.data.suburbs} />
                    : <InlineStep key={w.key} step={w.data} />)}
              </div>}
            {m.role === "assistant" && i === lastBot && activeSuggestions.length > 0 &&
              <div className="suggestion-chips">
                {activeSuggestions.map((s, j) =>
                  <button key={j} className="suggestion-chip" disabled={busy}
                    onClick={() => onSend(s)}>{s}</button>)}
              </div>}
          </React.Fragment>
        ))}
        {busy && messages.length > 0 &&
          <React.Fragment>
            <div className="bub bot"><div className="typing"><span /><span /><span /></div></div>
            <div className="agent-tags"><span className="agent-tag pending"><Icon name="bot" size={11} /> orchestrator routing…</span></div>
          </React.Fragment>}
      </div>

      {error && <div className="chat-err">{error}</div>}

      <div className="chat-compose">
        <button className="compose-icon" type="button" title="Attach"><Icon name="paperclip" size={18} /></button>
        <textarea value={text} rows="1" autoFocus placeholder="Type your message…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); } }} />
        <button className="chat-send" disabled={busy || !text.trim()} onClick={() => onSend()} title="Send">
          <Icon name="send" size={17} /></button>
      </div>
    </div>
  );
}

function InlineBudget({ budget, suburbs }) {
  return (
    <div className="inline-card budget-inline">
      <div className="inline-card-icon">💰</div>
      <div>
        <div className="inline-card-title">Budget</div>
        <div className="inline-card-val">~${budget}/week</div>
        {suburbs?.length > 0 &&
          <div className="inline-card-sub">{suburbs.map((s) => s.name).join(" · ")}</div>}
      </div>
    </div>
  );
}

function InlineStep({ step }) {
  return (
    <div className="inline-card step-inline">
      <div className="inline-card-icon">📋</div>
      <div>
        <div className="inline-card-title">{step.title}</div>
        {step.why &&
          <div className="inline-card-sub">{step.why.slice(0, 90)}{step.why.length > 90 ? "…" : ""}</div>}
      </div>
    </div>
  );
}

/* ---------------- Right grid (persistent widgets) ---------------- */
function ContextPanel({ journey, fields, ready, showMap, done, toggleDone, busy, citations, onReset }) {
  if (!journey) {
    return (
      <div className="context-grid">
        <div className="card context-placeholder span-2">
          {busy ? "Foundry agents are building your plan…" : "Your plan appears here as we chat."}
        </div>
      </div>
    );
  }

  const welcome = journey.welcome || {};
  const provider = journey.provider || {};
  const tips = journey.tips || [];
  const hasSteps = (journey.steps || []).length > 0;
  const locationRelated = showMap || (journey.places?.length > 0) || (journey.suburbs?.length > 0);

  return (
    <div className="context-grid">
      <OverviewCard journey={journey} fields={fields} done={done} onReset={onReset} />
      {(welcome.title || welcome.body) &&
        <div className="card new-country">
          {welcome.tag && <div className="nc-badge">{welcome.tag}</div>}
          {welcome.title && <h3>{welcome.title}</h3>}
          {welcome.body && <p>{welcome.body}</p>}
          {journey.stats?.skipped_count > 0 &&
            <span className="nc-tag">Skipped {journey.stats.skipped_count} already sorted</span>}
        </div>}
      {fields.weekly_budget && journey.suburbs?.length > 0 &&
        <BudgetCard budget={fields.weekly_budget} suburbs={journey.suburbs} />}
      {hasSteps &&
        <TodoPanel steps={journey.steps} done={done} toggleDone={toggleDone} ready={ready} />}
      {locationRelated &&
        <div className="span-2"><MapPanel suburbs={journey.suburbs} places={journey.places} /></div>}
      {tips.length > 0 &&
        <div className="card tips-card span-2">
          <div className="todo-head"><h3>Tips from your specialists</h3></div>
          {tips.map((t, i) =>
            <div className="event-item" key={i}>
              <span className="event-dot" style={{ background: t.color || "#3a86ff" }} />
              <span>{t.text}</span>
            </div>)}
        </div>}
      {(provider.name || citations?.length > 0) &&
        <ProviderCard provider={provider} citations={citations} suburb={fields.work_suburb} />}
    </div>
  );
}

function OverviewCard({ journey, fields, done, onReset }) {
  const steps = journey?.steps || [];
  const pct = steps.length ? Math.round((done.length / steps.length) * 100) : 0;
  const visa = fields.visa_label || "";
  const household = fields.household_label || journey?.profile?.summary || "New arrival";
  return (
    <div className="card overview-card span-2">
      <div className="ov-left">
        <div className="avatar-ring"><div className="avatar">🦘</div></div>
        <div>
          <div className="sb-name">{household}</div>
          {visa && <div className="sb-visa">{visa}</div>}
        </div>
      </div>
      <div className="ov-right">
        <div className="sb-progress-top"><span>Overview</span><span>{pct}%</span></div>
        <div className="bar-track"><div className="bar-fill" style={{ width: pct + "%" }} /></div>
        {journey?.stats?.first_action &&
          <div className="ov-next">Up next: {journey.stats.first_action}</div>}
      </div>
    </div>
  );
}

function BudgetCard({ budget, suburbs }) {
  const subs = (suburbs || []).slice(0, 4);
  return (
    <div className="card budget-card">
      <div className="todo-head"><h3>Your budget</h3><span>~${budget}/wk</span></div>
      <div className="todo-list">
        {subs.map((s) => (
          <div className="sub-row" key={s.name}>
            <span>{s.name}{s.distance_km != null ? ` · ${s.distance_km} km` : ""}</span>
            <span className={s.over_budget ? "over" : ""}>${s.rent}/wk{s.over_budget ? " ⚠︎" : ""}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function TodoPanel({ steps, done, toggleDone, ready }) {
  const items = (steps || []).slice(0, ready ? steps.length : 6);
  return (
    <div className="card todo-card">
      <div className="todo-head">
        <h3>Your to-do list</h3>
        <span>{done.length}/{steps?.length || 0}</span>
      </div>
      <div className="todo-list">
        {items.map((s) => {
          const isDone = done.includes(s.id);
          return (
            <label key={s.id} className={"todo-row" + (isDone ? " done" : "")}>
              <input type="checkbox" checked={isDone} onChange={() => toggleDone(s.id)} />
              <span className="todo-text">{s.title}</span>
            </label>
          );
        })}
        {!ready && steps?.length > 6 &&
          <div className="todo-more">+{steps.length - 6} more as we chat…</div>}
      </div>
    </div>
  );
}

function ProviderCard({ provider, citations, suburb }) {
  const src = citations?.[0];
  return (
    <div className="card provider-card span-2">
      <div className="provider-avatar">🏛️</div>
      <div>
        <div className="provider-name">{provider.name || src?.title || "Official source"}</div>
        <div className="provider-sub">{provider.subtitle || (suburb ? `Near ${suburb}` : "")}</div>
        {provider.verified_label &&
          <span className="provider-verified">✓ {provider.verified_label}</span>}
        {src?.url &&
          <a className="src-link" href={src.url} target="_blank" rel="noopener">{domain(src.url)}</a>}
      </div>
    </div>
  );
}

/* ---------------- Map ---------------- */
function MapPanel({ suburbs, places }) {
  const elRef = useRef(null), mapRef = useRef(null), layerRef = useRef(null);

  const groups = useMemo(() => {
    const map = new Map();
    if (suburbs?.length) {
      map.set("accommodation", { label: "Where to live", emoji: "🏠", color: GROUP_COLORS.accommodation });
    }
    (places || []).forEach((p) => {
      const g = p.group || "essentials";
      if (!map.has(g)) {
        map.set(g, { label: p.group_label || g, emoji: "📍", color: GROUP_COLORS[g] || "#3a86ff" });
      }
    });
    return [...map.entries()].map(([key, meta]) => ({ key, ...meta }));
  }, [suburbs, places]);

  const [active, setActive] = useState({});
  useEffect(() => {
    setActive((prev) => {
      const init = { ...prev };
      groups.forEach((g) => { if (!(g.key in init)) init[g.key] = true; });
      return init;
    });
  }, [groups.map((g) => g.key).join(",")]);

  useEffect(() => {
    if (!elRef.current || mapRef.current) return;
    const map = L.map(elRef.current, { scrollWheelZoom: false });
    L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 19 }).addTo(map);
    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);
  }, []);

  useEffect(() => {
    const map = mapRef.current, layer = layerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();
    const pts = [];
    const pin = (color, label) => L.divIcon({
      className: "", iconSize: [22, 22], iconAnchor: [11, 11],
      html: `<div class="map-pin" style="background:${color}">${label}</div>`,
    });

    if (active.accommodation) {
      (suburbs || []).forEach((s) => {
        pts.push([s.lat, s.lng]);
        const over = s.over_budget ? ' <span style="color:#fb5607">(above budget)</span>' : "";
        L.marker([s.lat, s.lng], { icon: pin(GROUP_COLORS.accommodation, "🏠") })
          .addTo(layer)
          .bindPopup(`<b>${s.name}</b><br>~$${s.rent}/wk${over}${s.distance_km != null ? ` · ${s.distance_km} km` : ""}<br>${s.notes || ""}`);
      });
    }
    (places || []).forEach((p) => {
      const g = p.group || "essentials";
      if (!active[g]) return;
      const meta = groups.find((x) => x.key === g) || { color: "#3a86ff", emoji: "📍", label: g };
      pts.push([p.lat, p.lng]);
      L.marker([p.lat, p.lng], { icon: pin(meta.color, meta.emoji) })
        .addTo(layer)
        .bindPopup(`<b>${p.name}</b><br>${meta.label} · ${p.suburb || ""}<br>${p.desc || ""}`);
    });
    if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 13 });
    setTimeout(() => map.invalidateSize(), 80);
  }, [suburbs, places, active, groups]);

  if (!groups.length) return null;

  return (
    <div className="card map-card">
      <div className="map-head"><h3>Your neighbourhood</h3><span>homes & spots for you</span></div>
      <div id="map" ref={elRef} />
      <div className="map-filters">
        {groups.map((g) => {
          const on = !!active[g.key];
          return (
            <button key={g.key} className={"map-filter" + (on ? " on" : "")}
              style={on ? { borderColor: g.color, color: g.color } : {}}
              onClick={() => setActive((a) => ({ ...a, [g.key]: !a[g.key] }))}>
              <span className="filt-dot" style={{ background: g.color }} />{g.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
