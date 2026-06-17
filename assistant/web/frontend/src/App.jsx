import React, { useEffect, useState, useCallback, useRef } from "react";
import { api, timeAgo } from "./api.js";
import { initTelegramApp, isTelegramMiniApp, getTelegramTheme, haptic } from "./telegram.js";
import { authenticateWithTelegram } from "./miniapp_auth.js";

const TIER_CLASS = {
  "Filed away quietly": "t0",
  "Told you, handled": "t1",
  "Drafting a reply for you": "t2",
  "Needs your decision": "t3",
};

// Primary bottom-nav tabs + secondary tabs accessible via "More"
const PRIMARY_TABS = [
  { id: "queue",       icon: "📬", label: "Queue" },
  { id: "commitments", icon: "✅", label: "Promises" },
  { id: "people",      icon: "👥", label: "People" },
  { id: "settings",    icon: "⚙️", label: "Settings" },
];
// Voice / Rules / WhatsApp are folded into the Settings screen; the dev-only tabs
// (Test the brain, Metrics) and the standalone Audit tab were cut ("less is more").
const MORE_TABS = [];

function useTheme() {
  const [theme, setTheme] = useState(() => {
    const tgTheme = getTelegramTheme();
    if (tgTheme) return "tg";
    return localStorage.getItem("theme") || "light";
  });
  useEffect(() => {
    if (theme !== "tg") {
      document.documentElement.setAttribute("data-theme", theme);
      localStorage.setItem("theme", theme);
    }
  }, [theme]);
  return [theme, setTheme];
}

// ─── Header ─────────────────────────────────────────────────────────────────
function Header({ status, theme, setTheme, lastSync, syncing, onRefresh }) {
  if (!status) return null;
  return (
    <div className="header">
      <h1>Steward</h1>
      <span
        className={"badge " + (status.live ? "live" : "dry")}
        title={status.live ? "Sends are real — every send still needs your tap" : "Safe mode — drafts only, nothing is sent"}
      >
        {status.live ? "LIVE" : "SAFE MODE"}
      </span>
      {status.paused && <span className="badge dry">PAUSED</span>}
      <span className="spacer" />
      <button
        className="icon-btn sync-btn"
        onClick={onRefresh}
        title="Fetch everything now (email + WhatsApp)"
        aria-label="Fetch everything now"
      >
        <span className={"sync-glyph" + (syncing ? " spin" : "")}>⟳</span>
        <span className="sync-label">
          {syncing ? "Fetching…" : lastSync ? "Updated " + timeAgo(lastSync) : "Fetch now"}
        </span>
      </button>
      {theme !== "tg" && (
        <button className="icon-btn" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
          {theme === "dark" ? "☀︎" : "☾"}
        </button>
      )}
    </div>
  );
}

// ─── Connection health strip ─────────────────────────────────────────────────
// At-a-glance "what's wired up" for a brand-new user — the first thing that
// answers "is this actually working?". Green dot = connected, grey = not yet.
function Connections({ status }) {
  if (!status || !status.connections) return null;
  const c = status.connections;
  const items = [
    { key: "gmail", icon: "📧", name: "Email", on: c.gmail?.connected, hint: c.gmail?.label },
    { key: "telegram", icon: "✈️", name: "Telegram", on: c.telegram?.connected, hint: c.telegram?.label },
  ];
  if (c.whatsapp?.enabled) {
    items.push({ key: "whatsapp", icon: "💬", name: "WhatsApp", on: c.whatsapp?.connected, hint: c.whatsapp?.label });
  }
  return (
    <div className="connections">
      {items.map((it) => (
        <div key={it.key} className={"conn" + (it.on ? " on" : " off")} title={it.hint}>
          <span className="conn-dot" />
          <span className="conn-icon">{it.icon}</span>
          <span className="conn-name">{it.name}</span>
        </div>
      ))}
    </div>
  );
}

// ─── Bottom nav (mobile only) ────────────────────────────────────────────────
function BottomNav({ tab, setTab }) {
  return (
    <nav className="bottom-nav">
      {PRIMARY_TABS.map((t) => (
        <button
          key={t.id}
          className={"nav-item" + (tab === t.id || (t.id === "more" && MORE_TABS.some((m) => m.id === tab)) ? " active" : "")}
          onClick={() => { haptic("light"); setTab(t.id); }}
        >
          <span className="nav-icon">{t.icon}</span>
          {t.label}
        </button>
      ))}
    </nav>
  );
}

// ─── Desktop tab bar ─────────────────────────────────────────────────────────
function DesktopTabs({ tab, setTab }) {
  const all = [
    ...PRIMARY_TABS.filter((t) => t.id !== "more"),
    ...MORE_TABS,
  ];
  return (
    <div className="tabs-desktop">
      {all.map((t) => (
        <button key={t.id} className={"tab" + (tab === t.id ? " active" : "")} onClick={() => setTab(t.id)}>
          {t.label || t.id}
        </button>
      ))}
    </div>
  );
}

