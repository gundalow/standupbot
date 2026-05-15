# Standupbot Modernization Plan

## Problem

Standupbot uses `maunium.net/go/mautrix` v0.10.12 (circa 2021). This version
predates the authentication changes required by current Fedora Matrix
homeservers. The bot cannot connect to modern Matrix infrastructure without
upgrading its Matrix client library.

## Current Codebase Summary

| Component            | File(s)                                    | Lines |
|----------------------|--------------------------------------------|-------|
| Main / sync loop     | `standupbot.go`                            | 345   |
| Commands & routing   | `command_handler.go`                       | 707   |
| Post creation        | `create_post.go`                           | 350   |
| Send helpers         | `helper.go`                                | 88    |
| Config loading       | `configuration.go`                         | 24    |
| Crypto logger        | `crypto-logger.go`                         | 27    |
| Store (state/sync)   | `store/store.go`, `storer.go`              | 189   |
| Store (user config)  | `store/config_room.go`                     | 159   |
| Store (crypto)       | `store/crypto_state_store.go`              | 122   |
| Custom event types   | `types/state_events.go`                    | 27    |
| **Total**            |                                            | **2038** |

**Key dependencies:** mautrix v0.10.12, logrus, go-sqlite3, libolm (C),
google/uuid, kyoh86/xdg, sethvargo/go-retry.

**Bot features:** DM-based standup flow (Done/Planned/Blockers/Notes), thread
mode, reaction-based confirm/cancel, post editing after send, scheduled
notifications, E2E encryption (Olm/Megolm), user config via Matrix state
events, flow persistence across restarts.

---

## Option A: Update mautrix-go to Latest (v0.27.0)

### What changes

Upgrade from v0.10.12 → v0.27.0, adapting to 5 years of breaking changes.
Keep the existing Go binary architecture. No new frameworks.

### Breaking changes to address

