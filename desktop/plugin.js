/**
 * Mindscape — desktop half.
 *
 * The Python half (`~/.hermes/plugins/mindscape/`) is `kind: platform`: it gives the
 * agent an inbound adapter and 12 tools, and Electron surfaces it as a messaging
 * platform. That platform tab and a `sidebar.nav` row are DIFFERENT registries, which is
 * why "connected" alone never produced a tab.
 *
 * Loaded uncompiled, so no JSX syntax here: every element is a jsx()/jsxs() call, and
 * only @hermes/plugin-sdk, react and react/jsx-runtime may be imported.
 *
 * Three surfaces off one data source (`/api/plugins/mindscape/*` via ctx.rest):
 *   - a layout PANE, the draggable "square" the layout editor saves
 *   - a `sidebar.nav` row (order 51, directly below Kanban's 50) plus its `/mindscape`
 *     route, which is the real tab
 *
 * `GET /roster` and `GET /activity` are local reads and return at once. `POST /probe`
 * dials every node and is the only slow call (a dead node costs its connect timeout), so
 * it runs on demand and never on mount.
 */

import { cn, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const MUTED = 'text-(--ui-text-tertiary)'
const QUATERNARY = 'text-(--ui-text-quaternary)'
const SECONDARY = 'text-(--ui-text-secondary)'
const PANEL = 'rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated)'
const BTN =
  'inline-flex h-6 items-center rounded-md border border-(--ui-stroke-tertiary) px-2 ' +
  'text-[0.6875rem] transition-colors hover:bg-(--ui-control-hover-background) ' +
  'hover:text-foreground disabled:opacity-50'

/* Colors come from theme tokens only, never a literal, so every surface reskins with the
   active theme. The word beside the dot carries the meaning on its own, so the tone is
   reinforcement rather than the only signal.

   ONE vocabulary, defined once. The card used to say "offline" on the left and
   "unreachable" on the right, which reads as two separate problems, and "not dialled"
   beside "not probed" was two phrases for one state. A second copy of a status
   vocabulary drifts exactly the way a duplicated terminal-status list does. */
function statusOf(health) {
  const h = health ?? {}
  if (h.reachable === true) return { tone: 'text-(--ui-green)', word: 'online' }
  if (h.reachable === false) return { tone: QUATERNARY, word: 'unreachable' }
  return { tone: MUTED, word: 'not dialled' }
}

/* A probe result means nothing without its age: "online" from four hours ago is not the
   same claim as "online" from four seconds ago. The API already returns health_age_s. */
function healthAge(health, ageS) {
  if ((health ?? {}).probed !== true) return 'never dialled'
  const s = Number(ageS)
  if (!Number.isFinite(s)) return ''
  if (s < 45) return 'dialled now'
  if (s < 3600) return `dialled ${Math.max(1, Math.round(s / 60))}m ago`
  if (s < 86400) return `dialled ${Math.round(s / 3600)}h ago`
  return `dialled ${Math.round(s / 86400)}d ago`
}

/* Run verdicts in the app's own semantic colors: done, in flight, failed, stopped. */
function runTone(status) {
  const s = String(status ?? '').trim().toLowerCase()
  if (s === 'completed' || s === 'complete' || s === 'succeeded') return 'text-(--ui-green)'
  if (s === 'failed' || s === 'error' || s === 'errored') return 'text-(--ui-red)'
  if (s === 'canceled' || s === 'cancelled' || s === 'stopped') return 'text-(--ui-yellow)'
  if (!s) return QUATERNARY
  return SECONDARY
}

/* Relative time, so a 25-entry feed has a spine to scan down. */
function relTime(at) {
  const s = Math.floor(Date.now() / 1000 - Number(at || 0))
  if (!Number.isFinite(s) || s < 0) return ''
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

/* Day headings are what turn an undifferentiated list into a log. */
function dayLabel(at) {
  const d = new Date(Number(at || 0) * 1000)
  if (Number.isNaN(d.getTime())) return ''
  const today = new Date()
  if (d.toDateString() === today.toDateString()) return 'Today'
  const y = new Date(today)
  y.setDate(y.getDate() - 1)
  if (d.toDateString() === y.toDateString()) return 'Yesterday'
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function Dot({ tone }) {
  return jsx('span', {
    'aria-hidden': true,
    style: {
      width: 6,
      height: 6,
      borderRadius: 999,
      background: 'currentColor',
      display: 'inline-block',
      flex: 'none'
    },
    className: tone
  })
}

/* ------------------------------------------------------------------ pixel characters */

/* 8x8 glyphs. 'x' is body, 'o' is a lit pixel. Tiny on purpose: several read clearly side
   by side, and it costs no canvas, no ResizeObserver and no image asset. */
const SPRITES = [
  [
    '..xxxx..',
    '.xxxxxx.',
    'oxxxxxxo',
    '.x.xx.x.',
    '..xxxx..',
    '.xxxxxx.',
    'x.xxxx.x',
    '..x..x..'
  ],
  [
    '..xxxx..',
    '.xxxxxx.',
    'oxxxxxxo',
    '.x.xx.x.',
    '.xxxxxx.',
    '..xxxx..',
    '.xx..xx.',
    '..x..x..'
  ],
  [
    '...xx...',
    '..xxxx..',
    '.oxxxxo.',
    '..xxxx..',
    '.xxxxxx.',
    'xxxxxxxx',
    '.x.xx.x.',
    'x......x'
  ]
]

function Sprite({ variant, busy }) {
  const rows = SPRITES[Math.abs(variant) % SPRITES.length]
  const cell = 2
  const rects = []
  for (let y = 0; y < rows.length; y += 1) {
    for (let x = 0; x < rows[y].length; x += 1) {
      const ch = rows[y][x]
      if (ch === '.') continue
      rects.push(
        jsx(
          'rect',
          {
            x: x * cell,
            y: y * cell,
            width: cell,
            height: cell,
            // 'o' pixels are the eyes: accent so a character reads as alive at 16px.
            className: ch === 'o' ? 'fill-(--ui-accent)' : 'fill-(--ui-text-secondary)'
          },
          `${x}-${y}`
        )
      )
    }
  }
  return jsx('svg', {
    width: 16,
    height: 16,
    viewBox: `0 0 ${rows[0].length * cell} ${rows.length * cell}`,
    shapeRendering: 'crispEdges',
    'aria-hidden': true,
    className: cn('shrink-0', busy && 'animate-pulse')
  })
}

/* --------------------------------------------------------------------------- nodes --- */

/** The interactive unit: a character, its name, its state, and a way in. */
function NodeCard({ node, index, selected, onSelect }) {
  const health = node.health ?? {}
  const status = statusOf(health)
  const busy = health.reachable === true && (node.active_runs ?? 0) > 0
  const caps = node.capabilities ?? []
  const shown = caps.slice(0, 3)
  const hidden = caps.length - shown.length

  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(node.node),
    title: node.summary || health.error || node.node,
    className: cn(
      PANEL,
      'flex flex-col items-stretch gap-2 p-2.5 text-left transition-colors',
      'hover:bg-(--ui-control-hover-background)',
      selected ? 'border-(--ui-accent)' : null
    ),
    children: [
      jsxs('div', {
        className: 'flex items-start gap-2',
        children: [
          jsx(Sprite, { variant: index, busy }),
          jsxs('div', {
            className: 'flex min-w-0 flex-1 flex-col',
            children: [
              jsx('span', {
                className: 'truncate text-[0.75rem] font-medium text-foreground',
                children: node.node
              }),
              jsx('span', {
                className: cn('truncate text-[0.6875rem]', MUTED),
                children: node.role || 'node'
              })
            ]
          }),
          node.active_runs > 0
            ? jsx('span', {
                className: cn(
                  'shrink-0 rounded-full border border-(--ui-stroke-tertiary) px-1.5 text-[0.625rem] tabular-nums',
                  SECONDARY
                ),
                title: `${node.active_runs} run(s) in flight`,
                children: `${node.active_runs} run`
              })
            : null
        ]
      }),
      /* ONE status word, then freshness. The old right-hand label ("reachable" beside
         "online") was a second word for the same fact; the age is the useful second half. */
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Dot, { tone: status.tone }),
          jsx('span', { className: cn('text-[0.6875rem]', status.tone), children: status.word }),
          jsx('span', {
            className: cn('ml-auto shrink-0 text-[0.625rem] tabular-nums', QUATERNARY),
            children: healthAge(health, node.health_age_s)
          })
        ]
      }),
      /* Capabilities were silently sliced to three. A count of what is hidden costs one
         chip and stops the card from quietly lying about what the node advertises. */
      caps.length
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-1',
            children: [
              ...shown.map(c =>
                jsx(
                  'span',
                  {
                    className: cn(
                      'rounded border border-(--ui-stroke-tertiary) px-1 text-[0.625rem]',
                      MUTED
                    ),
                    children: c
                  },
                  c
                )
              ),
              hidden > 0
                ? jsx(
                    'span',
                    {
                      className: cn('px-1 text-[0.625rem]', QUATERNARY),
                      title: caps.join(', '),
                      children: `+${hidden} more`
                    },
                    '__more'
                  )
                : null
            ]
          })
        : null
    ]
  })
}

