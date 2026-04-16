"""
Web Dashboard -- Sprint 4 of FlowDelta.

A lightweight FastAPI web application that serves an interactive
delta visualisation dashboard. Features:

* **Flow list** -- all recorded flows with run counts
* **Trace viewer** -- snapshot-by-snapshot state changes for any run
* **Delta diff view** -- side-by-side variable changes with colour coding
* **Trend chart** -- Chart.js line chart of change volume over time
* **Invariant panel** -- list of detected invariants for a flow
* **REST API** -- JSON endpoints for all data (consumable by external tools)

The dashboard runs entirely in-process (no database server needed) and
reads directly from the :class:`DeltaStore`.

Usage::

    from src.observability import DeltaDashboard
    from src.delta_engine import DeltaStore

    store = DeltaStore(store_path=".flowdelta/runs")
    dashboard = DeltaDashboard(store)
    dashboard.run(host="127.0.0.1", port=8765)
    # -> open http://localhost:8765

Or from the CLI::

    flowdelta dashboard --port 8765

Requires ``fastapi`` + ``uvicorn``::

    pip install "flowdelta[dashboard]"
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..delta_engine.delta_store import DeltaStore
from .trend_chart import TrendChartGenerator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Inline HTML / CSS / JS for the dashboard (single-file, no build step)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>FlowDelta Dashboard</title>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&amp;family=JetBrains+Mono:wght@400;500&amp;display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body, #root { height: 100%; }
    body {
      background: #0d0f18;
      color: #dde1f5;
      font-family: Inter, system-ui, -apple-system, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #2a2f52; border-radius: 99px; }
    ::-webkit-scrollbar-thumb:hover { background: #4a5280; }

    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes shimmer {
      from { background-position: -600px 0; }
      to   { background-position:  600px 0; }
    }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(12px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes slideIn {
      from { transform: translateX(100%); opacity: 0; }
      to   { transform: translateX(0);    opacity: 1; }
    }
    .fade-up { animation: fadeUp 0.24s ease both; }

    #boot {
      position: fixed; inset: 0;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      background: #0d0f18; color: #4a5280; gap: 16px; font-size: 14px;
      font-family: Inter, system-ui, sans-serif;
    }
    .spin-ring {
      width: 38px; height: 38px;
      border: 3px solid #2a2f52; border-top-color: #6c8bff;
      border-radius: 50%; animation: spin 0.75s linear infinite;
    }
    .fd-tr-hover { transition: background 0.12s; cursor: pointer; }
    .val-pre {
      background: #09090f; border: 1px solid #2a2f52;
      border-radius: 6px; padding: 6px 10px;
      font-family: 'JetBrains Mono', monospace; font-size: 12px;
      color: #8892c0; white-space: pre-wrap; word-break: break-all;
      max-width: 230px; max-height: 80px; overflow: auto; margin: 0;
    }
    .val-pre.is-new { color: #34d399; }
  </style>
</head>
<body>
  <div id="root">
    <div id="boot">
      <div class="spin-ring"></div>
      <span>Initialising FlowDelta...</span>
    </div>
  </div>

  <script type="text/babel">
    const { useState, useEffect, useRef, useCallback } = React;

    /* --- Colour tokens ------------------------------------ */
    const C = {
      bg:      "#0d0f18",
      sidebar: "#0a0c1e",
      surface: "#13162a",
      s2:      "#1a1e36",
      border:  "#2a2f52",
      primary: "#6c8bff",
      accent:  "#a78bfa",
      success: "#34d399",
      danger:  "#f87171",
      warn:    "#fbbf24",
      muted:   "#4a5280",
      soft:    "#8892c0",
      text:    "#dde1f5",
    };

    /* --- API helper --------------------------------------- */
    async function apiFetch(path) {
      const r = await fetch(path);
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    }

    /* --- Utilities ---------------------------------------- */
    const fmt = iso => iso ? iso.slice(0, 19).replace("T", " ") : "--";

    const shortVal = v => {
      if (v === null || v === undefined) return "null";
      const s = typeof v === "string" ? v : JSON.stringify(v, null, 2);
      return s.length > 200 ? s.slice(0, 200) + "..." : s;
    };

    /* Badge config - ASCII-only icons */
    const BADGE_CFG = {
      added:        { color: "#34d399", bg: "rgba(52,211,153,.13)",  icon: "[+]" },
      removed:      { color: "#f87171", bg: "rgba(248,113,113,.13)", icon: "[-]" },
      changed:      { color: "#6c8bff", bg: "rgba(108,139,255,.15)", icon: "[~]" },
      type_changed: { color: "#a78bfa", bg: "rgba(167,139,250,.15)", icon: "[T]" },
      golden:       { color: "#fbbf24", bg: "rgba(251,191,36,.14)",  icon: "[G]" },
    };

    /* --- Badge -------------------------------------------- */
    function Badge({ type }) {
      const b = BADGE_CFG[type] || { color: C.soft, bg: "rgba(100,100,100,.1)", icon: "" };
      return (
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          padding: "3px 9px", borderRadius: 99,
          fontSize: 12, fontWeight: 600,
          color: b.color, background: b.bg,
        }}>
          <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{b.icon}</span>
          {" "}{type.replace("_", " ")}
        </span>
      );
    }

    /* --- StatCard ----------------------------------------- */
    function StatCard({ label2, value, label, accent = false, onClick }) {
      const [hov, setHov] = useState(false);
      const active = hov && !!onClick;
      return (
        <div
          onClick={onClick}
          onMouseEnter={() => setHov(true)}
          onMouseLeave={() => setHov(false)}
          style={{
            background: C.surface,
            border: "1px solid " + (active ? C.primary : C.border),
            borderRadius: 12, padding: "18px 20px",
            display: "flex", flexDirection: "column", gap: 7,
            transition: "border-color .18s, box-shadow .18s, transform .12s",
            boxShadow: active ? "0 0 0 1px " + C.primary + "40" : "none",
            cursor: onClick ? "pointer" : "default",
            transform: active ? "translateY(-2px)" : "none",
            position: "relative",
          }}
        >
          <span style={{
            fontSize: 11, fontWeight: 700, textTransform: "uppercase",
            letterSpacing: "0.07em", color: C.muted,
          }}>
            {label2}
          </span>
          <span style={{
            fontSize: 30, fontWeight: 800, lineHeight: 1,
            color: accent ? C.accent : C.primary,
            letterSpacing: "-0.02em",
          }}>
            {value}
          </span>
          <span style={{
            fontSize: 11, fontWeight: 500, color: C.muted,
          }}>
            {label}
          </span>
          {onClick && (
            <span style={{
              position: "absolute", top: 10, right: 12,
              fontSize: 10, color: active ? C.primary : C.muted,
              fontFamily: "monospace", transition: "color .15s",
            }}>details &rsaquo;</span>
          )}
        </div>
      );
    }

    /* --- StatDrawer --------------------------------------- */
    function StatDrawer({ title, badge2, onClose, children }) {
      useEffect(() => {
        const onKey = e => e.key === "Escape" && onClose();
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
      }, [onClose]);
      return (
        <>
          {/* overlay */}
          <div
            onClick={onClose}
            style={{
              position: "fixed", inset: 0, background: "rgba(0,0,0,.55)",
              zIndex: 100, backdropFilter: "blur(2px)",
            }}
          />
          {/* panel */}
          <div style={{
            position: "fixed", top: 0, right: 0, bottom: 0, width: 480,
            background: C.sidebar, borderLeft: "1px solid " + C.border,
            zIndex: 101, display: "flex", flexDirection: "column",
            boxShadow: "-12px 0 48px rgba(0,0,0,.5)",
            animation: "slideIn .22s cubic-bezier(.22,.61,.36,1)",
          }}>
            {/* drawer header */}
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "18px 22px", borderBottom: "1px solid " + C.border, flexShrink: 0,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {badge2 && (
                  <span style={{
                    background: C.s2, border: "1px solid " + C.border,
                    borderRadius: 5, padding: "2px 8px",
                    fontSize: 11, fontFamily: "monospace", color: C.primary,
                  }}>{badge2}</span>
                )}
                <span style={{ fontWeight: 700, fontSize: 15, color: C.text }}>{title}</span>
              </div>
              <button
                onClick={onClose}
                style={{
                  background: C.s2, border: "1px solid " + C.border,
                  color: C.soft, borderRadius: 7, padding: "5px 12px",
                  cursor: "pointer", fontSize: 13,
                }}
              >x close</button>
            </div>
            {/* drawer body */}
            <div style={{ flex: 1, overflowY: "auto", padding: "20px 22px" }}>
              {children}
            </div>
          </div>
        </>
      );
    }

    /* --- Skeleton loader ---------------------------------- */
    function Skel({ w = "100%", h = 14, r = 6, style = {} }) {
      return (
        <div style={{
          width: w, height: h, borderRadius: r,
          background: "linear-gradient(90deg, " + C.surface + " 25%, " + C.s2 + " 50%, " + C.surface + " 75%)",
          backgroundSize: "600px 100%", animation: "shimmer 1.4s infinite",
          ...style,
        }} />
      );
    }

    /* --- Empty state -------------------------------------- */
    function EmptyState({ symbol = "~", title, sub }) {
      return (
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", gap: 14, minHeight: 340, color: C.muted,
        }}>
          <div style={{
            width: 72, height: 72, borderRadius: 18,
            background: "rgba(108,139,255,.08)", border: "1px solid " + C.border,
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 30, fontWeight: 700, color: C.muted, fontFamily: "monospace",
          }}>
            {symbol}
          </div>
          <div style={{ fontSize: 17, fontWeight: 700, color: C.soft }}>{title}</div>
          {sub && <div style={{ fontSize: 14, textAlign: "center", maxWidth: 380, lineHeight: 1.7 }}>{sub}</div>}
        </div>
      );
    }

    /* --- Panel card --------------------------------------- */
    function Panel({ title, badge2, right, noPad = false, children }) {
      return (
        <div style={{
          background: C.surface, border: "1px solid " + C.border,
          borderRadius: 12, overflow: "hidden",
        }}>
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "13px 20px", borderBottom: "1px solid " + C.border,
          }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 10,
              fontSize: 12, fontWeight: 700, textTransform: "uppercase",
              letterSpacing: "0.08em", color: C.muted,
            }}>
              {badge2 && (
                <span style={{
                  background: C.s2, border: "1px solid " + C.border,
                  borderRadius: 5, padding: "2px 7px",
                  fontSize: 11, fontFamily: "monospace", color: C.primary,
                }}>
                  {badge2}
                </span>
              )}
              {title}
            </div>
            {right && <div style={{ fontSize: 12, color: C.muted }}>{right}</div>}
          </div>
          {noPad ? children : <div style={{ padding: "16px 20px" }}>{children}</div>}
        </div>
      );
    }

    /* --- Chart.js wrapper --------------------------------- */
    function TrendChart({ points }) {
      const canvasRef = useRef(null);
      const chartRef  = useRef(null);

      useEffect(() => {
        if (!canvasRef.current) return;
        if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
        if (!points || points.length === 0) return;

        chartRef.current = new Chart(canvasRef.current.getContext("2d"), {
          type: "line",
          data: {
            labels: points.map(p => p.run_id.slice(0, 7)),
            datasets: [{
              label: "Changes",
              data:  points.map(p => p.total_changes),
              borderColor:          C.primary,
              backgroundColor:      "rgba(108,139,255,0.1)",
              pointBackgroundColor: points.map(p => p.golden ? C.warn : C.primary),
              pointBorderColor:     points.map(p => p.golden ? C.warn : C.primary),
              pointRadius: 6, pointHoverRadius: 9,
              tension: 0.4, fill: true, borderWidth: 2,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
              legend: { display: false },
              tooltip: {
                backgroundColor: "#1a1e36", borderColor: C.border, borderWidth: 1,
                titleColor: C.text, bodyColor: C.soft, padding: 12,
                callbacks: {
                  title: i => "Run " + i[0].label,
                  label: i => "  " + i.parsed.y + " change" + (i.parsed.y !== 1 ? "s" : ""),
                  afterBody: i => points[i[0].dataIndex].golden ? ["  [G] golden baseline"] : [],
                },
              },
            },
            scales: {
              x: {
                grid:  { color: "rgba(42,47,82,0.5)" },
                ticks: { color: C.muted, font: { size: 11 } },
              },
              y: {
                beginAtZero: true,
                grid:  { color: "rgba(42,47,82,0.5)" },
                ticks: { color: C.muted, font: { size: 11 }, precision: 0 },
              },
            },
          },
        });

        return () => {
          if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
        };
      }, [points]);

      if (!points || points.length === 0) {
        return (
          <div style={{ textAlign: "center", color: C.muted, padding: "40px 0", fontSize: 14 }}>
            No trend data yet. Run the pipeline more times to populate this chart.
          </div>
        );
      }
      return <canvas ref={canvasRef} style={{ height: 240 }} />;
    }

    /* --- Table header cell -------------------------------- */
    const TH = ({ children }) => (
      <th style={{
        padding: "10px 18px", textAlign: "left",
        fontSize: 11, fontWeight: 700, textTransform: "uppercase",
        letterSpacing: "0.07em", color: C.muted,
        borderBottom: "1px solid " + C.border, whiteSpace: "nowrap",
        background: C.bg,
      }}>
        {children}
      </th>
    );

    /* --- Table data cell ---------------------------------- */
    const TD = ({ children, mono = false, last = false, style = {} }) => (
      <td style={{
        padding: "11px 18px",
        borderBottom: last ? "none" : "1px solid " + C.border,
        fontFamily: mono ? "'JetBrains Mono', monospace" : "inherit",
        fontSize: mono ? 13 : 14,
        verticalAlign: "top",
        ...style,
      }}>
        {children}
      </td>
    );

    /* --- Run ID chip -------------------------------------- */
    const RunChip = ({ id }) => (
      <span style={{
        fontFamily: "'JetBrains Mono', monospace", fontSize: 13,
        color: C.primary, background: "rgba(108,139,255,.1)",
        padding: "3px 9px", borderRadius: 6,
      }}>
        {id}
      </span>
    );

    /* --- Back button -------------------------------------- */
    function BackBtn({ onClick, label }) {
      const [hov, setHov] = useState(false);
      return (
        <button
          onClick={onClick}
          onMouseEnter={() => setHov(true)}
          onMouseLeave={() => setHov(false)}
          style={{
            display: "flex", alignItems: "center", gap: 6,
            background: hov ? C.border : C.s2,
            border: "1px solid " + C.border,
            color: hov ? C.text : C.soft,
            padding: "8px 16px", borderRadius: 8,
            cursor: "pointer", fontSize: 13, transition: "all .15s",
          }}
        >
          {"< Back to " + label}
        </button>
      );
    }

    /* --- Sidebar ------------------------------------------ */
    function Sidebar({ flows, selectedFlow, onSelect, storePath, loading, activePage, onPageSelect }) {
      return (
        <aside style={{
          width: 260, flexShrink: 0, background: C.sidebar,
          borderRight: "1px solid " + C.border,
          display: "flex", flexDirection: "column",
          height: "100vh", overflow: "hidden",
        }}>
          {/* Logo */}
          <div style={{
            display: "flex", alignItems: "center", gap: 12,
            padding: "20px 18px 16px", borderBottom: "1px solid " + C.border,
          }}>
            <div style={{
              width: 38, height: 38, borderRadius: 10, flexShrink: 0,
              background: "linear-gradient(135deg, #6c8bff, #a78bfa)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 18, fontWeight: 800, color: "#fff",
              fontFamily: "monospace", userSelect: "none",
            }}>
              FD
            </div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 15, color: C.text }}>FlowDelta</div>
              <div style={{ fontSize: 11, color: C.muted, letterSpacing: "0.04em" }}>
                State-delta engine
              </div>
            </div>
          </div>

          {/* Tools nav */}
          <div style={{ padding: "14px 10px 6px" }}>
            <div style={{
              padding: "4px 8px 8px",
              fontSize: 11, fontWeight: 700, textTransform: "uppercase",
              letterSpacing: "0.09em", color: C.muted,
            }}>Tools</div>
            {[{ id: "probe", label: "URL Analyser", badge: "NEW" }].map(item => {
              const isActive = activePage === item.id;
              return (
                <div
                  key={item.id}
                  onClick={() => onPageSelect(item.id)}
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "9px 12px 9px " + (isActive ? "9px" : "12px"),
                    borderRadius: 8, cursor: "pointer", marginBottom: 2,
                    fontSize: 14,
                    color:      isActive ? C.primary : C.soft,
                    background: isActive
                      ? "linear-gradient(90deg, rgba(108,139,255,.16), rgba(167,139,250,.06))"
                      : "transparent",
                    borderLeft: "3px solid " + (isActive ? C.primary : "transparent"),
                    transition: "all .15s",
                  }}
                >
                  <span>{item.label}</span>
                  {item.badge && (
                    <span style={{
                      background: "rgba(108,139,255,.2)", color: C.primary,
                      fontSize: 10, padding: "1px 6px", borderRadius: 99, fontWeight: 700,
                    }}>{item.badge}</span>
                  )}
                </div>
              );
            })}
          </div>

          {/* Divider */}
          <div style={{ height: 1, background: C.border, margin: "0 18px" }} />

          {/* Section label */}
          <div style={{
            padding: "14px 18px 8px",
            fontSize: 11, fontWeight: 700, textTransform: "uppercase",
            letterSpacing: "0.09em", color: C.muted,
          }}>
            Flows
          </div>

          {/* Flow list */}
          <div style={{ flex: 1, overflowY: "auto", padding: "0 10px 10px" }}>
            {loading ? (
              <div style={{ padding: "4px 8px", display: "flex", flexDirection: "column", gap: 9 }}>
                <Skel w="80%" /><Skel w="62%" /><Skel w="74%" />
              </div>
            ) : flows.length === 0 ? (
              <div style={{ padding: "8px", fontSize: 13, color: C.muted }}>
                No flows recorded yet. Run the pipeline first.
              </div>
            ) : flows.map(f => {
              const active = activePage !== "probe" && selectedFlow === f.flow_id;
              return (
                <div key={f.flow_id}
                  onClick={() => onSelect(f.flow_id)}
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "9px 12px 9px " + (active ? "9px" : "12px"),
                    borderRadius: 8, cursor: "pointer", marginBottom: 2,
                    fontSize: 14,
                    color:      active ? C.primary : C.soft,
                    background: active
                      ? "linear-gradient(90deg, rgba(108,139,255,.16), rgba(167,139,250,.06))"
                      : "transparent",
                    borderLeft: "3px solid " + (active ? C.primary : "transparent"),
                    transition: "all .15s",
                  }}
                >
                  <span>{f.flow_id}</span>
                  <span style={{
                    background: active ? "rgba(108,139,255,.2)" : C.s2,
                    color: active ? C.primary : C.muted,
                    fontSize: 11, padding: "2px 7px", borderRadius: 99,
                  }}>
                    {f.run_count}
                  </span>
                </div>
              );
            })}
          </div>

          {/* Footer */}
          <div style={{
            padding: "12px 18px", borderTop: "1px solid " + C.border,
            fontSize: 12, color: C.muted,
            display: "flex", alignItems: "center", gap: 8,
          }}>
            <span style={{
              width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
              background: C.success, boxShadow: "0 0 6px " + C.success,
            }} />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {storePath || ".flowdelta/runs"}
            </span>
          </div>
        </aside>
      );
    }

    /* --- Welcome screen ----------------------------------- */
    function WelcomeScreen() {
      return (
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", height: "100%", gap: 20, color: C.muted,
        }}>
          <div style={{
            width: 90, height: 90, borderRadius: 22,
            background: "rgba(108,139,255,.08)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 36, fontWeight: 800, color: C.primary,
            fontFamily: "monospace", border: "1px solid " + C.border,
          }}>
            FD
          </div>
          <div style={{ fontSize: 22, fontWeight: 700, color: C.soft }}>
            Welcome to FlowDelta
          </div>
          <div style={{ fontSize: 14, textAlign: "center", maxWidth: 380, lineHeight: 1.8, color: C.muted }}>
            Select a flow from the sidebar to explore its delta history,
            regression trends, and variable-level change timeline.
          </div>
        </div>
      );
    }

    /* --- Inline bar ---------------------------------------- */
    function MiniBar({ value, max, color }) {
      const pct = max > 0 ? Math.round((value / max) * 100) : 0;
      return (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            flex: 1, height: 8, borderRadius: 99,
            background: C.s2, overflow: "hidden",
          }}>
            <div style={{
              height: "100%", width: pct + "%",
              background: color || C.primary,
              borderRadius: 99, transition: "width .4s ease",
            }} />
          </div>
          <span style={{
            fontSize: 13, fontWeight: 700, color: C.text,
            minWidth: 28, textAlign: "right",
          }}>{value}</span>
        </div>
      );
    }

    /* --- Flow overview ------------------------------------ */
    function FlowView({ flowId, onSelectRun }) {
      const [s, setS]           = useState({ loading: true, data: null, error: null });
      const [activeCard, setActiveCard] = useState(null);

      useEffect(() => {
        setS({ loading: true, data: null, error: null });
        Promise.all([
          apiFetch("/api/flows/" + flowId + "/runs"),
          apiFetch("/api/flows/" + flowId + "/trend"),
        ])
          .then(([runs, trend]) => setS({ loading: false, data: { runs, trend }, error: null }))
          .catch(e => setS({ loading: false, data: null, error: e.message }));
      }, [flowId]);

      if (s.loading) return (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Skel h={30} w="40%" r={8} />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 12 }}>
            {[...Array(5)].map((_, i) => <Skel key={i} h={100} r={12} />)}
          </div>
          <Skel h={290} r={12} />
          <Skel h={220} r={12} />
        </div>
      );

      if (s.error) return <EmptyState symbol="!" title="Failed to load flow" sub={s.error} />;

      const pts    = s.data.trend.points || [];
      const rl     = s.data.runs.runs    || [];
      const total  = pts.reduce((acc, p) => acc + p.total_changes, 0);
      const avgNum = pts.length ? total / pts.length : 0;
      const avg    = avgNum.toFixed(1);
      const goldenRuns = rl.filter(r => r.golden);
      const golden = goldenRuns.length;
      const peak   = pts.length ? Math.max(...pts.map(p => p.total_changes)) : 0;
      const peakRun = pts.find(p => p.total_changes === peak);
      const maxChg = peak;

      /* --- Drawer content builders ----------------------- */
      const DrawerRuns = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <p style={{ fontSize: 13, color: C.muted, marginBottom: 12, lineHeight: 1.7 }}>
            All {rl.length} recorded run{rl.length !== 1 ? "s" : ""} for the <strong style={{ color: C.text }}>{flowId}</strong> flow, ordered by timestamp.
          </p>
          {rl.map((r, i) => {
            const chg = pts.find(p => p.run_id === r.run_id)?.total_changes ?? null;
            return (
              <div key={r.run_id} style={{
                background: C.surface, border: "1px solid " + C.border,
                borderRadius: 10, padding: "12px 16px",
                display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
              }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{
                      fontFamily: "monospace", fontSize: 13, color: C.primary,
                      background: "rgba(108,139,255,.1)", padding: "2px 8px", borderRadius: 5,
                    }}>#{i + 1} {r.run_id}</span>
                    {r.golden && <Badge type="golden" />}
                  </div>
                  <span style={{ fontSize: 12, color: C.muted }}>{fmt(r.saved_at)}</span>
                </div>
                <div style={{ textAlign: "right" }}>
                  {chg !== null
                    ? <span style={{ fontSize: 16, fontWeight: 700, color: chg > 0 ? C.primary : C.muted }}>{chg}</span>
                    : <span style={{ fontSize: 12, color: C.muted }}>no delta</span>}
                  <div style={{ fontSize: 11, color: C.muted }}>changes</div>
                </div>
              </div>
            );
          })}
        </div>
      );

      const DrawerAvg = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{
            background: C.surface, border: "1px solid " + C.border,
            borderRadius: 10, padding: "16px 20px",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
            <span style={{ fontSize: 13, color: C.muted }}>Average changes per run</span>
            <span style={{ fontSize: 28, fontWeight: 800, color: C.primary }}>{avg}</span>
          </div>
          <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.7 }}>
            Changes per run compared to the average ({avg}).
            Bars above average indicate regressions or more complex state changes.
          </p>
          {pts.map(p => (
            <div key={p.run_id} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontFamily: "monospace", fontSize: 12, color: C.soft }}>{p.run_id}</span>
                <span style={{
                  fontSize: 11,
                  color: p.total_changes > avgNum ? C.warn : C.success,
                  fontWeight: 600,
                }}>
                  {p.total_changes > avgNum ? "above avg" : p.total_changes < avgNum ? "below avg" : "at avg"}
                </span>
              </div>
              <MiniBar
                value={p.total_changes}
                max={maxChg || 1}
                color={p.total_changes > avgNum ? C.warn : C.success}
              />
              {/* avg reference line */}
              <div style={{ position: "relative", height: 1, marginTop: -6 }}>
                <div style={{
                  position: "absolute",
                  left: Math.round((avgNum / (maxChg || 1)) * 100) + "%",
                  top: 0, width: 1, height: 8,
                  background: C.muted, opacity: 0.6,
                }} />
              </div>
            </div>
          ))}
          {pts.length === 0 && <div style={{ color: C.muted, fontSize: 13 }}>No trend data yet.</div>}
        </div>
      );

      const DrawerGolden = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <p style={{ fontSize: 13, color: C.muted, marginBottom: 8, lineHeight: 1.7 }}>
            Golden runs are canonical baselines. Future runs are compared against the most recent golden run to detect regressions.
          </p>
          {goldenRuns.length === 0 && (
            <div style={{ color: C.muted, fontSize: 13 }}>No golden runs recorded yet.</div>
          )}
          {goldenRuns.map((r, i) => (
            <div key={r.run_id} style={{
              background: "rgba(251,191,36,.06)", border: "1px solid rgba(251,191,36,.25)",
              borderRadius: 10, padding: "14px 18px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                <span style={{
                  fontFamily: "monospace", fontSize: 13, color: C.warn,
                  background: "rgba(251,191,36,.12)", padding: "2px 8px", borderRadius: 5,
                }}>baseline #{i + 1}</span>
                <span style={{
                  fontFamily: "monospace", fontSize: 13, color: C.text,
                }}>{r.run_id}</span>
              </div>
              <div style={{ fontSize: 12, color: C.muted, marginBottom: 4 }}>Recorded: {fmt(r.saved_at)}</div>
              {i === goldenRuns.length - 1 && (
                <div style={{
                  marginTop: 8, fontSize: 12, color: C.success, fontWeight: 600,
                }}>Active baseline - new runs compare against this</div>
              )}
            </div>
          ))}
        </div>
      );

      /* Build change-type totals across all pts */
      const DrawerTotal = () => {
        const [detail, setDetail] = useState(null);
        const typeTotals = {};
        pts.forEach(p => {
          Object.entries(p.change_counts || {}).forEach(([t, n]) => {
            typeTotals[t] = (typeTotals[t] || 0) + n;
          });
        });
        const hasBreakdown = Object.keys(typeTotals).length > 0;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{
              background: C.surface, border: "1px solid " + C.border,
              borderRadius: 10, padding: "16px 20px",
              display: "flex", alignItems: "center", justifyContent: "space-between",
            }}>
              <span style={{ fontSize: 13, color: C.muted }}>Total state changes across all runs</span>
              <span style={{ fontSize: 28, fontWeight: 800, color: C.primary }}>{total}</span>
            </div>
            {hasBreakdown && (
              <>
                <p style={{ fontSize: 13, color: C.muted }}>Change type breakdown across all runs:</p>
                {Object.entries(typeTotals).map(([t, n]) => (
                  <div key={t} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <Badge type={t} />
                      <span style={{ fontSize: 12, color: C.muted }}>{Math.round(n / total * 100)}%</span>
                    </div>
                    <MiniBar value={n} max={total} color={BADGE_CFG[t]?.color || C.primary} />
                  </div>
                ))}
              </>
            )}
            <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.7, marginTop: 4 }}>
              Per-run breakdown:
            </p>
            {pts.map(p => (
              <div key={p.run_id} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <span style={{ fontFamily: "monospace", fontSize: 12, color: C.soft }}>{p.run_id}</span>
                  {p.golden && <Badge type="golden" />}
                </div>
                <MiniBar value={p.total_changes} max={maxChg || 1} color={C.primary} />
              </div>
            ))}
          </div>
        );
      };

      const DrawerPeak = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{
            background: "rgba(167,139,250,.08)", border: "1px solid rgba(167,139,250,.3)",
            borderRadius: 10, padding: "16px 20px",
          }}>
            <div style={{ fontSize: 12, color: C.muted, marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.07em" }}>Peak run</div>
            {peakRun ? (
              <>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                  <span style={{
                    fontFamily: "monospace", fontSize: 14, color: C.accent,
                    background: "rgba(167,139,250,.12)", padding: "3px 10px", borderRadius: 6,
                  }}>{peakRun.run_id}</span>
                  {peakRun.golden && <Badge type="golden" />}
                </div>
                <span style={{ fontSize: 32, fontWeight: 800, color: C.accent }}>{peak}</span>
                <span style={{ fontSize: 13, color: C.muted, marginLeft: 8 }}>state changes</span>
              </>
            ) : (
              <span style={{ color: C.muted, fontSize: 13 }}>No data yet.</span>
            )}
          </div>
          <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.7 }}>
            The peak run had the most state changes in a single execution.
            This often indicates a complex or edge-case code path was exercised.
          </p>
          <p style={{ fontSize: 13, color: C.muted, marginBottom: 6 }}>All runs vs peak ({peak}):</p>
          {pts.map(p => (
            <div key={p.run_id} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontFamily: "monospace", fontSize: 12, color: p.run_id === peakRun?.run_id ? C.accent : C.soft }}>
                  {p.run_id}{p.run_id === peakRun?.run_id ? " [peak]" : ""}
                </span>
                <span style={{ fontSize: 11, color: C.muted }}>{p.total_changes} chg</span>
              </div>
              <MiniBar
                value={p.total_changes}
                max={maxChg || 1}
                color={p.run_id === peakRun?.run_id ? C.accent : C.primary}
              />
            </div>
          ))}
        </div>
      );

      const DRAWERS = {
        runs:    { title: "All Runs",           badge2: "RUNS",   content: <DrawerRuns /> },
        avg:     { title: "Average Changes",    badge2: "AVG",    content: <DrawerAvg /> },
        golden:  { title: "Golden Baselines",   badge2: "GOLDEN", content: <DrawerGolden /> },
        total:   { title: "Total Changes",      badge2: "TOTAL",  content: <DrawerTotal /> },
        peak:    { title: "Peak Run",           badge2: "PEAK",   content: <DrawerPeak /> },
      };

      return (
        <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          {/* Drawer */}
          {activeCard && (
            <StatDrawer
              title={DRAWERS[activeCard].title}
              badge2={DRAWERS[activeCard].badge2}
              onClose={() => setActiveCard(null)}
            >
              {DRAWERS[activeCard].content}
            </StatDrawer>
          )}

          {/* Header */}
          <div style={{ paddingBottom: 18, borderBottom: "1px solid " + C.border }}>
            <div style={{ fontSize: 23, fontWeight: 800, letterSpacing: "-0.02em" }}>{flowId}</div>
            <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>
              Flow overview &middot; {rl.length} run{rl.length !== 1 ? "s" : ""}
              <span style={{ marginLeft: 10, color: C.border, fontSize: 11 }}>Click any tile for details</span>
            </div>
          </div>

          {/* Stats grid */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(130px,1fr))", gap: 12 }}>
            <StatCard label2="Runs"          value={rl.length} label="recorded"   onClick={() => setActiveCard("runs")} />
            <StatCard label2="Avg Changes"   value={avg}       label="per run"    onClick={() => setActiveCard("avg")} />
            <StatCard label2="Golden Runs"   value={golden}    label="baselines"  onClick={() => setActiveCard("golden")} />
            <StatCard label2="Total Changes" value={total}     label="all runs"   onClick={() => setActiveCard("total")} />
            <StatCard label2="Peak"          value={peak}      label="single run" onClick={() => setActiveCard("peak")} accent />
          </div>

          {/* Trend chart */}
          <Panel title="Regression Trend" badge2="CHART">
            <div style={{ height: 260 }}>
              <TrendChart points={pts} />
            </div>
          </Panel>

          {/* Run history table */}
          <Panel title="Run History" badge2="TABLE" right="Click a row to inspect deltas" noPad>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr><TH>Run ID</TH><TH>Timestamp</TH><TH>Changes</TH><TH>Flag</TH></tr>
                </thead>
                <tbody>
                  {rl.length === 0 ? (
                    <tr>
                      <td colSpan={4} style={{ textAlign: "center", color: C.muted, padding: "2rem" }}>
                        No runs recorded yet.
                      </td>
                    </tr>
                  ) : rl.map((r, i) => {
                    const chg  = pts.find(p => p.run_id === r.run_id)?.total_changes ?? "--";
                    const last = i === rl.length - 1;
                    return (
                      <tr key={r.run_id}
                        className="fd-tr-hover"
                        onClick={() => onSelectRun(r.run_id)}
                        onMouseEnter={e => e.currentTarget.style.background = "rgba(108,139,255,.06)"}
                        onMouseLeave={e => e.currentTarget.style.background = "transparent"}
                      >
                        <TD last={last}><RunChip id={r.run_id} /></TD>
                        <TD last={last} style={{ color: C.soft }}>{fmt(r.saved_at)}</TD>
                        <TD last={last} style={{
                          fontWeight: 600,
                          color: typeof chg === "number" && chg > 0 ? C.primary : C.muted,
                        }}>
                          {typeof chg === "number" ? chg + " change" + (chg !== 1 ? "s" : "") : chg}
                        </TD>
                        <TD last={last}>
                          {r.golden
                            ? <Badge type="golden" />
                            : <span style={{ color: C.muted }}>--</span>}
                        </TD>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>
      );
    }

    /* --- Run detail --------------------------------------- */
    function RunDetailView({ flowId, runId, onBack }) {
      const [s, setS] = useState({ loading: true, data: null, error: null });

      useEffect(() => {
        setS({ loading: true, data: null, error: null });
        apiFetch("/api/runs/" + runId + "/delta")
          .then(d  => setS({ loading: false, data: d,    error: null }))
          .catch(e => setS({ loading: false, data: null, error: e.message }));
      }, [runId]);

      if (s.loading) return (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Skel h={32} w="50%" r={8} />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Skel h={100} r={12} /><Skel h={100} r={12} />
          </div>
          <Skel h={400} r={12} />
        </div>
      );

      if (s.error) return <EmptyState symbol="!" title="Could not load run" sub={s.error} />;

      /* Golden baseline run - no delta was computed (nothing to compare against) */
      if (s.data.note === "no_delta") return (
        <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            paddingBottom: 18, borderBottom: "1px solid " + C.border,
          }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 20, fontWeight: 800 }}>Run</span>
                <RunChip id={runId} />
                <Badge type="golden" />
              </div>
              <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>Flow: {flowId}</div>
            </div>
            <BackBtn onClick={onBack} label={flowId} />
          </div>
          <div style={{
            display: "flex", flexDirection: "column", alignItems: "center",
            justifyContent: "center", gap: 16, padding: "48px 20px",
            background: C.surface, border: "1px solid " + C.border,
            borderRadius: 12,
          }}>
            <div style={{
              width: 64, height: 64, borderRadius: 16,
              background: "rgba(251,191,36,.12)", border: "1px solid rgba(251,191,36,.3)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 24, fontWeight: 800, color: C.warn, fontFamily: "monospace",
            }}>G</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: C.soft }}>Golden Baseline Run</div>
            <div style={{ fontSize: 14, color: C.muted, textAlign: "center", maxWidth: 420, lineHeight: 1.8 }}>
              This is the first recorded run for this flow and serves as the golden baseline.
              No delta is available because there was no prior run to compare against.
              Future runs will be compared to this baseline.
            </div>
          </div>
        </div>
      );

      const deltas   = s.data.deltas || [];
      const totalChg = deltas.reduce((acc, d) => acc + d.changes.length, 0);

      const typeCts = {};
      deltas.forEach(d => d.changes.forEach(c => {
        typeCts[c.change_type] = (typeCts[c.change_type] || 0) + 1;
      }));

      const rows = [];
      deltas.forEach((d, di) => {
        if (!d.changes.length) return;
        const from = (d.from_location || "").includes("(")
          ? d.from_location.split("(").pop().replace(")", "") : d.from_location || "?";
        const to = (d.to_location || "").includes("(")
          ? d.to_location.split("(").pop().replace(")", "") : d.to_location || "?";

        rows.push(
          <tr key={"sep-" + di} style={{ background: "rgba(9,9,15,0.8)" }}>
            <td colSpan={5} style={{
              padding: "6px 18px", fontSize: 12, color: C.muted,
              borderTop: di > 0 ? "1px solid " + C.border : "none",
            }}>
              <span style={{ fontFamily: "monospace", color: C.accent }}>{from}</span>
              <span style={{ margin: "0 10px", color: C.border }}>{"--->"}</span>
              <span style={{ fontFamily: "monospace", color: C.accent }}>{to}</span>
              <span style={{ marginLeft: 10, color: C.muted, fontSize: 11 }}>
                {"[ transition " + (di + 1) + " ]"}
              </span>
            </td>
          </tr>
        );

        d.changes.forEach((c, ci) => {
          const last = di === deltas.length - 1 && ci === d.changes.length - 1;
          rows.push(
            <tr key={di + "-" + ci}
              className="fd-tr-hover"
              onMouseEnter={e => e.currentTarget.style.background = "rgba(108,139,255,.05)"}
              onMouseLeave={e => e.currentTarget.style.background = "transparent"}
            >
              <TD mono last={last} style={{ color: C.primary }}>{c.name}</TD>
              <TD last={last}><Badge type={c.change_type} /></TD>
              <TD mono last={last} style={{ color: C.accent, fontSize: 12 }}>{to}</TD>
              <TD last={last}><pre className="val-pre">{shortVal(c.old_value)}</pre></TD>
              <TD last={last}><pre className="val-pre is-new">{shortVal(c.new_value)}</pre></TD>
            </tr>
          );
        });
      });

      return (
        <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          {/* Header */}
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            paddingBottom: 18, borderBottom: "1px solid " + C.border,
          }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 20, fontWeight: 800 }}>Run</span>
                <RunChip id={runId} />
              </div>
              <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>Flow: {flowId}</div>
            </div>
            <BackBtn onClick={onBack} label={flowId} />
          </div>

          {/* Stats */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(130px,1fr))", gap: 12 }}>
            <StatCard label2="Transitions" value={deltas.length} label="state transitions" />
            <StatCard label2="Changes"     value={totalChg}      label="variable changes" accent />
          </div>

          {/* Breakdown */}
          {Object.keys(typeCts).length > 0 && (
            <div style={{
              display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
              background: C.surface, border: "1px solid " + C.border,
              borderRadius: 12, padding: "12px 20px",
            }}>
              <span style={{
                fontSize: 11, fontWeight: 700, textTransform: "uppercase",
                letterSpacing: "0.08em", color: C.muted,
              }}>
                Breakdown
              </span>
              {Object.entries(typeCts).map(([t, n]) => (
                <div key={t} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ fontWeight: 700, fontSize: 15, color: C.text }}>{n}x</span>
                  <Badge type={t} />
                </div>
              ))}
            </div>
          )}

          {/* Delta table */}
          <Panel
            title="State Delta Timeline"
            badge2="DELTA"
            right={totalChg + " variable change" + (totalChg !== 1 ? "s" : "")}
            noPad
          >
            {rows.length === 0 ? (
              <div style={{ textAlign: "center", color: C.muted, padding: "2rem" }}>
                No state changes recorded for this run.
              </div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      <TH>Variable</TH><TH>Change Type</TH><TH>Function</TH>
                      <TH>Old Value</TH><TH>New Value</TH>
                    </tr>
                  </thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      );
    }

    /* --- Root App ----------------------------------------- */
    function App() {
      const [flows,     setFlows]     = useState([]);
      const [sideLoad,  setSideLoad]  = useState(true);
      const [storePath, setStorePath] = useState("");
      const [nav, setNav] = useState({ view: "welcome", flowId: null, runId: null });

      useEffect(() => {
        Promise.all([
          apiFetch("/api/flows"),
          apiFetch("/api/health").catch(() => ({})),
        ]).then(([f, h]) => {
          const fl = f.flows || [];
          setFlows(fl);
          setStorePath(h.store || "");
          setSideLoad(false);
          if (fl.length > 0) setNav({ view: "flow", flowId: fl[0].flow_id, runId: null });
        }).catch(() => setSideLoad(false));
      }, []);

      const onSelectFlow = useCallback(id => {
        setNav({ view: "flow", flowId: id, runId: null });
      }, []);

      const onSelectRun = useCallback(id => {
        setNav(prev => ({ ...prev, view: "run", runId: id }));
      }, []);

      const onBack = useCallback(() => {
        setNav(prev => ({ ...prev, view: "flow", runId: null }));
      }, []);

      const onPageSelect = useCallback(pageId => {
        setNav(prev => ({ ...prev, view: pageId, runId: null }));
      }, []);

      /* activePage for sidebar highlight */
      const activePage = nav.view === "probe" ? "probe" : null;

      return (
        <div style={{
          display: "flex", minHeight: "100vh",
          background: C.bg, color: C.text,
        }}>
          <Sidebar
            flows={flows}
            selectedFlow={nav.flowId}
            onSelect={onSelectFlow}
            storePath={storePath}
            loading={sideLoad}
            activePage={activePage}
            onPageSelect={onPageSelect}
          />
          <main style={{ flex: 1, overflowY: "auto", height: "100vh", padding: "30px 34px" }}>
            {nav.view === "welcome" && <WelcomeScreen />}
            {nav.view === "probe"   && <ProbeView />}
            {nav.view === "flow" && nav.flowId && (
              <FlowView key={nav.flowId} flowId={nav.flowId} onSelectRun={onSelectRun} />
            )}
            {nav.view === "run" && nav.runId && (
              <RunDetailView
                key={nav.runId}
                flowId={nav.flowId}
                runId={nav.runId}
                onBack={onBack}
              />
            )}
          </main>
        </div>
      );
    }

    /* =============================================================
       PROBE VIEW - analyse any URL
       ============================================================= */
    const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];

    const STATUS_COLOR = code => {
      if (!code) return C.muted;
      if (code < 300) return C.success;
      if (code < 400) return C.warn;
      if (code < 500) return C.danger;
      return "#ff6b9d";
    };

    function StatusBadge({ code }) {
      if (!code) return null;
      const color = STATUS_COLOR(code);
      const label = code < 200 ? "info" : code < 300 ? "ok" : code < 400 ? "redirect" : code < 500 ? "client err" : "server err";
      return (
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          padding: "4px 12px", borderRadius: 99,
          fontSize: 13, fontWeight: 700,
          color, background: color + "22", border: "1px solid " + color + "55",
        }}>
          <span style={{ fontFamily: "monospace", fontSize: 16 }}>{code}</span>
          <span style={{ fontWeight: 500, fontSize: 11 }}>{label}</span>
        </span>
      );
    }

    function SecHeaderRow({ name, info }) {
      return (
        <tr>
          <td style={{ padding: "8px 14px", fontFamily: "monospace", fontSize: 12, color: C.soft, borderBottom: "1px solid " + C.border }}>{name}</td>
          <td style={{ padding: "8px 14px", borderBottom: "1px solid " + C.border }}>
            {info.present
              ? <span style={{ color: C.success, fontSize: 12, fontWeight: 600 }}>present</span>
              : <span style={{ color: C.danger,  fontSize: 12, fontWeight: 600 }}>missing</span>}
          </td>
          <td style={{ padding: "8px 14px", fontFamily: "monospace", fontSize: 11, color: C.muted, borderBottom: "1px solid " + C.border, maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {info.value || "--"}
          </td>
        </tr>
      );
    }

    function JsonViewer({ data, depth = 0 }) {
      const [collapsed, setCollapsed] = useState(depth > 1);
      if (data === null)      return <span style={{ color: C.muted }}>null</span>;
      if (data === undefined) return <span style={{ color: C.muted }}>undefined</span>;
      if (typeof data === "boolean") return <span style={{ color: C.accent }}>{String(data)}</span>;
      if (typeof data === "number")  return <span style={{ color: C.warn }}>{data}</span>;
      if (typeof data === "string")  return <span style={{ color: C.success }}>"{data.length > 120 ? data.slice(0, 120) + "..." : data}"</span>;
      if (Array.isArray(data)) {
        if (data.length === 0) return <span style={{ color: C.muted }}>[]</span>;
        return (
          <span>
            <span style={{ color: C.primary, cursor: "pointer", userSelect: "none" }} onClick={() => setCollapsed(c => !c)}>
              {collapsed ? "[+" + data.length + "]" : "["}
            </span>
            {!collapsed && (
              <div style={{ paddingLeft: 18, borderLeft: "1px solid " + C.border }}>
                {data.slice(0, 50).map((v, i) => (
                  <div key={i} style={{ lineHeight: 1.8 }}>
                    <span style={{ color: C.muted, fontSize: 11 }}>{i}: </span>
                    <JsonViewer data={v} depth={depth + 1} />
                    {i < data.length - 1 && <span style={{ color: C.muted }}>,</span>}
                  </div>
                ))}
                {data.length > 50 && <div style={{ color: C.muted, fontSize: 11 }}>... {data.length - 50} more</div>}
              </div>
            )}
            {!collapsed && <span style={{ color: C.primary }}>]</span>}
          </span>
        );
      }
      if (typeof data === "object") {
        const keys = Object.keys(data);
        if (keys.length === 0) return <span style={{ color: C.muted }}>{"{}"}</span>;
        return (
          <span>
            <span style={{ color: C.primary, cursor: "pointer", userSelect: "none" }} onClick={() => setCollapsed(c => !c)}>
              {collapsed ? "{+" + keys.length + "}" : "{"}
            </span>
            {!collapsed && (
              <div style={{ paddingLeft: 18, borderLeft: "1px solid " + C.border }}>
                {keys.slice(0, 80).map((k, i) => (
                  <div key={k} style={{ lineHeight: 1.8 }}>
                    <span style={{ color: C.accent }}>"{k}"</span>
                    <span style={{ color: C.muted }}>: </span>
                    <JsonViewer data={data[k]} depth={depth + 1} />
                    {i < keys.length - 1 && <span style={{ color: C.muted }}>,</span>}
                  </div>
                ))}
                {keys.length > 80 && <div style={{ color: C.muted, fontSize: 11 }}>... {keys.length - 80} more keys</div>}
              </div>
            )}
            {!collapsed && <span style={{ color: C.primary }}>{"}"}</span>}
          </span>
        );
      }
      return <span style={{ color: C.muted }}>{String(data)}</span>;
    }

    function ProbeView() {
      const [url, setUrl]         = useState("https://");
      const [method, setMethod]   = useState("GET");
      const [headersRaw, setHeadersRaw] = useState("");
      const [bodyRaw, setBodyRaw] = useState("");
      const [loading, setLoading] = useState(false);
      const [result, setResult]   = useState(null);
      const [history, setHistory] = useState([]);
      const [activeTab, setActiveTab] = useState("response");
      const [showAdvanced, setShowAdvanced] = useState(false);

      // load probe history on mount
      useEffect(() => {
        apiFetch("/api/probe/history").then(d => setHistory(d.probes || [])).catch(() => {});
      }, []);

      const run = async () => {
        if (!url || url === "https://") return;
        setLoading(true);
        setResult(null);
        try {
          let extraHeaders = {};
          if (headersRaw.trim()) {
            headersRaw.trim().split("\\n").forEach(line => {
              const idx = line.indexOf(":");
              if (idx > 0) extraHeaders[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
            });
          }
          const r = await fetch("/api/probe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, method, headers: extraHeaders, body: bodyRaw || null }),
          });
          const d = await r.json();
          setResult(d);
          setActiveTab("response");
          // refresh history
          apiFetch("/api/probe/history").then(h => setHistory(h.probes || [])).catch(() => {});
        } catch (e) {
          setResult({ ok: false, error: e.message, url });
        }
        setLoading(false);
      };

      const onKey = e => e.key === "Enter" && !loading && run();

      return (
        <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          {/* Header */}
          <div style={{ paddingBottom: 18, borderBottom: "1px solid " + C.border }}>
            <div style={{ fontSize: 23, fontWeight: 800 }}>URL Analyser</div>
            <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>
              Probe any URL - inspect response, headers, JSON structure, security posture, and delta changes.
            </div>
          </div>

          {/* Input bar */}
          <div style={{
            background: C.surface, border: "1px solid " + C.border,
            borderRadius: 14, padding: "18px 20px",
            display: "flex", flexDirection: "column", gap: 14,
          }}>
            {/* Method + URL row */}
            <div style={{ display: "flex", gap: 10 }}>
              <select
                value={method}
                onChange={e => setMethod(e.target.value)}
                style={{
                  background: C.s2, border: "1px solid " + C.border, color: C.primary,
                  borderRadius: 9, padding: "10px 14px", fontSize: 13, fontWeight: 700,
                  fontFamily: "monospace", cursor: "pointer", flexShrink: 0,
                }}
              >
                {METHODS.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
              <input
                value={url}
                onChange={e => setUrl(e.target.value)}
                onBlur={e => {
                  const v = e.target.value.trim();
                  if (v && !v.startsWith("http://") && !v.startsWith("https://")) {
                    setUrl("https://" + v);
                  }
                }}
                onKeyDown={onKey}
                placeholder="amazon.in or https://api.example.com/endpoint"
                style={{
                  flex: 1, background: C.s2, border: "1px solid " + C.border,
                  color: C.text, borderRadius: 9, padding: "10px 16px",
                  fontSize: 14, fontFamily: "monospace", outline: "none",
                }}
              />
              <button
                onClick={run}
                disabled={loading}
                style={{
                  background: loading ? C.muted : "linear-gradient(135deg, #6c8bff, #a78bfa)",
                  border: "none", color: "#fff", fontWeight: 700,
                  borderRadius: 9, padding: "10px 24px", fontSize: 14,
                  cursor: loading ? "not-allowed" : "pointer", flexShrink: 0,
                  transition: "opacity .15s",
                  opacity: loading ? 0.7 : 1,
                }}
              >
                {loading ? "Probing..." : "Analyze"}
              </button>
            </div>

            {/* Advanced toggle */}
            <button
              onClick={() => setShowAdvanced(a => !a)}
              style={{
                background: "none", border: "none", color: C.muted,
                fontSize: 12, cursor: "pointer", textAlign: "left", padding: 0,
              }}
            >
              {showAdvanced ? "- Hide" : "+ Show"} custom headers / request body
            </button>
            {showAdvanced && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <label style={{ fontSize: 11, color: C.muted, fontWeight: 600, textTransform: "uppercase" }}>
                    Extra Headers (one per line, Key: Value)
                  </label>
                  <textarea
                    value={headersRaw}
                    onChange={e => setHeadersRaw(e.target.value)}
                    placeholder={"Authorization: Bearer token\\nX-Custom: value"}
                    rows={4}
                    style={{
                      background: C.s2, border: "1px solid " + C.border, color: C.text,
                      borderRadius: 8, padding: "10px 12px", fontSize: 12,
                      fontFamily: "monospace", resize: "vertical", outline: "none",
                    }}
                  />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <label style={{ fontSize: 11, color: C.muted, fontWeight: 600, textTransform: "uppercase" }}>
                    Request Body (for POST/PUT/PATCH)
                  </label>
                  <textarea
                    value={bodyRaw}
                    onChange={e => setBodyRaw(e.target.value)}
                    placeholder={'{"key": "value"}'}
                    rows={4}
                    style={{
                      background: C.s2, border: "1px solid " + C.border, color: C.text,
                      borderRadius: 8, padding: "10px 12px", fontSize: 12,
                      fontFamily: "monospace", resize: "vertical", outline: "none",
                    }}
                  />
                </div>
              </div>
            )}
          </div>

          {/* Recent probes */}
          {history.length > 0 && !result && (
            <Panel title="Recent Probes" badge2="HISTORY">
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {history.map(h => (
                  <div key={h.url} style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    background: C.s2, borderRadius: 8, padding: "8px 14px", cursor: "pointer",
                  }} onClick={() => setUrl(h.url)}>
                    <span style={{ fontFamily: "monospace", fontSize: 13, color: C.primary }}>{h.url}</span>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <StatusBadge code={h.status} />
                      <span style={{ fontSize: 11, color: C.muted }}>{h.elapsed_ms}ms</span>
                      <span style={{ fontSize: 11, color: C.muted }}>{fmt(h.probed_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </Panel>
          )}

          {/* Loading skeleton */}
          {loading && (
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
                {[...Array(4)].map((_, i) => <Skel key={i} h={90} r={12} />)}
              </div>
              <Skel h={300} r={12} />
              <Skel h={220} r={12} />
            </div>
          )}

          {/* Error */}
          {result && !result.ok && (
            <div style={{
              background: "rgba(248,113,113,.08)", border: "1px solid rgba(248,113,113,.3)",
              borderRadius: 12, padding: "20px 24px",
            }}>
              <div style={{ fontSize: 15, fontWeight: 700, color: C.danger, marginBottom: 6 }}>
                Request Failed
                {result.error_type && (
                  <span style={{
                    marginLeft: 10, fontSize: 12, fontWeight: 600,
                    background: "rgba(248,113,113,.2)", padding: "2px 8px", borderRadius: 6,
                    fontFamily: "monospace",
                  }}>{result.error_type}</span>
                )}
              </div>
              <div style={{ fontSize: 13, color: C.muted, marginBottom: 8 }}>
                <strong style={{ color: C.soft }}>URL: </strong>
                <span style={{ fontFamily: "monospace" }}>{result.url}</span>
              </div>
              <pre style={{ fontFamily: "monospace", fontSize: 13, color: C.soft, whiteSpace: "pre-wrap",
                background: "rgba(0,0,0,.3)", borderRadius: 8, padding: "12px 14px" }}>
                {result.error || "Connection failed. The server may have refused the connection, be unreachable, or blocking automated requests."}
              </pre>
              <div style={{ marginTop: 12, fontSize: 12, color: C.muted, lineHeight: 1.7 }}>
                <strong>Common causes:</strong> connection refused, DNS failure, SSL error, server is blocking scrapers, or no network path to this host.
              </div>
            </div>
          )}

          {/* Results */}
          {result && result.ok && (() => {
            const secPresent = Object.values(result.security_headers || {}).filter(h => h.present).length;
            const secTotal   = Object.keys(result.security_headers || {}).length;
            const secScore   = secTotal > 0 ? Math.round((secPresent / secTotal) * 100) : 0;
            const secColor   = secScore >= 70 ? C.success : secScore >= 40 ? C.warn : C.danger;
            const hasDelta   = result.delta_from_prev && (
              result.delta_from_prev.added.length +
              result.delta_from_prev.removed.length +
              result.delta_from_prev.changed.length > 0
            );
            const TABS = [
              { k: "response", label: "Response" },
              { k: "headers",  label: "Headers (" + Object.keys(result.headers || {}).length + ")" },
              { k: "security", label: "Security (" + secPresent + "/" + secTotal + ")" },
              ...(result.is_json ? [{ k: "json", label: "JSON Explorer" }] : []),
              ...(hasDelta ? [{ k: "delta", label: "Delta [!]" }] : []),
            ];
            return (
              <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
                {/* Summary cards */}
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(140px,1fr))", gap: 12 }}>
                  <div style={{ background: C.surface, border: "1px solid " + C.border, borderRadius: 12, padding: "16px 18px" }}>
                    <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700, marginBottom: 8 }}>Status</div>
                    <StatusBadge code={result.status} />
                  </div>
                  <div style={{ background: C.surface, border: "1px solid " + C.border, borderRadius: 12, padding: "16px 18px" }}>
                    <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700, marginBottom: 4 }}>Response Time</div>
                    <div style={{ fontSize: 26, fontWeight: 800, color: result.elapsed_ms < 200 ? C.success : result.elapsed_ms < 800 ? C.warn : C.danger }}>
                      {result.elapsed_ms}
                    </div>
                    <div style={{ fontSize: 11, color: C.muted }}>ms</div>
                  </div>
                  <div style={{ background: C.surface, border: "1px solid " + C.border, borderRadius: 12, padding: "16px 18px" }}>
                    <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700, marginBottom: 4 }}>Size</div>
                    <div style={{ fontSize: 26, fontWeight: 800, color: C.primary }}>
                      {result.content_length > 1024 ? (result.content_length / 1024).toFixed(1) + "K" : result.content_length}
                    </div>
                    <div style={{ fontSize: 11, color: C.muted }}>{result.content_length > 1024 ? "KB" : "bytes"}</div>
                  </div>
                  <div style={{ background: C.surface, border: "1px solid " + C.border, borderRadius: 12, padding: "16px 18px" }}>
                    <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700, marginBottom: 4 }}>Security</div>
                    <div style={{ fontSize: 26, fontWeight: 800, color: secColor }}>{secScore}%</div>
                    <div style={{ fontSize: 11, color: C.muted }}>{secPresent}/{secTotal} headers</div>
                  </div>
                  {result.redirect_count > 0 && (
                    <div style={{ background: C.surface, border: "1px solid " + C.border, borderRadius: 12, padding: "16px 18px" }}>
                      <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700, marginBottom: 4 }}>Redirects</div>
                      <div style={{ fontSize: 26, fontWeight: 800, color: C.warn }}>{result.redirect_count}</div>
                      <div style={{ fontSize: 11, color: C.muted, wordBreak: "break-all" }}>{result.final_url !== result.url ? "-> " + result.final_url.slice(0, 30) : "same"}</div>
                    </div>
                  )}
                </div>

                {/* Tab bar */}
                <div style={{ display: "flex", gap: 4, borderBottom: "1px solid " + C.border, paddingBottom: 0 }}>
                  {TABS.map(t => (
                    <button
                      key={t.k}
                      onClick={() => setActiveTab(t.k)}
                      style={{
                        background: activeTab === t.k ? C.surface : "transparent",
                        border: "1px solid " + (activeTab === t.k ? C.border : "transparent"),
                        borderBottom: activeTab === t.k ? "1px solid " + C.surface : "1px solid transparent",
                        color: activeTab === t.k ? C.text : C.muted,
                        padding: "8px 16px", borderRadius: "8px 8px 0 0",
                        cursor: "pointer", fontSize: 13, fontWeight: activeTab === t.k ? 600 : 400,
                        marginBottom: -1,
                      }}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>

                {/* Tab content */}
                <div>
                  {/* Response tab */}
                  {activeTab === "response" && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                        <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700 }}>Content-Type</div>
                        <code style={{ fontSize: 13, color: C.accent }}>{result.content_type || "not specified"}</code>
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                        <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700 }}>Probed At</div>
                        <span style={{ fontSize: 13, color: C.soft }}>{fmt(result.probed_at)}</span>
                      </div>
                      <div>
                        <div style={{ fontSize: 11, color: C.muted, textTransform: "uppercase", fontWeight: 700, marginBottom: 6 }}>
                          Response Body Preview
                        </div>
                        <pre style={{
                          background: "#09090f", border: "1px solid " + C.border,
                          borderRadius: 10, padding: "14px 16px",
                          fontSize: 12, fontFamily: "JetBrains Mono, monospace",
                          color: C.soft, whiteSpace: "pre-wrap", wordBreak: "break-all",
                          maxHeight: 340, overflowY: "auto",
                        }}>
                          {result.body_preview || "(empty body)"}
                        </pre>
                      </div>
                    </div>
                  )}

                  {/* Headers tab */}
                  {activeTab === "headers" && (
                    <div style={{ overflowX: "auto" }}>
                      <table style={{ width: "100%", borderCollapse: "collapse" }}>
                        <thead>
                          <tr>
                            <TH>Header</TH>
                            <TH>Value</TH>
                          </tr>
                        </thead>
                        <tbody>
                          {Object.entries(result.headers || {}).map(([k, v], i) => {
                            const last = i === Object.keys(result.headers).length - 1;
                            return (
                              <tr key={k}
                                onMouseEnter={e => e.currentTarget.style.background = "rgba(108,139,255,.04)"}
                                onMouseLeave={e => e.currentTarget.style.background = "transparent"}
                              >
                                <TD mono last={last} style={{ color: C.accent, whiteSpace: "nowrap" }}>{k}</TD>
                                <TD mono last={last} style={{ color: C.soft, wordBreak: "break-all", maxWidth: 420 }}>{v}</TD>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {/* Security tab */}
                  {activeTab === "security" && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                      <div style={{
                        display: "flex", alignItems: "center", gap: 16,
                        background: C.surface, border: "1px solid " + secColor + "44",
                        borderRadius: 12, padding: "16px 20px",
                      }}>
                        <div style={{ fontSize: 36, fontWeight: 800, color: secColor }}>{secScore}%</div>
                        <div>
                          <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>
                            {secScore >= 70 ? "Good security posture" : secScore >= 40 ? "Partially secured" : "Weak security headers"}
                          </div>
                          <div style={{ fontSize: 12, color: C.muted, marginTop: 3 }}>
                            {secPresent} of {secTotal} recommended security headers present
                          </div>
                        </div>
                      </div>
                      <div style={{ overflowX: "auto" }}>
                        <table style={{ width: "100%", borderCollapse: "collapse" }}>
                          <thead>
                            <tr><TH>Header</TH><TH>Status</TH><TH>Value</TH></tr>
                          </thead>
                          <tbody>
                            {Object.entries(result.security_headers || {}).map(([name, info]) => (
                              <SecHeaderRow key={name} name={name} info={info} />
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {/* JSON explorer tab */}
                  {activeTab === "json" && result.parsed_json !== null && (
                    <div style={{
                      background: "#09090f", border: "1px solid " + C.border,
                      borderRadius: 10, padding: "16px 20px",
                      fontFamily: "JetBrains Mono, monospace", fontSize: 13, lineHeight: 1.9,
                      maxHeight: 520, overflowY: "auto",
                    }}>
                      <JsonViewer data={result.parsed_json} depth={0} />
                    </div>
                  )}

                  {/* Delta tab */}
                  {activeTab === "delta" && result.delta_from_prev && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                      <div style={{
                        fontSize: 13, color: C.muted, lineHeight: 1.7,
                        background: C.surface, border: "1px solid " + C.border,
                        borderRadius: 10, padding: "12px 16px",
                      }}>
                        Compared to probe at <strong style={{ color: C.soft }}>{fmt(result.prev_probed_at)}</strong>
                      </div>
                      {[
                        { type: "added",        items: result.delta_from_prev.added },
                        { type: "removed",      items: result.delta_from_prev.removed },
                        { type: "changed",      items: result.delta_from_prev.changed },
                        { type: "type_changed", items: result.delta_from_prev.type_changed },
                      ].map(({ type, items }) => items.length > 0 && (
                        <Panel key={type} title={type.replace("_", " ") + " (" + items.length + ")"} badge2={type.toUpperCase().slice(0,3)}>
                          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                            {items.map((path, i) => (
                              <div key={i} style={{
                                fontFamily: "monospace", fontSize: 12, color: BADGE_CFG[type]?.color || C.soft,
                                background: (BADGE_CFG[type]?.bg || "rgba(100,100,100,.1)"),
                                padding: "6px 12px", borderRadius: 6,
                              }}>
                                {path}
                              </div>
                            ))}
                          </div>
                        </Panel>
                      ))}
                      {!hasDelta && (
                        <div style={{ color: C.muted, fontSize: 13 }}>No changes detected since last probe.</div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })()}
        </div>
      );
    }

    ReactDOM.createRoot(document.getElementById("root")).render(<App />);
  </script>
</body>
</html>
"""