| mautrix version | Breaking change | Impact on standupbot |
|-----------------|-----------------|----------------------|
| v0.15 | Logging switched from maulogger to **zerolog** | Replace all logrus usage with zerolog, or adapt the crypto logger interface |
| v0.17 | **`context.Context` required** on all client calls | Add `ctx` parameter to every `client.*` call (~50+ call sites) |
| v0.17 | `EventSource` moved from handler param to `event.Source` field | Update all syncer handler signatures (6 handlers) |
| v0.17 | OlmMachine API changes (context params) | Update crypto init, decrypt, encrypt calls |
| v0.21 | Unauthenticated media dropped | Verify no media usage (standupbot doesn't use media — no impact) |
| v0.25 | gorilla/mux → stdlib ServeMux | No impact (standupbot doesn't run an HTTP server) |
| v0.27 | Removed auto gob registrations, PK encryption removals | Minor — update crypto store init |
| Ongoing | libolm deprecated → **goolm** (pure Go) | Drop C dependency on libolm; simplify Docker build |

### Implementation plan

#### Phase 1: Dependency update and compilation (1–2 days)

1. Update `go.mod`: bump Go version to 1.21+, update mautrix to v0.27.0.
2. Run `go build` — collect all compilation errors.
3. Fix import path changes (if any sub-packages moved).

#### Phase 2: Context threading (1–2 days)

Every client method now requires `context.Context`. This is the largest
mechanical change.

1. Add `ctx context.Context` to `SendMessage()`, `SendReaction()` in
   `helper.go`.
2. Thread context through `HandleMessage()`, `HandleReaction()`,
   `HandleRedaction()` in `command_handler.go`.
3. Thread context through `CreatePost()` and all post-send helpers in
   `create_post.go`.
4. Update `main()`: pass `context.Background()` to login, sync, joined-rooms,
   members, state-event calls.
5. Update store methods if the `Storer` interface now requires context.

#### Phase 3: Event handler signature update (0.5 day)

The syncer handler signature changed — `EventSource` is no longer a parameter.

```go
// Old (v0.10):
syncer.OnEventType(mevent.EventMessage, func(source mautrix.EventSource, event *mevent.Event) { ... })

// New (v0.17+):
syncer.OnEventType(mevent.EventMessage, func(event *mevent.Event) { ... })
// Access source via event.Source
```

Update all 6 handlers in `standupbot.go` (lines 257–300).

#### Phase 4: Crypto / OlmMachine update (1 day)

1. Switch from libolm (C library) to goolm (pure Go) — remove `olm-dev` from
   Dockerfile.
2. Update `NewSQLCryptoStore()` call if constructor signature changed.
3. Update `NewOlmMachine()` — likely now requires context and has a different
   logger interface.
4. Update `DecryptMegolmEvent()` and encrypt paths to pass context.
5. Update `ProcessSyncResponse()` and `HandleMemberEvent()` signatures.

#### Phase 5: Logging migration (0.5–1 day)

mautrix v0.15+ uses zerolog internally. Options:

- **Option A (recommended):** Switch standupbot to zerolog too. Replace logrus
  calls (~40 sites) with zerolog equivalents. Keeps one logging library.
- **Option B:** Keep logrus for standupbot's own logging, implement the new
  crypto logger interface to bridge to logrus. Results in two logging
  libraries.

#### Phase 6: Dockerfile and build modernization (0.5 day)

1. Update base image from `golang:1-alpine3.16` to current Alpine.
2. Remove `olm-dev` / `olm` packages (no longer needed with goolm).
3. Update Go version to 1.21+.
4. Test multi-stage build produces working binary.

#### Phase 7: Testing and validation (1–2 days)

1. Test login against Fedora Matrix homeserver with new auth.
2. Test E2E encryption (key exchange, message decrypt/encrypt).
3. Test full standup flow: new → items → confirm → send.
4. Test thread mode, edit-after-send, undo.
5. Test notification scheduling.
6. Test flow persistence across restart.
7. Test invite/join handling.

### Risks

- **Crypto state migration:** Existing Olm sessions in the SQLite DB may not
  be compatible with goolm's storage format. May need to wipe crypto state and
  re-verify devices.
- **Untested API surface:** mautrix doesn't have a formal migration guide.
  Some changes may only surface at runtime (e.g., new required fields in
  requests).
- **goolm maturity:** goolm is newer than libolm. The April 2026 release notes
  mention ongoing hardening. Ed25519 key storage differences have been found
  via differential fuzzing.

### Estimated effort

**5–8 days** of focused development, assuming familiarity with the codebase.

### What you get

- Working bot on modern Fedora Matrix homeservers.
- Pure Go binary (no C dependencies).
- Same architecture, same deployment model, same features.
- Position to adopt future mautrix improvements incrementally.

---

## Option B: Rewrite as a Maubot Plugin (Python) — SELECTED

### Decisions

| Decision                   | Choice                                       |
|----------------------------|----------------------------------------------|
| **Database backend**       | Support both aiosqlite and asyncpg (config-time choice) |
| **User config storage**    | Matrix room state events (preserve existing `com.nevarro.standupbot.*` events) |
| **Flow persistence**       | Database (survives crashes, hot-reloads, restarts) |
| **Command prefixes**       | `!standupbot`, `!su`, `@standupbot` (all three) |
| **Feature scope**          | Full parity — threads, edit-after-send, undo, MSC3464, notifications, E2E |
| **Encryption migration**   | New bot account — users re-verify once |
| **Maubot instance**        | Assume recent (not latest) version already available |
| **Deployment target**      | OpenShift (existing deployment) |
| **Testing environment**    | None currently — manual testing against live homeserver |

### What changes

Rewrite standupbot from scratch as a Python maubot plugin. The bot runs inside
a maubot instance rather than as a standalone binary. The entire Go codebase
is replaced. A new Matrix bot account will be used.

### Maubot architecture

```
maubot instance (runs on server or container)
├── Web management UI (plugin upload, config, logs)
├── Matrix client (handled by maubot, not your code)
├── E2E encryption (handled by maubot)
└── Plugins (.mbp files)
    └── standupbot/
        ├── maubot.yaml          (plugin metadata)
        ├── standupbot/
        │   ├── __init__.py      (plugin entry point)
        │   ├── bot.py           (Plugin subclass — commands, event routing)
        │   ├── flow.py          (standup flow state machine & data model)
        │   ├── post.py          (post formatting & sending)
        │   ├── scheduler.py     (notification scheduling)
        │   ├── config.py        (user config via Matrix state events)
        │   └── db.py            (database models — flow persistence)
        └── base-config.yaml     (default config)
```

### What maubot handles for you

- Matrix client lifecycle (login, sync, reconnect)
- E2E encryption (key management, session sharing)
- Database connection (asyncpg or aiosqlite — configured at instance level)
- Event dispatching to handlers
- Plugin hot-reload (update without restarting)
- Web UI for configuration and monitoring
- Multi-bot hosting (one instance, many plugins)

### What you write

Only the bot-specific logic:
- Command handlers (`@command.new()` decorators)
- Event handlers (`@event.on()` decorators)
- Standup flow state machine (persisted to database)
- Post formatting
- Notification scheduling (async timers)
- User config read/write via Matrix room state events
- Database schema for flow persistence only (user config stays in state events)

### Implementation plan

#### Phase 1: Plugin scaffold and database (1 day)

1. Create plugin directory structure with `maubot.yaml`.
2. Define database schema for flow persistence (supports both SQLite and
   PostgreSQL via maubot's database abstraction):
   - `standup_flow` table: user_id, flow_id (UUID), state (enum), room_id,
     created_at, updated_at.
   - `standup_item` table: flow_id, section (done/planned/blockers/notes),
     event_id, body, formatted_body, position.
   - `previous_post` table: user_id, event_id, flow_id, day, room_id.
   - `reactable_event` table: flow_id, event_id (for tracking ✅/❌ targets).
3. Implement database upgrade mechanism (maubot's built-in upgrade table).
4. Write `base-config.yaml` with default settings.

Note: User config (timezone, notify time, send room, thread preference) is
NOT stored in the database. It is read/written from Matrix room state events
using the existing `com.nevarro.standupbot.*` custom event types, preserving
compatibility with the current Go bot's stored config.

#### Phase 2: User config via state events (1 day)

Port the state event read/write layer from `store/config_room.go` and
`types/state_events.go`:

1. Define custom state event content types:
   - `com.nevarro.standupbot.timezone` → `TzSettingEventContent`
   - `com.nevarro.standupbot.notify` → `NotifyEventContent`
   - `com.nevarro.standupbot.send_room` → `SendRoomEventContent`
   - `com.nevarro.standupbot.use_threads` → `UseThreadsEventContent`
   - `com.nevarro.standupbot.previous_post` → `PreviousPostEventContent`
2. Implement in-memory cache (dict per user) populated on startup by scanning
   joined rooms' state events (port of `standupbot.go` lines 177–226).
3. Implement cache-through write: update state event + cache on user config
   changes.
4. State key = user ID local part (e.g., `alice` from `@alice:example.com`),
   matching Go bot behavior.

#### Phase 3: Core command handlers (2–3 days)

Port each command from `command_handler.go`:

```python
from maubot import Plugin
from maubot.handlers import command

class StandupBot(Plugin):
    @command.new(name="standupbot", aliases=["su"])
    async def standup(self, evt) -> None:
        pass

    @standup.subcommand("new")
    async def new_post(self, evt) -> None:
        # Start standup flow
        ...

    @standup.subcommand("help")
    async def help(self, evt) -> None:
        ...

    @standup.subcommand("show")
    async def show(self, evt) -> None:
        ...

    @standup.subcommand("edit")
    async def edit(self, evt) -> None:
        ...

    @standup.subcommand("cancel")
    async def cancel(self, evt) -> None:
        ...

    @standup.subcommand("undo")
    async def undo(self, evt) -> None:
        ...

    @standup.subcommand("tz")
    async def timezone(self, evt) -> None:
        ...

    @standup.subcommand("notify")
    async def notify(self, evt) -> None:
        ...

    @standup.subcommand("room")
    async def room(self, evt) -> None:
        ...

    @standup.subcommand("threads")
    async def threads(self, evt) -> None:
        ...

    @standup.subcommand("vanquish")
    async def vanquish(self, evt) -> None:
        ...
```

Also register `@standupbot` as an additional trigger (maubot mention handler
or raw event filter) to maintain the third command prefix.

#### Phase 4: Standup flow state machine (2–3 days)

This is the most complex part — porting the interactive flow from
`command_handler.go` and `create_post.go`.

1. Port `StandupFlow` struct → Python dataclass with DB-backed persistence.
   Every state change writes to the database immediately (no JSON-on-shutdown).
2. Port flow state transitions (Done → Planned → Blockers → Notes → Confirm).
3. Port message collection: listen for non-command messages during active flow,
   store items to DB as they arrive.
4. Port reaction handling: ✅ to advance, ❌ to cancel.
5. Port thread mode: create thread roots, collect replies via relation events.
6. Port preview generation and edit-after-send.
7. Port post formatting (Markdown → HTML via mautrix-python format utils).
8. Port "on behalf of" posting (MSC3464 `space.nevarro.msc3464.on_behalf_of`
   custom field in message content).

#### Phase 5: Notification scheduler (1 day)

1. Implement async background task (`asyncio.create_task` in `start()`) that
   checks notifications each minute.
2. Port timezone conversion logic (Python `zoneinfo` stdlib module).
3. Port weekend skipping.
4. Port duplicate-flow prevention (check DB for active flow before creating).
5. Cancel task in `stop()` to handle plugin reload cleanly.
6. On startup, reload notification schedule from state events (same scan as
   Phase 2).

#### Phase 6: Event handlers for edits and redactions (1 day)

1. Handle `m.room.message` with `m.relates_to` / `m.new_content` (edits) →
   update item in DB, regenerate preview.
2. Handle `m.room.redaction` → remove item from DB, update preview.
3. Handle reactions on non-command messages during active flow.
4. Handle reactions on preview messages (✅ Send / ❌ Cancel).

#### Phase 7: Invite and room management (0.5 day)

1. Auto-join on invite (port of `standupbot.go` lines 261–273).
2. Track room membership for encryption key sharing (maubot handles most of
   this, but verify behavior).
3. Implement `vanquish` command (bot leaves room).

#### Phase 8: Deployment (1 day)

1. Build `.mbp` plugin package.
2. Upload to existing maubot instance on OpenShift.
3. Configure bot client in maubot management UI.
4. Verify PVC / persistent storage for maubot's database.
5. Update CI/CD pipeline: build `.mbp` on push, optionally auto-upload.

#### Phase 9: Testing and validation (2–3 days)

Manual testing against live Fedora Matrix homeserver:

1. Test login and encryption (new bot account, users re-verify).
2. Test full standup flow: `!su new` → add items → ✅ confirm → send to room.
3. Test thread mode: `!su threads true`, verify thread roots and reply
   collection.
4. Test edit-after-send: modify items after posting, ✅ re-send.
5. Test undo: `!su undo` redacts sent post.
6. Test all config commands: `!su tz`, `!su notify`, `!su room`, `!su threads`.
7. Test notification scheduling: set notify time, verify DM arrives.
8. Test flow persistence: hot-reload plugin mid-flow, verify flow resumes.
9. Test invite handling: invite bot to new room, verify auto-join.
10. Test all three command prefixes: `!standupbot new`, `!su new`,
    `@standupbot new`.
11. Test reaction-based cancel (❌) at each flow stage.
12. Test message redaction during flow (delete an item, verify preview updates).
13. Test edge cases: multiple rapid messages, empty sections, very long items.

### Risks

- **Feature parity gaps:** The interactive flow (reactions, edits, threads,
  edit-after-send) is intricate. Subtle behaviors may be lost in translation.
  The Go codebase has ~1050 lines of flow logic across `command_handler.go`
  and `create_post.go`.
- **Maubot dependency:** You now depend on the maubot project for the
  framework, its update cadence, and its compatibility with homeservers.
- **Maubot version uncertainty:** Assuming "recent but not latest" — some APIs
  may differ from current docs. May need to pin to a specific version and test.
- **Python async complexity:** The standup flow is stateful and interactive.
  Managing concurrent user flows in async Python requires care around shared
  state, cancellation, and error handling.
- **No test environment:** All testing against live homeserver increases risk
  of disruption during development. Consider creating a test room or using a
  staging homeserver if possible.
- **E2E encryption:** Maubot handles encryption, but its support may have
  different behavior or limitations vs. the current direct Olm usage.

### Estimated effort

**10–15 days** of focused development, including testing and deployment.

### What you get

- Modern Python codebase with maubot's plugin ecosystem.
- No direct Matrix client management (maubot handles login, sync, encryption).
- Hot-reloadable plugin (update without restarting the bot).
- Web management UI for configuration and monitoring.
- Potential to host other Matrix bots on the same instance.
- Cleaner separation of concerns (framework vs. bot logic).
- DB-backed flow persistence (more robust than current JSON-on-shutdown).
- Dual database support (SQLite for dev, PostgreSQL for production).

---

## Comparison

| Dimension                  | Option A: Update mautrix-go | Option B: Rewrite in maubot |
|----------------------------|-----------------------------|-----------------------------|
| **Effort**                 | 5–8 days                    | 10–15 days                  |
| **Risk of regressions**    | Low–Medium                  | Medium–High                 |
| **Language**               | Go (same)                   | Python (new)                |
| **Deployment model**       | Standalone binary (same)    | Maubot instance (new)       |
| **E2E encryption**         | Direct control (goolm)      | Framework-managed           |
| **C dependencies**         | None (goolm is pure Go)     | None                        |
| **Feature parity**         | Guaranteed (same code)      | Must be re-validated        |
| **Future maintenance**     | You maintain Matrix client code | Maubot maintains client code |
| **Hot reload**             | No (restart required)       | Yes                         |
| **Multi-bot hosting**      | No                          | Yes                         |
| **Operational familiarity**| Same as today               | New ops model               |

## Recommendation

**Option A is lower risk and faster** if the primary goal is to unblock
connectivity to modern Fedora Matrix homeservers. The codebase is only ~2000
lines of Go — the mechanical changes (context threading, handler signatures)
are tedious but straightforward, and you retain full feature parity with zero
translation risk.

**Option B makes sense if** you also want to reduce long-term maintenance
burden, plan to add more Matrix bots, or prefer Python. But the rewrite cost
is roughly double, and the stateful interactive flow is the hardest part to
get right in a new language and framework.