/* ---------------------------------------------------------------------------- chat --- */

/**
 * One message into a peer's Mindscape Chat lane.
 *
 * A timeout here is NOT a failure: the peer accepted the message and is working on it,
 * and the API reports that as delivered:"unconfirmed". Presenting it as an error is what
 * makes someone send it twice.
 */
function ChatBox({ ctx, node, onClose }) {
  const [text, setText] = useState('')
  const [state, setState] = useState({ busy: false, note: null, bad: false })

  const send = async () => {
    const message = text.trim()
    if (!message) return
    setState({ busy: true, note: null, bad: false })
    try {
      const result = await ctx.rest('/say', {
        method: 'POST',
        body: { target: node, message }
      })
      if (result && result.delivered === 'unconfirmed') {
        setState({ busy: false, bad: false, note: 'Accepted, not yet answered. Do not resend.' })
      } else {
        setState({ busy: false, bad: false, note: 'Sent.' })
      }
      setText('')
    } catch (err) {
      setState({ busy: false, bad: true, note: String((err && err.message) || err).slice(0, 160) })
    }
  }

  return jsxs('div', {
    className: cn(PANEL, 'flex flex-col gap-2 p-2.5'),
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', { className: 'text-[0.75rem] font-medium text-foreground', children: `message ${node}` }),
          jsx('button', {
            type: 'button',
            className: cn(MUTED, 'text-[0.75rem] leading-none hover:text-foreground'),
            onClick: onClose,
            title: 'close',
            children: '×'
          })
        ]
      }),
      jsx('textarea', {
        className: cn(
          PANEL,
          'w-full resize-y bg-transparent p-2 text-[0.6875rem] text-foreground',
          'placeholder:text-(--ui-text-quaternary)'
        ),
        rows: 3,
        value: text,
        placeholder: 'Goes into this node\u2019s Mindscape Chat lane, not its work lane',
        onChange: e => setText(e.target.value),
        onKeyDown: e => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send()
        }
      }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx('button', {
            type: 'button',
            className: BTN,
            disabled: state.busy || !text.trim(),
            onClick: send,
            children: state.busy ? 'sending…' : 'Send'
          }),
          state.note
            ? jsx('span', {
                className: cn('text-[0.625rem]', state.bad ? 'text-(--ui-accent)' : MUTED),
                children: state.note
              })
            : null
        ]
      })
    ]
  })
}

