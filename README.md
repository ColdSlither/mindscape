# Mindscape

A shared interior space across Hermes nodes.

Mindscape is three things in one plugin:

1. **A platform** (`kind: platform`), which is what gives it a tab in the desktop
   app alongside Sessions, Telegram and Photon. The inbound adapter accepts peer
   messages and routes each one into a **lane**.
2. **An outbound toolset** (12 tools) for dispatching work, chatting, and managing
   peers and identity.
3. **A desktop half** (`desktop/plugin.js`), which registers a Mindscape sidebar
   row and route in the desktop app. See [Desktop half](#desktop-half-sidebar-tab)
   below.

## Lanes: the shadow clone

A **lane** is a session with a known title. It is not a different agent: same
profile, same memory, same skills, its own conversation. Two lanes are the default:

| Lane | Carries | Default title |
| --- | --- | --- |
| work | dispatched tasks (`mindscape_assign`) | `Mindscape` |
| chat | peer messages (`mindscape_say`, inbound) | `Mindscape Chat` |

Measured, not assumed: two 20-second probes in two lanes on one node overlapped by
**18 seconds**, so lanes give genuine concurrency, not just a reordered queue. The
gateway corroborates at `gateway.api_server.max_concurrent_runs: 10`. There is a gate
*before* admission (a busy node delays new runs) while admitted runs overlap.

Before lanes, everything landed in Bot Chat and a chat message queued behind a
long build. `hermes peer run` hardcodes the Bot Chat title, which is why lanes go
over the direct HTTP surface in `transport.py`.

## Desktop half (sidebar tab, page, and pane)

Platform tabs and `sidebar.nav` rows are different registries. Being
`kind: platform` gives Mindscape its inbound adapter, but nothing in the
sidebar — so `desktop/plugin.js` adds three surfaces off the plugin's own
dashboard API (`plugins/mindscape/dashboard/plugin_api.py` — nothing
reimplemented):

- a `panes` **square** for the layout tree: draggable, remembered by saved
  layouts, named `mindscape`. Compact form of the same roster — pixel-sprite
  character per node, online/offline state, up to three capability chips,
  in-flight run count, a **Ping** button (manual `POST /probe`, same on-demand
  dial as the page), and a chat box into the selected node's Mindscape Chat
  lane (`POST /say`, Cmd/Ctrl+Enter to send);
- a `sidebar.nav` row (**order 51, directly below Kanban's 50**) with label
  *Mindscape*, codicon `broadcast`, path `/mindscape`;
- a `routes` page at `/mindscape`: **Nodes** grid (click a card to message it,
  from `GET /roster`) and **Activity** feed (merged messages + runs from
  `GET /activity`), with manual **Probe all** (`POST /probe`) and 30-second
  auto-refresh.

Each node renders as a pixel-sprite character (three 8x8 variants, accent eyes,
pulse while it has runs in flight) with theme-token colors only, so every
surface reskins with the active theme. State words are plain — online, offline,
not dialled — never color-alone.

Electron treats `plugins/<package>/desktop/` as the desktop half of a unified
package and reconciles it into `~/.hermes/desktop-plugins/<package>/` at boot
(copy with a `.hermes-package.json` marker). Saving the file hot-reloads in
place via the fs watcher, so no restart is needed for edits. One caveat, verified
the hard way: a copy materialized at boot can sit **unloaded**, and ⌘K → Reload
desktop plugins against an unchanged folder does nothing. A write anywhere in
`desktop-plugins/<package>/` (touching `plugin.js` is enough) wakes the watcher
and the surfaces appear within seconds. The row appears the way `desktop-plugins`
rows always do, not as a platform tab.

## Security posture

- **Loopback-only unless a token is set.** A non-loopback bind without a token is
  *refused*, never silently widened.
- **Bearer token required** for any non-loopback caller.
- **Every inbound message is framed as UNTRUSTED DATA** before it reaches the agent,
  and `allow_gateway_control` is forced off so peer text can never resolve gateway
  commands. A network of agents is a prompt-injection path with tool access behind
  it; peer content is material to reason about, never instructions to follow.
- Inbound text is length-capped, lane names are pattern-checked, and the body has a
  hard byte ceiling.

Teammates gives **one machine** a named roster of subagents. Mindscape gives a
**mesh of machines** one roster of agents: every registered peer shows up with
its role, its advertised capabilities and its live reachability; work dispatches
to a node asynchronously and is tracked in a durable ledger; every run can be
recalled later from any node.

The name is the intent. A mindscape is not a message queue: it is a common
interior where separate minds coexist and can see each other.

## Install

```bash
# from the repository (a custom source: installs the branch tip, and warns it is unvetted)
hermes plugins install ColdSlither/mindscape

# or pin an exact reviewed commit (the current one is the tip of main)
hermes plugins install ColdSlither/mindscape --ref <40-char-commit-sha>

hermes plugins enable mindscape
```

Requires Hermes **>= 0.22**. Tools appear on the next session (`/reset`) or gateway
restart. Tool changes do not apply mid-conversation, by design (prompt caching).

### Reaching another node

Installing this gives you a mindscape of one. A peer is an ordinary Hermes gateway with
`api_server` enabled, registered locally:

```bash
hermes peer add <name> --url <url> --key <key>
```

**Both sides must be mutually reachable, and that is not something this plugin can fix.**
If the other side sits on a private network — a mesh, a VPN, a LAN — you need shared
reachability first (join that network, or tunnel to it), and the URL you register has to be
one *you* can dial. Two nodes on unrelated private networks, each reachable only inside its
own, simply cannot see each other; the roster will report the peer as unreachable rather
than pretend otherwise.

It is also worth being clear about what a peer grant means. An admitted peer can dispatch
work that executes in **your** agent's own session, with your tools, memory and context.
Admission is invite-only and enforced on the receiving side, but it is a real trust decision
rather than a follow.


## Tools

| Tool | What it does |
| --- | --- |
| `mindscape_identity` | This node's Mindscape ID and public key. Private key never leaves the machine |
| `mindscape_peers` | The buddy list: admitted peers, their tier, their address |
| `mindscape_admit` | Admit a peer by Mindscape ID, invite-only. Grants **chat only** |
| `mindscape_revoke` | Remove a peer. One local action, nothing announced |
| `mindscape_say` | **One-to-one chat**, the baseline primitive. Available at the chat tier |
| `mindscape_presence` | Who is reachable, at what tier, and when you last spoke |
| `mindscape_roster` | Estate nodes: role, capabilities, live reachability, active runs |
| `mindscape_assign` | Dispatch work to a node, return a run ID immediately (`wait_seconds` to block) |
| `mindscape_status` | Current status and output of one run |
| `mindscape_wait` | Wait for 1-16 runs, `all` or `any` |
| `mindscape_recall` | Read back past dispatches from the durable ledger |
| `mindscape_stop` | Stop one run without disturbing other work on that node |

## How it moves

Transport is the stock `hermes peer` CLI, which speaks the peer's own Runs API:

```
mindscape_assign
      │
      └─► hermes peer run <node> --json --idempotency-key <k>   (goal on stdin)
                │
                └─► POST {peer}/v1/runs          → run_id, session_id
                    GET  {peer}/v1/runs/<id>     → status, output
                    POST {peer}/v1/runs/<id>/stop
```

Nothing new is exposed on the wire. No peer needs a patched core. The bearer key
resolution and cross-origin redirect hardening come from the upstream transport
rather than being reimplemented here.

Dispatch is durable **before** it is returned: the ledger row is written first,
so any run ID a caller holds is always recallable even if the process dies.

## Ledger reconciler

`reconcile.py` keeps the run ledger current without a human polling it. It is started from
`__init__.py` in every process that registers the plugin, as one daemon thread, and is disabled
with `MINDSCAPE_RECONCILE=0`.

**The defect it fixes.** The ledger used to be poll-driven: `updated_at` moved only when somebody
called `mindscape_status`, so a run that had silently died was indistinguishable from one working
hard, and the roster's active-run count was whatever the last human happened to look at.

**What it does.** Asks the ledger for runs that are not terminal and not polled recently, polls
each through the same `hermes peer status --json` path the tools use, and writes the outcome back.
Liveness lands in `last_polled_at` and `poll_count`, so a viewer can tell "still running" apart from
"nobody has looked in an hour". Idle cost is nothing: the loop wakes, finds nothing eligible, and
sleeps. Ten seconds while something is in flight, sixty while idle.

Staleness is judged from `last_polled_at` rather than a local timer on purpose. This plugin loads
into short-lived CLI processes too, so judging from the ledger means two processes cannot stampede
one peer, and a process that dies simply stops contributing.

**It is per-process, so a running gateway does not pick it up.** Measured 2026-10-30: the gateway
had been up since the previous evening while `reconcile.py` landed that afternoon, so the poller
existed only in short-lived processes that started and died around the module's arrival. Poll
stamps clustered at a single timestamp and then stopped, which is exactly what that looks like.
Restart the gateway after deploying it.

### Two measured defects, both older than the reconciler — FIXED

Found by probing the live ledger on 2026-10-30, not by reading the code. Both are fixed at the
source; the observed figures below are the before state.

1. **The terminal-status list did not match the peer's vocabulary.** `_TERMINAL` was
   `succeeded, failed, stopped, cancelled, error, timeout`. The peer reports `completed` and
   `running`. So `completed` read as in-flight: finished runs were re-polled indefinitely and the
   roster over-reported active work. Observed: five ledger rows on `node-alpha`, four of them `completed`,
   all five reported as active.
2. **A run the peer has forgotten never resolved.** `hermes peer status` for a run its registry no
   longer holds returns HTTP 404 `Run not found`. That arrived as a failed poll, so the row was
   stamped `last_polled_at` with its status left alone: sticky forever, permanently inflating the
   active count.

**The roster bug had a third cause, and it was the real one.** `mindscape_roster` carried a
*second, hardcoded copy* of the terminal list — precisely the drift `reconcile.py`'s own docstring
warns against. So correcting `_TERMINAL` alone would still have left the count lying. The roster now
builds its `NOT IN (...)` from `_TERMINAL`.

| Change | Effect |
| --- | --- |
| `completed`, `complete`, `canceled`, `not_found`, `gone` added to `_TERMINAL` | the peer's real vocabulary is terminal |
| `_is_terminal()` normalises case and whitespace | no spelling variant can resurrect the lie |
| `_update()` normalises status on write | reconcile's SQL `NOT IN` cannot miss a cased state |
| `transport.RunNotFound` raised on HTTP 404; `_poll` returns status `not_found` | a forgotten run ends instead of staying sticky |
| roster derives its status filter from `_TERMINAL` | one list, not two |

Measured after the fix, on a ledger holding one synthetic `completed` and one synthetic `running`
row: the old hardcoded query counted **7** active, the corrected query **2**. That gap is the
reported lie, reproduced.

The lesson is the one this codebase keeps teaching: a vocabulary assumed rather than observed is a
defect waiting for a time. Read the terminal states off the peer, give "not found" a terminal state
of its own, and never keep two copies of the list that decides what is finished.

## Disclosure

What this plugin does beyond its own data, in one place, so the blast radius is judgeable
before installing rather than after.

- **Inbound listener.** `adapter.py` runs a threaded HTTP server so peers can reach this
  node. It binds `127.0.0.1` by default and widens only when `MINDSCAPE_TOKEN` is set; a
  wide bind without a token is refused, not honoured. Default port `9901`.
- **Outbound network calls.** Runs and chat are sent to the URLs you register per peer, with
  `Authorization: Bearer <peer key>` on each request. They go through the same redirect-safe
  opener `hermes peer` uses, so the key is not carried across a cross-origin redirect, and it
  is never logged or returned.
- **Subprocess.** The assign/status/stop path invokes the stock `hermes peer` CLI with
  `subprocess.run` for the transport the CLI owns. No shell, no other executables.
- **Stored credentials.** An Ed25519 identity keypair is written to this plugin's own data
  directory at mode `0600`. Peer bearer keys are read through
  `agent.secret_scope.get_secret` (profile-scoped) and are never written by this plugin.
- **Files read outside plugin data.** A roster overlay at `~/.hermes/mindscape-nodes.yaml`,
  or whatever `settings.nodes_file` points at, if you create one.
- **No telemetry, no self-update.** Nothing is reported anywhere and nothing fetches its own
  replacement code. Updates only ever arrive by installing a newer reviewed pin.

### Known coupling

`transport.py` imports `hermes_cli.urllib_security.open_credentialed_url`, which
`hermes plugins validate` lists as a private core import (catalog rule 9). That is deliberate
and currently unavoidable: the public alternatives (`tools.url_safety.is_safe_url`,
`gateway.platforms.helpers.platform_http_client`) reject private and loopback addresses by
design, which is exactly what a peer on a mesh or LAN must reach, and the public client is
async while this transport is synchronous. The stock `hermes peer` CLI imports the same
helper for the same reason. A documented seam is requested upstream; this note goes away when
one exists.

## Configuration

Optional, and only needed to give nodes meaning beyond their registered name.
The roster lives in a file, not in `config.yaml`, so it can be edited like any
other document and shared between profiles:

```
~/.hermes/mindscape-nodes.yaml      # default; override with settings.nodes_file
```

```yaml
nodes:
  studio:
    role: Deep reasoning
    key: STUD-1A2-3BCD
    capabilities: [code, analysis, rendering]
    summary: "Workstation; owns the render lane"
  vault:
    role: Truth source
    key: VULT-4E5-6FGH
    capabilities: [storage, models, inventory]
```

Read order is `settings.nodes` (inline) → `settings.nodes_file` → the default
path above. With no overlay at all every node still appears, with role `peer`
and capability `*`. The file is re-read on every call, so edits apply without a
restart. Identity keys follow the estate's Hermes Agent Registry, so there is one
source of keys rather than two.

Note for debugging: the YAML path needs PyYAML, which the gateway venv has. A
test run under a bare system `python3` will silently fall back to JSON parsing
and return an empty overlay.

## Design rules this holds to

- **The model never picks a URL or a credential.** Targets resolve only against
  the registered `bot_peers` map; an unknown name fails closed.
- **The model never picks a provider or model.** It picks a node name.
- **A node that is down is a fact, not an exception.** Probes are short and the
  caller is never blocked on an absent machine.
- **One transport.** No private imports from `hermes_cli`; no second HTTP client.
- **No printing.** stdout in this process belongs to the gateway.

## Known limitations (v0.1, honest list)

1. ~~**Dispatch lands in the peer's Bot Chat, and that is intended.**~~ **Reversed; lanes
   shipped.** The original argument was that a per-dispatch lane would make this "a message bus
   wearing a mindscape's name", and that sharing the conversation is what lets a peer answer as
   itself. The evidence that overturned it: with one conversation, a chat message queues behind a
   long build, and the operator watches every dispatch interleave with their own thread. Lanes were
   measured before they were believed (two 20-second probes overlapping by 18 seconds), and the
   original fear did not survive contact, because a lane is the same profile, memory and skills
   with its own conversation. Context isolation turned out to be separable from identity. The
   objection is kept here because it is why the measurement was done at all.
2. ~~**Outbound only.**~~ **Half-resolved.** The inbound adapter shipped (`adapter.py`, three
   routes: identity, lanes, inbound message), so a peer can reach this node and its message lands
   in a lane rather than the operator's conversation. What is still true: presence is computed
   **locally**, so a node sees *reachability*, not *what its peers are doing right now*.
3. **No blackboard yet.** Runs and their outputs live in each node's own ledger.
   There is no shared artifact space that all nodes read and write.
4. **No capability routing yet.** `assign` takes a node name. Choosing a node by
   capability (`assign to anything that does 'image'`) is not implemented.

## Relationship to what already ships

| Existing | What it does | Why Mindscape is not a duplicate |
| --- | --- | --- |
| **Teammates** (community plugin) | Named subagents on one machine, local ledger, Kanban lane | Local only. Same shape, different axis: Mindscape's roster is machines. |
| **Bot Mode** (built into Desktop) | Cross-machine chat: roster, `@name-device`, group chats, relayed DMs | A chat/relay layer requiring Desktop and its Connections registry. Not an orchestration API; no run IDs, no ledger, no headless path. |
| **`hermes peer`** (core CLI) | `dm`, `run`, `status`, `stop` over the Runs API | This is the *transport*. Mindscape is the orchestration and memory layer on top of it. |
| **A2A plugin** (bundled, not enabled here) | `a2a_orchestrate` fan-out by capability, Agent Cards, works with non-Hermes agents | Request/reply fan-out with no durable shared state and a separate `a2a_agents` config. Interop-first; Mindscape is Hermes-native and ledgered. |

## Roadmap

- ~~**v0.2** Inbound surface~~ **Shipped 2026-10-30.** `adapter.py` serves
  `GET /mindscape/v1/identity`, `GET /mindscape/v1/lanes` and `POST /mindscape/v1/message`, with
  inbound text framed as untrusted data and gateway control forced off.
- **v0.3** Capability routing and broadcast: `assign` by capability, mirroring
  `a2a_orchestrate`'s `all` / `first` / `best` modes but Hermes-native and
  ledgered.
- **v0.4** Shared blackboard for artifacts, so results land somewhere every node
  can read.
- **v0.5** Registry sync: read the estate's Hermes Agent Registry directly
  instead of a hand-seeded `mindscape-nodes.yaml`.

~~Explicitly **not** on the roadmap: a dedicated session lane per dispatch.~~ That was the position
before lanes shipped and were measured. A lane per work stream and a lane for chat are now the
shipped design; see the lanes section above.

## Verified

Re-measured 2026-10-30 (this section previously claimed 6 tools, which was already stale).

- `hermes plugins doctor` → manifest parsed, import and registration passed, **12 tools, 0 hooks**.
  Manifest is `kind: platform`, so the desktop tab registers from the adapter rather than the
  toolset.
- `mindscape_roster`, run in-process against a live three-node mesh: **reachable 1 of 3.**
  - `node-alpha` — reachable, `runs_api` true, key set, connected directly.
  - `node-beta` — key set but unreachable, `URLError: <urlopen error timed out>`, and absent from the
    mesh listing entirely, so its registered address is stale or it is down.
  - `node-gamma` — `no key set`, and its mesh node key expired ten days ago. Two separate blockers,
    both needing hands on that machine.
- A run ID the peer no longer holds returns HTTP 404 `Run not found`. That is the live evidence
  behind defect 2 above.
- Unregistered target refused; missing goal refused.