// ─── Pipeline strip ──────────────────────────────────────────────────────────
function Pipeline({ pipeline }) {
  if (!pipeline) return null;
  const last = pipeline.last;
  const activeIdx = pipeline.busy ? 2 : last ? 4 : -1;
  return (
    <div className="pipeline">
      {pipeline.stages.map((name, i) => (
        <div key={name} className={"stage" + (i === activeIdx ? " active" : "")}>
          <div className="name"><span className="dot" />{name}</div>
          <div className="sub">
            {i === 4 && last ? `${last.who}: ${last.label}` : i === 2 && pipeline.busy ? "working…" : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Stat cards ──────────────────────────────────────────────────────────────
function Stats({ stats }) {
  if (!stats) return null;
  const card = (num, lbl, hero) => (
    <div className={"stat" + (hero ? " hero" : "")}>
      <div className="num">{num}</div>
      <div className="lbl">{lbl}</div>
    </div>
  );
  return (
    <div className="stats">
      {card(stats.conversations ?? 0, "Conversations")}
      {card(stats.replies_waiting, "Drafts waiting", true)}
      {card(stats.sent ?? 0, "Replies sent")}
      {card(stats.flagged_for_you, "Flagged for you")}
    </div>
  );
}

// ─── Recent activity feed (with non-destructive "clear for now") ───────────────
function ActivityFeed({ data, onClear }) {
  if (!data || !data.items || data.items.length === 0) return null;
  return (
    <div className="activity">
      <div className="activity-head">
        <span className="activity-title">Recent activity</span>
        <button className="link-btn" onClick={onClear}>Clear</button>
      </div>
      {data.items.slice(0, 8).map((a, i) => (
        <div key={i} className="activity-row">
          <span className="activity-what">{a.what}</span>
          <span className="activity-detail">{a.detail}</span>
          <span className="activity-at">{timeAgo(a.at)}</span>
        </div>
      ))}
    </div>
  );
}

// ─── Queue summary bar ────────────────────────────────────────────────────────
function QueueSummary({ summary }) {
  if (!summary) return null;
  const { waiting = 0, handled_today = 0, by_via = {} } = summary;
  const parts = [];
  if (by_via.telegram) parts.push(`${by_via.telegram} via Telegram`);
  if (by_via.web)      parts.push(`${by_via.web} via App`);
  if (by_via.human)    parts.push(`${by_via.human} by you`);
  if (by_via.direct)   parts.push(`${by_via.direct} replied directly`);
  if (by_via.auto)     parts.push(`${by_via.auto} auto-filed`);
  return (
    <div className="queue-summary">
      <span className="qs-waiting">{waiting} waiting</span>
      {handled_today > 0 && (
        <span className="qs-handled">
          · {handled_today} handled today
          {parts.length > 0 && <span className="qs-breakdown"> ({parts.join(", ")})</span>}
        </span>
      )}
    </div>
  );
}

// ─── Queue list ───────────────────────────────────────────────────────────────
const VIA_ICON = { telegram: "✈️", web: "🖥️", auto: "🤖", direct: "💬", human: "👤" };

function QueueSection({ title, items, selected, onSelect, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  if (items.length === 0) return null;
  return (
    <div className="queue-section">
      <button className="queue-section-hdr" onClick={() => setOpen(o => !o)}>
        <span>{title}</span>
        <span className="qs-count">{items.length} {open ? "▲" : "▼"}</span>
      </button>
      {open && items.map((it) => (
        <div
          key={it.message_id}
          className={"row" + (selected === it.message_id ? " sel" : "") + (it.response_via ? " handled" : "")}
          onClick={() => { haptic("light"); onSelect(it.message_id); }}
        >
          <div className="top">
            <span className="who">{it.channel_icon} {it.sender}</span>
            <span className="when">{timeAgo(it.at)}</span>
          </div>
          <div className="subj">
            <span className="source-label-sm">{it.channel_label}</span>
            {it.is_saved
              ? <span className="contact-badge-sm saved">Saved</span>
              : it.is_wa_contact
                ? <span className="contact-badge-sm wa">WhatsApp</span>
                : <span className="contact-badge-sm unsaved">Unsaved</span>
            }
            {it.subject ? <span className="subj-text"> {it.subject}</span> : null}
          </div>
          <div className="row-bottom">
            <span className={"pill " + (TIER_CLASS[it.label] || "")}>{it.label}</span>
            {it.via_label && (
              <span className="via-badge">{VIA_ICON[it.response_via] || ""} {it.via_label}</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function Queue({ items, selected, onSelect, summary }) {
  const waiting = items.filter(it => !it.response_via);
  const handled = items.filter(it => it.response_via);

  return (
    <div className="panel">
      <h2>Live queue</h2>
      <QueueSummary summary={summary} />
      <div className="queue">
        {items.length === 0 && (
          <div className="placeholder empty-welcome">
            <div className="empty-icon">🧭</div>
            <div className="empty-title">Steward is watching your inbox.</div>
            <div className="empty-sub">When an email or WhatsApp needs you, it appears here — already read, sorted, and drafted. Nothing needs you right now.</div>
          </div>
        )}
        <QueueSection title="Waiting for you" items={waiting} selected={selected} onSelect={onSelect} defaultOpen={true} />
        <QueueSection title="Already handled" items={handled} selected={selected} onSelect={onSelect} defaultOpen={false} />
      </div>
    </div>
  );
}

// ─── Detail (drill-down overlay on mobile, side-panel on desktop) ─────────────
function renderDraft(text) {
  const parts = (text || "").split(/(\[[^\]]+\])/g);
  return parts.map((p, i) =>
    /^\[[^\]]+\]$/.test(p) ? <span key={i} className="ph">{p}</span> : <span key={i}>{p}</span>
  );
}

function Detail({ detail, onAction, busy, onClose, className = "" }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [fbTier, setFbTier] = useState("");
  useEffect(() => { setEditing(false); setFbTier(""); }, [detail && detail.message_id]);

  const inner = !detail ? (
    <div className="placeholder">Pick an email to see what the AI did and why.</div>
  ) : (() => {
    const a = detail.arrived, ai = detail.ai, d = detail.draft;
    return (
      <div className="detail">
        <div className="section">
          <h3>What arrived</h3>
          <div className="kv" style={{ alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <b>{a.from}</b>
            {a.is_saved
              ? <span className="contact-badge saved">Saved{a.contact_note ? ` · ${a.contact_note}` : ""}</span>
              : a.is_wa_contact
                ? <span className="contact-badge wa">Seen on WhatsApp</span>
                : <span className="contact-badge unsaved">Unsaved{a.from_email ? ` · ${a.from_email}` : ""}</span>
            }
            <span className="muted">{timeAgo(a.at)}</span>
          </div>
          <div className="kv"><span className="source-label">{detail.channel_icon} {detail.channel_label}</span>{a.subject ? <span className="muted"> · {a.subject}</span> : null}</div>
          <div className="quote" style={{ marginTop: 8 }}>{a.quote || "(no preview)"}</div>
          {detail.source_link && detail.source_link.url && (
            <a className="source-jump" href={detail.source_link.url} target="_blank" rel="noopener noreferrer">
              ↗ {detail.source_link.label}
            </a>
          )}
        </div>

        <div className="section">
          <h3>What the AI figured out</h3>
          <div className="chips">
            <span className="chip">{ai.who_is_sender}</span>
            <span className="chip">{ai.urgency}</span>
            <span className="chip">{ai.undo}</span>
            <span className="chip">{ai.confidence}</span>
          </div>
          <div className="why">{ai.why}</div>
        </div>

        {detail.reasoning && <Reasoning r={detail.reasoning} />}

        {d && (
          <div className="section">
            <h3>Reply drafted in your voice</h3>
            {!editing ? (
              <>
                <div className="draft">{renderDraft(d.text)}</div>
                {d.placeholders.length > 0 && (
                  <div className="note">Amber bits need you to fill in before sending.</div>
                )}
                {d.decided ? (
                  <div className="note">Already handled.</div>
                ) : (
                  <>
                    <button className="btn-block" disabled={busy} onClick={() => { haptic("medium"); onAction("approve", d.action_id); }}>
                      ✅ Send this
                    </button>
                    <button className="btn-block ghost" disabled={busy} onClick={() => { setText(d.text); setEditing(true); }}>
                      ✏️ Edit first
                    </button>
                    <button className="btn-block warn" disabled={busy} onClick={() => { haptic("medium"); onAction("skip", d.action_id); }}>
                      ⏭ Skip
                    </button>
                  </>
                )}
              </>
            ) : (
              <>
                <textarea value={text} onChange={(e) => setText(e.target.value)} />
                <button className="btn-block" disabled={busy} onClick={() => { onAction("edit", d.action_id, text); setEditing(false); }}>
                  Save draft
                </button>
                <button className="btn-block ghost" onClick={() => setEditing(false)}>Cancel</button>
              </>
            )}
          </div>
        )}

        <div className="section">
          <h3>Was this the right call?</h3>
          <select value={fbTier} onChange={(e) => setFbTier(e.target.value)}>
            <option value="">Choose…</option>
            {detail.feedback.options.map((o, i) => (
              <option key={i} value={o.tier}>{o.label}</option>
            ))}
          </select>
          <div className="btns">
            <button className="btn" disabled={busy} onClick={() => onAction("feedback", detail.message_id, { tier: fbTier === "" ? null : Number(fbTier), thumbs: "up" })}>👍 Good</button>
            <button className="btn warn" disabled={busy} onClick={() => onAction("feedback", detail.message_id, { tier: fbTier === "" ? null : Number(fbTier), thumbs: "down" })}>👎 Off</button>
          </div>
          <div className="note">Teaches the brain — proposes a rule for your confirmation.</div>
        </div>
      </div>
    );
  })();

  // On mobile: full-screen overlay with back button
  // On desktop: rendered inline (CSS hides detail-header, makes overlay static)
  return (
    <div className={"detail-overlay" + (className ? " " + className : "")}>
      <div className="detail-header">
        <button className="back-btn" onClick={() => { haptic("light"); onClose(); }}>‹ Back</button>
        {detail && <p className="detail-title">{detail.label}</p>}
      </div>
      <div className="detail-scroll">{inner}</div>
    </div>
  );
}

// ─── Contacts ────────────────────────────────────────────────────────────────
function ContactRow({ c, onSave, busy }) {
  const [imp, setImp] = useState(c.importance);
  const [vip, setVip] = useState(c.is_vip);
  const dirty = imp !== c.importance || vip !== c.is_vip;
  const save = () => {
    const flags = new Set(c.flags || []);
    if (vip) flags.add("vip"); else flags.delete("vip");
    onSave(c.email, { importance: Number(imp), flags: [...flags] });
  };
  return (
    <tr>
      <td>
        <b>{c.name}</b>
        <div className="muted" style={{ fontSize: 11 }}>{c.email}</div>
        <div style={{ fontSize: 12, marginTop: 2 }}>{c.relationship || ""}</div>
      </td>
      <td style={{ textAlign: "center" }}>
        <label><input type="checkbox" checked={vip} onChange={(e) => setVip(e.target.checked)} /></label>
      </td>
      <td>
        <input type="number" min="0" max="100" value={imp} style={{ width: 56 }}
               onChange={(e) => setImp(e.target.value)} />
      </td>
      <td>{dirty && <button className="btn primary" disabled={busy} onClick={save}>Save</button>}</td>
    </tr>
  );
}

function People({ rows, onSave, busy, onSync }) {
  const [q, setQ] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState("");
  const filtered = rows.filter((c) =>
    !q || (c.name + " " + c.email).toLowerCase().includes(q.toLowerCase()));

  const doSync = async () => {
    setSyncing(true);
    setSyncMsg("");
    try {
      const r = await api.syncContacts();
      setSyncMsg(r.matched > 0 ? `Synced ${r.matched} contact(s) from your phone.` : "No new matches found.");
      if (onSync) onSync();
    } catch (_) {
      setSyncMsg("Sync failed.");
    } finally {
      setSyncing(false);
    }
  };

  return (
    <div className="panel">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 14px 0" }}>
        <h2 style={{ margin: 0 }}>People it knows</h2>
        <button className="btn" onClick={doSync} disabled={syncing} title="Match WhatsApp contacts against macOS Contacts app">
          {syncing ? "Syncing…" : "Sync from phone"}
        </button>
      </div>
      {syncMsg && <div className="note" style={{ padding: "4px 14px", color: "var(--green)" }}>{syncMsg}</div>}
      <div style={{ padding: "10px 14px" }}>
        <input placeholder="Search contacts…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <table>
        <thead><tr><th>Name</th><th>VIP</th><th>Importance</th><th></th></tr></thead>
        <tbody>
          {filtered.slice(0, 50).map((c) => (
            <ContactRow key={c.email} c={c} onSave={onSave} busy={busy} />
          ))}
        </tbody>
      </table>
      {filtered.length === 0 && <div className="placeholder">No contacts learned yet.</div>}
      {filtered.length > 50 && <div className="note" style={{ padding: "8px 14px" }}>Showing 50 of {filtered.length} — search to narrow.</div>}
    </div>
  );
}

// ─── Rules ────────────────────────────────────────────────────────────────────
function Rules({ rows, proposed, onRule, busy }) {
  return (
    <div className="panel">
      <h2>Rules</h2>
      <div style={{ padding: 14 }}>
        {proposed && proposed.length > 0 && (
          <div className="section">
            <h3>Proposed — need your OK</h3>
            {proposed.map((p) => (
              <div key={p.id} className="row" style={{ cursor: "default" }}>
                <div className="subj">{p.rule}</div>
                {p.evidence && <div className="muted">Why: {p.evidence}</div>}
                <div className="btns">
                  <button className="btn primary" disabled={busy} onClick={() => onRule("confirm", p.id)}>Confirm</button>
                  <button className="btn warn" disabled={busy} onClick={() => onRule("reject", p.id)}>Reject</button>
                </div>
              </div>
            ))}
          </div>
        )}
        <h3>Active rules</h3>
        <table>
          <thead><tr><th>Rule</th><th>Source</th><th>Status</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.id}>
              <td>{r.rule}</td>
              <td>{r.learned ? "Learned" : "You set"}</td>
              <td>{r.needs_confirm ? "⏳ Proposed" : r.status}</td>
            </tr>
          ))}</tbody>
        </table>
        {rows.length === 0 && <div className="placeholder">No rules yet.</div>}
      </div>
    </div>
  );
}

// ─── Audit ────────────────────────────────────────────────────────────────────
function Audit({ rows }) {
  return (
    <div className="panel">
      <h2>Everything it did today</h2>
      <table>
        <thead><tr><th>When</th><th>What</th><th>Detail</th></tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={i}>
            <td className="muted" style={{ whiteSpace: "nowrap" }}>{timeAgo(r.at)}</td>
            <td>{r.what}{r.was_dry_run ? " (dry)" : ""}</td>
            <td className="muted">{r.detail}</td>
          </tr>
        ))}</tbody>
      </table>
      {rows.length === 0 && <div className="placeholder">Nothing logged yet today.</div>}
    </div>
  );
}

// ─── Eval ─────────────────────────────────────────────────────────────────────
function Eval() {
  const [form, setForm] = useState({ sender: "", subject: "", body: "" });
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setRes(null);
    try {
      setRes(await api.testPipeline({ sender: form.sender, subject: form.subject, email_text: form.body }));
    } catch (e) { setRes({ error: String(e) }); }
    setBusy(false);
  };
  const clear = () => { setForm({ sender: "", subject: "", body: "" }); setRes(null); };
  return (
    <div className="panel">
      <h2>Test the brain</h2>
      <div style={{ padding: 14 }}>
        <div className="banner">⚗️ Test run — zero side effects.</div>
        <div style={{ marginTop: 10 }}>
          <input placeholder="From (name or email)" value={form.sender} onChange={(e) => setForm({ ...form, sender: e.target.value })} />
        </div>
        <div style={{ marginTop: 8 }}>
          <input placeholder="Subject" value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} />
        </div>
        <div style={{ marginTop: 8 }}>
          <textarea placeholder="Body of the email…" value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} />
        </div>
        <button className="btn-block" disabled={busy} onClick={run}>{busy ? "Thinking…" : "Run it through"}</button>
        <button className="btn-block ghost" onClick={clear}>Clear</button>
        {res && !res.error && (
          <div className="section" style={{ marginTop: 16 }}>
            <h3>It would have… <b>{res.final_label}</b></h3>
            <div className="chips">
              <span className="chip">tier {res.base_tier}→{res.final_tier}</span>
              <span className="chip">{res.category}</span>
              <span className="chip">{res.confidence}</span>
              {res.was_critical && <span className="chip">🔴 critical</span>}
            </div>
            {res.surfaced_reason && <div className="why">Surfaced: {res.surfaced_reason}</div>}
            {res.guardrail_floors && res.guardrail_floors.length > 0 && (
              <div className="note">Guardrails: {res.guardrail_floors.join("; ")}</div>
            )}
            {res.reasoning && <Reasoning r={res.reasoning} />}
          </div>
        )}
        {res && res.error && <div className="note">Error: {res.error}</div>}
      </div>
    </div>
  );
}

// ─── WhatsApp ─────────────────────────────────────────────────────────────────
function WhatsAppTab() {
  const [st, setSt] = useState(null);
  useEffect(() => {
    let on = true;
    const load = () => api.wastatus().then((d) => on && setSt(d)).catch(() => {});
    load(); const t = setInterval(load, 3000);
    return () => { on = false; clearInterval(t); };
  }, []);
  const wrap = (inner) => <div className="panel"><h2>WhatsApp</h2><div style={{ padding: 14 }}>{inner}</div></div>;
  if (!st) return wrap(<div className="placeholder">Checking…</div>);
  if (!st.enabled) return wrap(<div className="coming-soon">WhatsApp is off. Set <code>WHATSAPP_ENABLED=true</code> and restart.</div>);
  if (!st.running) return wrap(<div className="coming-soon">🔴 Relay not running. Start it:<br/><code>cd relay && node whatsapp_relay.js</code></div>);
  return wrap(
    <>
      <div className="chips">
        <span className="chip">{st.connected ? "🟢 Connected" : "🔴 Disconnected"}</span>
        <span className="chip">{Math.floor((st.session_age_seconds || 0) / 3600)}h session</span>
        <span className="chip">{st.messages_today || 0} messages today</span>
      </div>
      <div className="note" style={{ marginTop: 8 }}>
        WhatsApp messages appear in the Live queue with a 💬 icon.
        Personal contacts are always surfaced; groups are ignored unless they mention you.
      </div>
    </>
  );
}

// ─── Collapsible ──────────────────────────────────────────────────────────────
function Collapsible({ title, children, open }) {
  const [show, setShow] = useState(!!open);
  return (
    <div className="collapsible">
      <button className="collapsible-h" onClick={() => setShow(!show)}>
        {show ? "▾" : "▸"} {title}
      </button>
      {show && <div className="collapsible-b">{children}</div>}
    </div>
  );
}

// ─── Reasoning ────────────────────────────────────────────────────────────────
function Reasoning({ r }) {
  if (!r) return null;
  const pretty = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));
  return (
    <div className="section">
      <h3>How it decided {r.was_critical && <span className="chip">🔴 critical</span>}</h3>
      {r.think && <Collapsible title="1 · Think"><pre className="raw">{pretty(r.think)}</pre></Collapsible>}
      {r.judge && <Collapsible title="2 · Judge"><pre className="raw">{pretty(r.judge)}</pre></Collapsible>}
      {r.critique && (
        <Collapsible title={`3 · Self-critique (+${r.critique_adjustment || 0})`}>
          <pre className="raw">{pretty(r.critique)}</pre>
        </Collapsible>
      )}
      {!r.think && !r.judge && <div className="muted">No reasoning recorded.</div>}
    </div>
  );
}

// ─── Commitments ─────────────────────────────────────────────────────────────
function Commitments({ data, onAction, busy }) {
  if (!data) return <div className="panel"><h2>Commitments</h2><Skeleton rows={3} /></div>;
  const { open = [], stale = [] } = data;
  return (
    <div className="panel">
      <h2>Commitments</h2>
      <div style={{ padding: 14 }}>
        <h3 className="section" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".05em", color: "var(--muted)" }}>Open promises</h3>
        {open.length === 0 && <div className="placeholder" style={{ padding: "20px 0" }}>Nothing outstanding. 🎉</div>}
        {open.map((c) => (
          <div key={c.id} className="row" style={{ cursor: "default", borderRadius: 10, marginBottom: 8, border: "1px solid var(--border)" }}>
            <div className="subj">You promised: {c.promise}</div>
            <div className="muted">To {c.to}{c.due_date ? ` · due ${c.due_date}` : ""}</div>
            <button className="btn-block" disabled={busy} onClick={() => { haptic("medium"); onAction("done", c.id); }}>✅ Done</button>
            <button className="btn-block ghost" style={{ marginTop: 6 }} disabled={busy} onClick={() => onAction("snooze", c.id)}>⏰ Snooze 2d</button>
          </div>
        ))}
        <h3 style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".05em", color: "var(--muted)", marginTop: 16 }}>Stale threads</h3>
        {stale.length === 0 && <div className="muted">No threads have gone quiet.</div>}
        {stale.map((s, i) => (
          <div key={i} className="row" style={{ cursor: "default" }}>
            <div className="subj">{s.email}</div>
            <div className="muted">Quiet {s.days}d · {s.subject || "(no subject)"}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Voice ────────────────────────────────────────────────────────────────────
function Voice({ items, onRebuild, busy }) {
  if (!items) return <div className="panel"><h2>Voice profiles</h2><Skeleton rows={4} /></div>;
  const SEGS = ["investor", "customer", "team", "external"];
  const by = Object.fromEntries(items.map((p) => [p.segment, p]));
  return (
    <div className="panel">
      <h2>Voice profiles</h2>
      <div style={{ padding: 14 }}>
        <button className="btn-block" disabled={busy} onClick={onRebuild}>Rebuild now</button>
        {SEGS.map((seg) => {
          const p = by[seg];
          return (
            <div key={seg} className="section" style={{ marginTop: 16 }}>
              <h3>{seg} {p ? <span className="muted">· {p.sample_count} samples</span> : <span className="muted">· no profile yet</span>}</h3>
              {p ? (
                <>
                  <div className="why">{p.summary || "(thin — using global profile)"}</div>
                  {(p.examples || []).map((ex, i) => <div key={i} className="quote" style={{ marginTop: 6 }}>{ex}</div>)}
                </>
              ) : <div className="muted">Fewer than 5 samples — using your global voice.</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Mini bar charts ──────────────────────────────────────────────────────────
function MiniBars({ rows, fields, labels, colors }) {
  const max = Math.max(1, ...rows.map((r) => fields.reduce((a, f) => a + (r[f] || 0), 0)));
  return (
    <div className="chart">
      {rows.map((r, i) => (
        <div key={i} className="bar-col" title={r.day}>
          <div className="bar-stack" style={{ height: 120 }}>
            {fields.map((f, j) => {
              const h = ((r[f] || 0) / max) * 120;
              return <div key={f} className="bar-seg" style={{ height: h, background: colors[j] }} title={`${labels[j]}: ${r[f] || 0}`} />;
            })}
          </div>
          <div className="bar-x">{(r.day || "").slice(5)}</div>
        </div>
      ))}
      {rows.length === 0 && <div className="placeholder">No data yet.</div>}
    </div>
  );
}

// ─── Metrics ──────────────────────────────────────────────────────────────────
function Metrics({ data }) {
  if (!data) return <div className="panel"><h2>Metrics</h2><Skeleton rows={4} /></div>;
  const { daily = [], accuracy = {}, costs = [], rt = {} } = data;
  const rtRow = (k, label) => {
    const v = rt[k] || {};
    return <span className="chip">{label}: {v.p50 || 0}ms / {v.p95 || 0}ms</span>;
  };
  const totalCost = costs.reduce((a, c) => a + (c.cost || 0), 0);
  return (
    <div className="panel">
      <h2>Metrics</h2>
      <div style={{ padding: 14 }}>
        <div className="section">
          <h3>Volume by tier (last 30 days)</h3>
          <MiniBars rows={daily} fields={["t0","t1","t2","t3"]} labels={["Filed","FYI","Draft","Ask"]} colors={["#9ca3af","#3b82f6","#eab308","#ef4444"]} />
        </div>
        <div className="section">
          <h3>Handled vs surfaced</h3>
          <MiniBars rows={daily} fields={["handled","surfaced"]} labels={["Handled","Surfaced"]} colors={["#22c55e","#f59e0b"]} />
        </div>
        <div className="section">
          <h3>Your feedback</h3>
          <div className="chips">
            <span className="chip">Approve {accuracy.approve || 0}</span>
            <span className="chip">Edit {accuracy.edit || 0}</span>
            <span className="chip">Skip {accuracy.skip || 0}</span>
            <span className="chip">{Math.round((accuracy.approval_rate || 0) * 100)}% approved</span>
          </div>
        </div>
        <div className="section">
          <h3>Response times (p50/p95)</h3>
          <div className="chips">
            {rtRow("email_to_notification", "Email→notify")}
            {rtRow("draft_generation", "Draft")}
          </div>
        </div>
        <div className="section">
          <h3>LLM cost — last 30 days (${totalCost.toFixed(3)})</h3>
          <table>
            <thead><tr><th>Task</th><th>Model</th><th>Calls</th><th>Cost</th></tr></thead>
            <tbody>{costs.map((c, i) => (
              <tr key={i}><td>{c.task}</td><td className="muted">{c.model}</td><td>{c.calls}</td><td>${(c.cost || 0).toFixed(4)}</td></tr>
            ))}</tbody>
          </table>
          {costs.length === 0 && <div className="placeholder">No LLM calls yet.</div>}
        </div>
      </div>
    </div>
  );
}

// ─── More menu ────────────────────────────────────────────────────────────────
function MoreMenu({ setTab }) {
  return (
    <div className="panel">
      <h2>More</h2>
      {MORE_TABS.map((t) => (
        <div key={t.id} className="row" onClick={() => { haptic("light"); setTab(t.id); }}>
          <div className="subj" style={{ color: "var(--text)", fontSize: 15, fontWeight: 500 }}>{t.label}</div>
        </div>
      ))}
    </div>
  );
}

// ─── Skeleton ─────────────────────────────────────────────────────────────────
function Skeleton({ rows = 3 }) {
  return (
    <div style={{ padding: "0 14px 14px" }}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton-row" />
      ))}
    </div>
  );
}

// ─── Teach Steward (plain-English rules / commands) ───────────────────────────
function Teach({ onRulesSaved }) {
  const [text, setText] = useState("");
  const [reply, setReply] = useState(null); // null = no reply yet
  const [busy, setBusy] = useState(false);
  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setReply(null);
    try {
      const r = await api.command(text.trim());
      const msg = r.reply || "";
      const lo = msg.toLowerCase();
      const saved = msg && (lo.startsWith("got it") || lo.startsWith("paused") || lo.startsWith("resumed") || lo.startsWith("done") || lo.startsWith("ok,") || lo.startsWith("skipping") || lo.startsWith("sent"));
      setReply({ text: msg, ok: saved });
      setText("");
      if (saved && onRulesSaved) onRulesSaved();
    } catch (_) {
      setReply({ text: "Something went wrong — try again.", ok: false });
    }
    setBusy(false);
  };
  const onKey = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  return (
    <div className="teach">
      <textarea
        className="teach-input"
        rows={3}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        placeholder={'Tell Steward in plain English — e.g. "never reply to my landlord without me", "treat Acme as high importance".\n\nPress Enter to send.'}
      />
      <button className="btn" disabled={busy || !text.trim()} onClick={send}>{busy ? "Thinking…" : "Tell Steward"}</button>
      {reply && (
        <div className={`teach-reply ${reply.ok ? "teach-reply-ok" : "teach-reply-err"}`}>
          {reply.ok ? "✓ " : "⚠ "}{reply.text}
        </div>
      )}
    </div>
  );
}

// ─── Settings (folds WhatsApp, Rules, Voice + Teach + Test into one screen) ────
function Settings({ rules, proposed, onRule, voice, onVoiceRebuild, busy, onRulesSaved }) {
  return (
    <div className="settings">
      <h3 className="section-h">Teach Steward</h3>
      <Teach onRulesSaved={onRulesSaved} />
      <h3 className="section-h">Test the pipeline (zero side effects)</h3>
      <Eval />
      <h3 className="section-h">WhatsApp</h3>
      <WhatsAppTab />
      <h3 className="section-h">Learned rules</h3>
      <Rules rows={rules} proposed={proposed} onRule={onRule} busy={busy} />
      <h3 className="section-h">Voice</h3>
      <Voice items={voice} onRebuild={onVoiceRebuild} busy={busy} />
    </div>
  );
}

// ─── Root app ─────────────────────────────────────────────────────────────────
export default function App() {
  const [theme, setTheme] = useTheme();
  const [status, setStatus] = useState(null);
  const [stats, setStats] = useState(null);
  const [pipeline, setPipeline] = useState(null);
  const [queue, setQueue] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [showDetail, setShowDetail] = useState(false);
  const [tab, setTab] = useState("queue");
  const [people, setPeople] = useState([]);
  const [rules, setRules] = useState([]);
  const [proposed, setProposed] = useState([]);
  const [audit, setAudit] = useState([]);
  const [commitments, setCommitments] = useState(null);
  const [voice, setVoice] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState("");

  const [lastSync, setLastSync] = useState(0);
  const [syncing, setSyncing] = useState(false);

  const [notifications, setNotifications] = useState(null);
  const [queueSummary, setQueueSummary] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [s, st, p, q, no, qs] = await Promise.all([
        api.status(), api.stats(), api.pipeline(), api.queue(), api.notifications(), api.queueSummary(),
      ]);
      setStatus(s); setStats(st); setPipeline(p); setQueue(q.items); setNotifications(no); setQueueSummary(qs);
    } catch (_) {}
  }, []);

  const clearNotifications = useCallback(async () => {
    try { await api.notificationsClear(); haptic(); await refresh(); } catch (_) {}
  }, [refresh]);

  // One-time Telegram Mini App init (theme + back-button wiring live below).
  useEffect(() => {
    initTelegramApp();
    if (isTelegramMiniApp()) authenticateWithTelegram("/api");
  }, []);

  // Telegram back button — close detail overlay
  useEffect(() => {
    if (!isTelegramMiniApp()) return;
    const tg = window.Telegram.WebApp;
    if (showDetail) {
      tg.BackButton.show();
      tg.BackButton.onClick(() => setShowDetail(false));
    } else {
      tg.BackButton.hide();
    }
    return () => tg.BackButton.offClick(() => setShowDetail(false));
  }, [showDetail]);

  const loadDetail = useCallback(async (id) => {
    setSelected(id);
    setDetail(null);
    setShowDetail(true);
    try { setDetail(await api.queueDetail(id)); }
    catch (_) { try { setDetail(await api.email(id)); } catch (__) { setDetail(null); } }
  }, []);

  const loadTab = useCallback(async (t) => {
    try {
      if (t === "people") setPeople((await api.contacts()).items);
      else if (t === "commitments") setCommitments(await api.commitments());
      else if (t === "settings") {
        const [r, pr, v] = await Promise.all([
          api.rules(), api.rulesProposed(), api.voiceProfiles(),
        ]);
        setRules(r.items); setProposed(pr.items); setVoice(v.items);
      }
    } catch (_) {}
  }, []);

  useEffect(() => { loadTab(tab); }, [tab, loadTab]);

  // Live sync: each tick refreshes the global overview AND the active tab's data, so the
  // whole screen stays current — not just the queue. Drives the manual Refresh button and
  // fires immediately when the window/app regains focus (feels instant after switching apps).
  const tabRef = useRef(tab);
  useEffect(() => { tabRef.current = tab; }, [tab]);
  const syncNow = useCallback(async () => {
    setSyncing(true);
    try {
      await Promise.all([refresh(), loadTab(tabRef.current)]);
      setLastSync(Math.floor(Date.now() / 1000));
    } finally { setSyncing(false); }
  }, [refresh, loadTab]);

  // Manual "fetch everything": force the engine to pull email + WhatsApp NOW (not just
  // re-read the DB), then re-render. New items land within a few seconds as the brain
  // processes them (the 3s auto-refresh picks them up). The header button uses this.
  const fetchEverything = useCallback(async () => {
    setSyncing(true);
    try { await api.fetchNow(); } catch (_) {}
    try {
      await Promise.all([refresh(), loadTab(tabRef.current)]);
      setLastSync(Math.floor(Date.now() / 1000));
    } finally { setSyncing(false); }
  }, [refresh, loadTab]);
  useEffect(() => {
    syncNow();
    const t = setInterval(syncNow, 3000);
    const onFocus = () => { if (!document.hidden) syncNow(); };
    document.addEventListener("visibilitychange", onFocus);
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onFocus);
      window.removeEventListener("focus", onFocus);
    };
  }, [syncNow]);

  const flash = (m) => { setToast(m); setTimeout(() => setToast(""), 3000); };

  const onAction = async (kind, id, extra) => {
    setBusy(true);
    try {
      if (kind === "approve") {
        const r = await api.approve(id);
        flash(r.result === "sent" ? (r.dry_run ? "Sent (dry-run)" : "Sent ✓") : r.result === "already" ? "Already handled" : "Couldn't send");
      } else if (kind === "skip") {
        const r = await api.skip(id);
        flash(r.ok ? (r.proposal ? "Skipped · rule proposed" : "Skipped") : r.reason);
      } else if (kind === "edit") {
        const r = await api.edit(id, extra);
        flash(r.ok ? "Draft updated" : r.reason);
      } else if (kind === "feedback") {
        const r = await api.feedback(id, extra.tier, extra.thumbs);
        flash(r.proposal ? "Thanks · rule proposed" : "Thanks — noted");
      }
      if (selected) await loadDetail(selected);
      await refresh();
    } catch (_) { flash("Something went wrong"); }
    setBusy(false);
  };

  const onCommitment = async (kind, id) => {
    setBusy(true);
    setCommitments((c) => c ? { ...c, open: c.open.filter((x) => x.id !== id) } : c);
    try {
      if (kind === "done") await api.commitmentDone(id); else await api.commitmentSnooze(id);
      flash(kind === "done" ? "Marked done" : "Snoozed 2 days");
    } catch (_) { flash("Failed — reloading"); }
    await loadTab("commitments"); setBusy(false);
  };

  const onRule = async (kind, id) => {
    setBusy(true);
    setProposed((p) => p.filter((x) => x.id !== id));
    try {
      kind === "confirm" ? await api.ruleConfirm(id) : await api.ruleReject(id);
      flash(kind === "confirm" ? "Rule confirmed" : "Rule rejected");
    } catch (_) { flash("Failed"); }
    await loadTab("rules"); setBusy(false);
  };

  const onContactSave = async (email, body) => {
    setBusy(true);
    try { await api.contactUpdate(email, body); flash("Saved"); } catch (_) { flash("Failed"); }
    await loadTab("people"); setBusy(false);
  };

  const onVoiceRebuild = async () => {
    setBusy(true);
    try { const r = await api.voiceRebuild(); flash(r.dry_run ? "Dry-run — would rebuild" : "Rebuilt ✓"); }
    catch (_) { flash("Failed"); }
    await loadTab("voice"); setBusy(false);
  };

  const switchTab = (id) => { setTab(id); setShowDetail(false); };

  return (
    <div className="app">
      <Header status={status} theme={theme} setTheme={setTheme}
              lastSync={lastSync} syncing={syncing} onRefresh={fetchEverything} />

      <div className="scroll-body">
        <DesktopTabs tab={tab} setTab={switchTab} />
        <Connections status={status} />
        <Pipeline pipeline={pipeline} />
        <Stats stats={stats} />
        {tab === "queue" && <ActivityFeed data={notifications} onClear={clearNotifications} />}

        {tab === "queue" && (
          <div className="panes">
            <Queue items={queue} selected={selected} onSelect={loadDetail} summary={queueSummary} />
            <Detail
              detail={detail}
              onAction={onAction}
              busy={busy}
              onClose={() => setShowDetail(false)}
              className={showDetail ? "show" : ""}
            />
          </div>
        )}
        {tab === "commitments" && <Commitments data={commitments} onAction={onCommitment} busy={busy} />}
        {tab === "people" && <People rows={people} onSave={onContactSave} busy={busy} onSync={() => loadTab("people")} />}
        {tab === "settings" && (
          <Settings rules={rules} proposed={proposed} onRule={onRule}
                    voice={voice} onVoiceRebuild={onVoiceRebuild} busy={busy}
                    onRulesSaved={() => loadTab("settings")} />
        )}
      </div>

      <BottomNav tab={tab} setTab={switchTab} />

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