/* --------------------------------------------------------------------------- shell --- */

function Empty({ children }) {
  return jsx('div', { className: cn('px-3 py-6 text-center text-[0.6875rem]', MUTED), children })
}

/**
 * Roster state shared by the pane and the page, plus the on-demand dial.
 *
 * The probe is deliberately not awaited into render: with a dead node it takes its full
 * connect timeout, so the surface paints cached state at once and picks up live health
 * when it lands.
 */
function useRoster(ctx) {
  const client = useQueryClient()
  const rest = (path, opts) => ctx.rest(path, opts)

  const roster = useQuery({
    queryKey: ['mindscape', 'roster'],
    queryFn: () => rest('/roster'),
    refetchInterval: 30_000,
    staleTime: 5_000
  })
  const activity = useQuery({
    queryKey: ['mindscape', 'activity'],
    queryFn: () => rest('/activity?limit=25'),
    refetchInterval: 30_000,
    staleTime: 5_000
  })

  const probe = async () => {
    try {
      await rest('/probe', { method: 'POST', body: {} })
    } finally {
      // Health is cached server-side; refetch so the UI shows the fresh dial.
      await client.invalidateQueries({ queryKey: ['mindscape'] })
    }
  }

  return { roster, activity, probe }
}

function ApiError({ error }) {
  if (!error) return null
  return jsx('div', {
    className: cn(PANEL, 'px-3 py-2 text-[0.6875rem] text-(--ui-accent)'),
    children: `Backend not answering: ${String((error && error.message) || error)}`
  })
}

/* ---------------------------------------------------------------------- the square --- */