# Dashboard server
# ---------------------------------------------------------------------------

class DeltaDashboard:
    """
    FastAPI-based web dashboard for FlowDelta delta visualisation.

    Parameters
    ----------
    store : DeltaStore
        The delta store to query.
    title : str
        Browser tab title.
    """

    def __init__(self, store: DeltaStore, title: str = "FlowDelta Dashboard") -> None:
        self.store = store
        self.title = title
        self._trend_gen = TrendChartGenerator(store)
        self._app = None
        self._probe_cache: dict = {}  # url -> last probe result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_app(self):
        """
        Return the FastAPI application instance (lazy-created).
        Raises ``ImportError`` if ``fastapi`` / ``uvicorn`` not installed.
        """
        if self._app is None:
            self._app = self._build_app()
        return self._app

    def run(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Start the dashboard server (blocking)."""
        try:
            import uvicorn
        except ImportError as exc:
            raise ImportError(
                "Install dashboard extras: pip install fastapi uvicorn"
            ) from exc

        app = self.get_app()
        logger.info("FlowDelta Dashboard -> http://%s:%d", host, port)
        uvicorn.run(app, host=host, port=port, log_level="warning")

    # ------------------------------------------------------------------
    # App builder
    # ------------------------------------------------------------------

    def _build_app(self):
        try:
            from fastapi import FastAPI, HTTPException, Body
            from fastapi.responses import HTMLResponse, JSONResponse
        except ImportError as exc:
            raise ImportError(
                "Install dashboard extras: pip install fastapi uvicorn"
            ) from exc

        app = FastAPI(title=self.title, docs_url=None, redoc_url=None)

        store = self.store
        trend_gen = self._trend_gen
        probe_cache = self._probe_cache

        # ---- UI ----
        @app.get("/", response_class=HTMLResponse)
        async def index():
            return _DASHBOARD_HTML

        # ---- API ----
        @app.get("/api/flows")
        async def list_flows():
            all_runs = store.list_runs()
            flow_counts: Dict[str, int] = {}
            for r in all_runs:
                fid = r.get("flow_id", "unknown")
                flow_counts[fid] = flow_counts.get(fid, 0) + 1
            return {"flows": [
                {"flow_id": fid, "run_count": cnt}
                for fid, cnt in sorted(flow_counts.items())
            ]}

        @app.get("/api/flows/{flow_id}/runs")
        async def flow_runs(flow_id: str):
            runs = store.list_runs(flow_id=flow_id)
            runs.sort(key=lambda r: r.get("saved_at", ""))
            return {"flow_id": flow_id, "runs": [
                {
                    "run_id": r.get("run_id"),
                    "saved_at": r.get("saved_at"),
                    "golden": bool(r.get("golden")),
                }
                for r in runs
            ]}

        @app.get("/api/flows/{flow_id}/trend")
        async def flow_trend(flow_id: str):
            points = trend_gen.get_points(flow_id)
            return {
                "flow_id": flow_id,
                "points": [p.to_dict() for p in points],
            }

        @app.get("/api/runs/{run_id}/delta")
        async def run_delta(run_id: str):
            data = store.load_delta(run_id)
            if data is None:
                # Run exists as a trace (golden baseline) but has no delta computed yet
                trace = store.load_trace(run_id)
                if trace:
                    return {
                        "run_id": run_id,
                        "flow_id": trace.get("flow_id"),
                        "deltas": [],
                        "golden": bool(trace.get("golden")),
                        "saved_at": trace.get("saved_at"),
                        "note": "no_delta",
                    }
                raise HTTPException(status_code=404, detail=f"No run found with id {run_id!r}")
            return data

        @app.get("/api/runs/{run_id}/trace")
        async def run_trace(run_id: str):
            data = store.load_trace(run_id)
            if data is None:
                raise HTTPException(status_code=404, detail=f"No trace for run {run_id!r}")
            # Strip large locals to keep response lightweight
            snapshots = []
            for s in data.get("snapshots", []):
                snapshots.append({
                    "sequence": s.get("sequence"),
                    "file": s.get("file", "").split("\\")[-1].split("/")[-1],
                    "line": s.get("line"),
                    "function": s.get("function"),
                    "event": s.get("event"),
                    "local_count": len(s.get("locals", {})) if isinstance(s.get("locals"), dict) else 0,
                })
            return {"run_id": run_id, "snapshots": snapshots}

        @app.get("/api/health")
        async def health():
            return {"status": "ok", "store": str(store.store_path)}

        # ---- Probe endpoint ----
        @app.post("/api/probe")
        async def probe_url(
            url:     str  = Body(...),
            method:  str  = Body("GET"),
            headers: dict = Body({}),
            body:    str  = Body(None),
        ):
            import time
            try:
                import httpx
            except ImportError:
                raise HTTPException(status_code=500, detail="httpx not installed: pip install httpx")

            url = (url or "").strip()
            method = (method or "GET").upper()
            extra_headers = headers or {}
            req_body = body

            if not url:
                raise HTTPException(status_code=422, detail="url is required")

            # Auto-prepend https:// if no scheme given (e.g. "amazon.in" or "www.example.com")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            BROWSER_UA = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )

            # ---- make the request ----
            start = time.perf_counter()
            last_exc = None
            resp = None
            # Try with browser UA first; if ConnectError on HTTPS try HTTP fallback
            for attempt_url in [url, url.replace("https://", "http://", 1) if url.startswith("https://") else None]:
                if attempt_url is None:
                    continue
                try:
                    async with httpx.AsyncClient(
                        follow_redirects=True, timeout=20, verify=False,
                    ) as client:
                        resp = await client.request(
                            method, attempt_url,
                            headers={**extra_headers, "User-Agent": BROWSER_UA,
                                     "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
                                     "Accept-Language": "en-US,en;q=0.9"},
                            content=req_body.encode() if req_body else None,
                        )
                    elapsed_ms = round((time.perf_counter() - start) * 1000)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    continue

            if last_exc is not None or resp is None:
                exc = last_exc
                exc_type = type(exc).__name__ if exc else "Unknown"
                exc_msg  = str(exc).strip() if exc else "No response"
                return {
                    "ok": False, "url": url,
                    "error": f"{exc_type}: {exc_msg}" if exc_msg else exc_type,
                    "error_type": exc_type,
                }

            content_type = resp.headers.get("content-type", "")
            body_bytes = resp.content
            try:
                body_text = resp.text[:10000] if body_bytes else "(empty response body)"
            except Exception:
                body_text = f"(binary content, {len(body_bytes)} bytes)"

            # ---- parse JSON ----
            parsed_json = None
            if "json" in content_type:
                try:
                    parsed_json = resp.json()
                except Exception:
                    pass

            # ---- security header audit ----
            SEC = [
                "strict-transport-security",
                "content-security-policy",
                "x-frame-options",
                "x-content-type-options",
                "x-xss-protection",
                "referrer-policy",
                "permissions-policy",
                "cross-origin-opener-policy",
                "cross-origin-resource-policy",
            ]
            security_headers = {
                h: {"present": h in resp.headers, "value": resp.headers.get(h, "")}
                for h in SEC
            }

            # ---- delta vs previous probe ----
            prev = probe_cache.get(url)
            delta = None
            if prev and prev.get("ok") and parsed_json is not None and prev.get("parsed_json") is not None:
                try:
                    from deepdiff import DeepDiff
                    diff = DeepDiff(prev["parsed_json"], parsed_json, ignore_order=True)
                    delta = {
                        "added":   [str(k) for k in diff.get("dictionary_item_added", {})],
                        "removed": [str(k) for k in diff.get("dictionary_item_removed", {})],
                        "changed": [str(k) for k in diff.get("values_changed", {})],
                        "type_changed": [str(k) for k in diff.get("type_changes", {})],
                    }
                except Exception:
                    pass

            result = {
                "ok": True,
                "url": url,
                "final_url": str(resp.url),
                "method": method,
                "status": resp.status_code,
                "elapsed_ms": elapsed_ms,
                "redirect_count": len(resp.history),
                "content_type": content_type,
                "content_length": len(body_bytes),
                "headers": dict(resp.headers),
                "security_headers": security_headers,
                "body_preview": body_text,
                "parsed_json": parsed_json,
                "is_json": parsed_json is not None,
                "delta_from_prev": delta,
                "prev_probed_at": prev.get("probed_at") if prev else None,
                "probed_at": datetime.now(timezone.utc).isoformat(),
            }
            probe_cache[url] = result
            return result

        @app.get("/api/probe/history")
        async def probe_history():
            return {"probes": [
                {"url": url, "status": r.get("status"), "elapsed_ms": r.get("elapsed_ms"),
                 "probed_at": r.get("probed_at"), "ok": r.get("ok")}
                for url, r in probe_cache.items()
            ]}

        return app