/** The layout pane. Compact: characters, state, and a place to type. */
function MindscapePane(ctx) {
  return function Pane() {
    const { roster, probe } = useRoster(ctx)
    const [selected, setSelected] = useState(null)

    const data = roster.data
    const nodes = data?.nodes ?? []

    return jsxs('div', {
      className: 'flex h-full flex-col gap-2 overflow-y-auto p-2.5',
      children: [
        jsxs('div', {
          className: 'flex items-center justify-between gap-2',
          children: [
            jsx('span', {
              className: cn('text-[0.6875rem] tabular-nums', MUTED),
              children: data
                ? `${data.reachable} of ${data.total} online${data.probed ? '' : ' · not dialled'}`
                : roster.isLoading
                  ? 'loading…'
                  : 'mindscape'
            }),
            jsx('button', {
              type: 'button',
              className: BTN,
              onClick: probe,
              title: 'Dial every registered node (may take a few seconds)',
              children: 'Ping'
            })
          ]
        }),
        jsx(ApiError, { error: roster.error }),
        nodes.length
          ? jsx('div', {
              className: 'grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2',
              children: nodes.map((n, i) =>
                jsx(NodeCard, { node: n, index: i, selected: selected === n.node, onSelect: setSelected }, n.node)
              )
            })
          : jsx(Empty, { children: roster.isLoading ? 'Loading roster…' : 'No peers registered.' }),
        selected
          ? jsx(ChatBox, { ctx, node: selected, onClose: () => setSelected(null) })
          : null
      ]
    })
  }
}

/* ------------------------------------------------------------------------- the tab --- */

/**
 * One feed entry, as a two-line log row rather than three competing columns.
 *
 * What this replaces and why it was unreadable: the body sat LAST, capped at
 * `max-w-[55%]` with a single-line `truncate`, so every message was clipped to roughly
 * forty characters while the right half of a wide row stayed empty, and the row ALSO
 * printed the raw direction beside the verb ("SENT" then "outbound"). Now the body is the
 * content and gets the full width at a readable weight, clamped to two lines with the full
 * text in the tooltip; the metadata collapses onto one line above it.
 */
function ActivityRow({ item, showNode }) {
  const isRun = item.type === 'run'
  const body = (isRun ? item.goal : item.body) || ''
  const verb = isRun ? 'run' : item.direction === 'outbound' ? 'sent' : 'recv'
  /* Runs get a verdict badge because it varies and it matters. Messages do not: `kind` is a
     routing detail that reads `node` on eight rows out of nine, so showing it would be the
     same constant-noise mistake as repeating the node name. It rides the tooltip instead. */
  const badge = isRun ? item.status || 'queued' : null
  const full = isRun
    ? item.run_id
      ? `${item.run_id}\n\n${body}`
      : body
    : [item.node, item.kind].filter(Boolean).join(' · ')
      ? `${[item.node, item.kind].filter(Boolean).join(' · ')}\n\n${body}`
      : body

  return jsxs('div', {
    className:
      'flex flex-col gap-1 border-b border-(--ui-stroke-tertiary) px-3 py-2 transition-colors ' +
      'last:border-b-0 hover:bg-(--ui-control-hover-background)',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx('span', {
            className: cn(
              'w-9 shrink-0 text-[0.625rem] uppercase tracking-wide',
              isRun ? SECONDARY : QUATERNARY
            ),
            children: verb
          }),
          showNode
            ? jsx('span', {
                className: 'truncate text-[0.75rem] text-foreground',
                children: item.node ?? '—'
              })
            : null,
          badge
            ? jsx('span', {
                className: cn(
                  'shrink-0 text-[0.625rem]',
                  isRun ? runTone(item.status) : QUATERNARY
                ),
                children: String(badge)
              })
            : null,
          jsx('span', {
            className: cn('ml-auto shrink-0 text-[0.625rem] tabular-nums', QUATERNARY),
            children: relTime(item.at)
          })
        ]
      }),
      body
        ? jsx('div', {
            className: cn('line-clamp-2 text-[0.6875rem] leading-relaxed', SECONDARY),
            title: full,
            children: body
          })
        : null
    ]
  })
}

function Section({ title, caption, action, scroll = true, children }) {
  return jsxs('section', {
    className: cn(PANEL, 'overflow-hidden'),
    children: [
      jsxs('header', {
        className:
          'flex items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-3 py-2',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'text-[0.75rem] font-medium text-foreground', children: title }),
              caption
                ? jsx('div', { className: cn('truncate text-[0.6875rem]', MUTED), children: caption })
                : null
            ]
          }),
          action ?? null
        ]
      }),
      /* `scroll={false}` for a section that should grow with the page instead of nesting a
         second scrollbar inside the page's own. */
      jsx('div', { className: scroll ? 'max-h-[46vh] overflow-y-auto' : null, children: children })
    ]
  })
}

/**
 * Flatten the feed into renderable rows: a day heading when the day changes, and the node
 * name only when it changes from the previous row. "eshu" appeared on 12 consecutive rows,
 * which is 12 pieces of noise carrying one fact.
 */
function buildFeed(items) {
  const rows = []
  let lastDay = null
  let lastNode = null
  for (const it of items) {
    const day = dayLabel(it.at)
    if (day && day !== lastDay) {
      rows.push({ key: `day-${day}`, day })
      lastDay = day
      lastNode = null
    }
    const node = it.node ?? '—'
    rows.push({
      key: `${it.type}-${it.run_id ?? it.at}-${rows.length}`,
      item: it,
      showNode: node !== lastNode
    })
    lastNode = node
  }
  return rows
}

function MindscapePage(ctx) {
  return function Page() {
    const { roster, activity, probe } = useRoster(ctx)
    const [selected, setSelected] = useState(null)

    const data = roster.data
    const nodes = data?.nodes ?? []
    const items = activity.data?.items ?? []

    return jsx('div', {
      className: 'mx-auto flex w-full max-w-4xl flex-col gap-3 px-4 py-4',
      children: jsxs('div', {
        className: 'flex flex-col gap-3',
        children: [
          jsxs('header', {
            className: 'flex items-end justify-between gap-3',
            children: [
              jsxs('div', {
                children: [
                  jsx('h1', { className: 'text-[1rem] font-semibold text-foreground', children: 'Mindscape' }),
                  jsx('p', {
                    className: cn('text-[0.6875rem]', MUTED),
                    children: data
                      ? `${data.reachable} of ${data.total} reachable · ${
                          data.probed ? 'health dialled' : 'health not dialled yet'
                        }`
                      : 'Cross-node roster and activity.'
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-1.5',
                children: [
                  jsx('button', {
                    type: 'button',
                    className: BTN,
                    onClick: () => {
                      roster.refetch()
                      activity.refetch()
                    },
                    children: 'Refresh'
                  }),
                  jsx('button', {
                    type: 'button',
                    className: BTN,
                    onClick: probe,
                    title: 'Dial every registered node (may take a few seconds)',
                    children: 'Probe all'
                  })
                ]
              })
            ]
          }),

          jsx(ApiError, { error: roster.error || activity.error }),

          jsx(Section, {
            title: 'Nodes',
            caption: `${nodes.length} registered · click one to message it`,
            children: nodes.length
              ? jsx('div', {
                  className: 'grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2 p-2.5',
                  children: nodes.map((n, i) =>
                    jsx(
                      NodeCard,
                      { node: n, index: i, selected: selected === n.node, onSelect: setSelected },
                      n.node
                    )
                  )
                })
              : jsx(Empty, { children: roster.isLoading ? 'Loading roster…' : 'No peers registered.' })
          }),

          selected ? jsx(ChatBox, { ctx, node: selected, onClose: () => setSelected(null) }) : null,

          jsx(Section, {
            title: 'Activity',
            caption: `${items.length} entries · ${activity.data?.messages ?? 0} messages · ${activity.data?.runs ?? 0} runs · newest first`,
            // The page already scrolls; a list inside a list gives two scrollbars and a
            // trap for the wheel.
            scroll: false,
            children: items.length
              ? buildFeed(items).map(row =>
                  row.day
                    ? jsx(
                        'div',
                        {
                          className: cn(
                            'border-b border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) px-3 py-1 ' +
                              'text-[0.625rem] uppercase tracking-wide',
                            QUATERNARY
                          ),
                          children: row.day
                        },
                        row.key
                      )
                    : jsx(ActivityRow, { item: row.item, showNode: row.showNode }, row.key)
                )
              : jsx(Empty, { children: activity.isLoading ? 'Loading activity…' : 'No activity yet.' })
          })
        ]
      })
    })
  }
}

/* ------------------------------------------------------------------------- registry --- */

export default {
  id: 'mindscape',
  name: 'Mindscape',
  defaultEnabled: true,
  register(ctx) {
    // The pane and page are factories so they close over the real ctx, which exists only
    // inside register. ChatBox takes ctx as a prop rather than a global: a bare window
    // property is invisible to the host and would outlive a disable or hot-reload.

    // The square: a pane in the layout tree, draggable anywhere, remembered by a saved
    // layout. This is the surface a platform tab can never give you.
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'mindscape',
      data: { placement: 'right', width: '320px' },
      render: () => jsx(MindscapePane(ctx), {})
    })

    // Sidebar row, order 51: directly below Kanban's 50.
    ctx.register({
      id: 'nav',
      area: 'sidebar.nav',
      order: 51,
      data: { codicon: 'broadcast', label: 'Mindscape', path: '/mindscape' }
    })

    // The page that row navigates to.
    ctx.register({
      id: 'page',
      area: 'routes',
      data: { path: '/mindscape' },
      render: () => jsx(MindscapePage(ctx), {})
    })
  }
}
