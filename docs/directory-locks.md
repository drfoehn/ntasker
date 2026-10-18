# Directory locks

Two agents editing the same working directory at the same time is the one thing the [task queue](task-queue.md) exists
to prevent. Its one-lane-per-project rule covers the common case; directory locks cover the rest: a task whose work
spans **several repos**, and a directory that is busy because of a task from *another* project.

## Model

* A run always holds **its own project's directory** -- a task in a git worktree holds that worktree's checkout instead
  (see [worktrees.md](worktrees.md)).
* A task may additionally lock other projects' directories: the `locks` field, a list of project names. The own project
  is implicit and never stored there (it is dropped on write).
* A task without a project holds only its explicit locks.
* The lock key at runtime is the **resolved directory** (`realpath` of the project's directory, see
  `claude_runner.default_cwd_for_project`), not the name -- two names for one path collide as they should.

## Start condition

With `dir_locks` on (the default), the queue worker starts a task only when

1. its project lane is free (the existing one-per-project rule), **and**
2. none of its directories is held by a live session of another task, **and**
3. with `require_clean` on: every one of its directories is git-clean (`git status --porcelain` empty; a directory that
   is not a repo counts as clean).

Otherwise the entry stays queued and the worker looks at the next task in that lane. The queue panel labels such an
entry with a lock badge: `#<holder>` for a held directory (tooltip names the project and the holding task), or
`dirty: <project>` for the clean gate.

The git check only runs for a candidate that passed every cheaper gate, so an idle queue costs nothing.

With `dir_locks` off only the lane rule applies -- exactly the pre-3.1 behaviour.

## Granting locks

| Where | How |
|---|---|
| New-task form / edit dialog | **Also locks** chip input -- pick from the dropdown (click / Tab / Enter) or type a name + Enter; the new-task form also takes a dropped sidebar project. |
| CLI | `ntasker add --locks a,b`, `ntasker patch <id> --locks a,b` (`''` clears). |
| CLI, against the running server | `ntasker lock add <id> <project...>`, `ntasker lock rm <id> <project...>`, `ntasker lock list <id>`. |
| API | `POST /api/tasks/<id>/locks {"projects": [...]}`, `DELETE /api/tasks/<id>/locks/<project>`, or the `locks` field on `POST`/`PATCH /api/tasks`. |

A grant is **all or nothing** and immediate: when the task has a live session and another live task holds one of the
requested directories, the request is refused with `409 "<project> is held by task #N"` (`ntasker lock add` prints
that and exits 1). A task without a live session is not checked -- the worker decides at start time. "Live" means a
session ntasker spawned or an external `/task` session.

### Waiting for a held lock

`ntasker lock add <id> <project...> --wait` (API: `"wait": true`) queues the task instead of failing:
`202 {"waiting": [{project, holder}]}`, a `lock_requests` row per project, and a `wait` message to each holder. The
queue worker grants the queued locks on the first tick all of them are free -- all or nothing, oldest request first,
and before a queued task could start into the freed directory -- and messages the waiter (*nTasker granted you the
lock on X*). A request of a session that ended is dropped. `ntasker peers` shows what a task waits for.

A wait that would close a circle -- the holder already waits, directly or through others, for the requester; lock
requests and dependencies (`ntasker wait-for`) both count -- is refused with `409 "deadlock: ..."`, nothing is queued,
and the other side gets a message naming the circle. The two agents then agree who gives way (see
[messaging.md](messaging.md#deadlocks)). Module: `src/ntasker/lockqueue.py`.

`ntasker lock` talks to the server rather than the DB because the check needs the live session registry. It reads the
server address from `--host/--port`, else from `NTASKER_URL` -- which `ntasker serve` sets in the environment of every
session it spawns, so an agent inside a run needs no flags.

Board rows and kanban cards show a small lock badge with the count of extra locks; the tooltip lists the projects.

## Releasing locks

`ntasker finish <id>` (`POST /api/tasks/<id>/outcome`) clears the task's extra locks -- with any `--status`, since
`finish` is the run's last command in every case. The release is part of the same `UPDATE` as the status / phase
transition, so the two cannot drift apart, and the next worker tick (2s) starts whatever was waiting for one of those
directories. Nothing is notified: the worker re-reads the queue from scratch each tick.

The task's **own** project directory is not a lock but a property of the task, so it is never cleared this way. It
stays held while the session lives -- which is what keeps the lane busy during your review -- and stops counting once
the task is `done` or the session is gone (`held_dirs`). Running sessions that wait for the task (`ntasker wait-for`)
are told when it goes to review that the directory stays locked, and get a second message once the task is `done` --
see [messaging.md](messaging.md#events). A session that ends while its task is still open frees the directory as well;
the worker tells the live dependents on its next tick.

A resumed run that needs a released directory again takes it with `ntasker lock add <id> <project>`, and is refused if
someone else holds it by then. Manual release stays available: `ntasker lock rm`, `DELETE /api/tasks/<id>/locks/<p>`,
or the chip input in the edit dialog.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `dir_locks` | `on` | Honour directory locks in the worker, the lock API and the Claude Code hook. `off` = lanes only. |
| `require_clean` | `off` | Additionally require every held directory to be git-clean before a start. |
| `quicktasks_bypass_lanes` | `on` | Quicktasks skip lanes, locks and the git check, and hold nothing while they run (see [task-queue.md](task-queue.md)). |

Both are switches on the `/settings` *Agents & runs* tab; ENV `NTASKER_DIR_LOCKS` / `NTASKER_REQUIRE_CLEAN`.

## Path check

`GET /api/locks/check?task=N&path=P` -> `{"allowed": bool, "project": str|null, "holder": int|null}` answers whether
task `N` may write to `P`. It refuses only when `P` lies inside another **known** project's directory (a project any
task names, or a discovered agent project; longest match wins) that the task does not hold. Paths belonging to no
project -- temp files, plan files, dotfiles -- always pass, so tooling that writes outside the repos keeps working.
The home directory and the system temp dir never count as a project, even when an agent was once launched there.
This is what the Claude Code `PreToolUse` hook asks.

## Hooks (Claude Code)

Every Claude Code session ntasker spawns gets an extra settings file via `claude --settings <file>` -- two static files
shipped as package data in `src/ntasker/claude_assets/hooks/`:

| File | Passed when | Hooks |
|---|---|---|
| `base.json` | `dir_locks` off | `SessionStart` (`clear`) -> `ntasker hook clear`; `Stop`, `Notification` (`permission_prompt`) -> `ntasker hook waiting`; `UserPromptSubmit`, `PostToolUse` -> `ntasker hook running`; `PreToolUse` (`AskUserQuestion`) -> `ntasker hook ask`; `StopFailure` (`rate_limit`) -> `ntasker hook limit` |
| `locks.json` | `dir_locks` on | the same, plus `PreToolUse` (`Edit\|Write\|MultiEdit\|NotebookEdit\|Bash`) -> `ntasker hook pretooluse` |

The state hooks give the board an **explicit** waiting / running signal instead of the output-silence heuristic (see
[claude-runs.md](claude-runs.md#session-indicators----running-vs-waiting)). A subagent's steps fire the same hooks
(their payload carries `agent_id`); `hook running` ignores those, so a background agent at work cannot flag a session
"running" while it waits on the user and drop its open question. The `ask` hook hands the agent's open
question to the conversation pane (see [claude-runs.md](claude-runs.md#the-run-view----conversation-terminal-split)).
The `clear` hook moves the task to the new session id Claude Code starts on `/clear`.
The lock hook refuses a tool call whose
target lies inside another project's directory the task does not hold: exit 2 with

```
<path> is not locked by task #N -- run `ntasker lock add N <project>` if it is free (`--wait` queues you for it if
another task holds it), or leave it to a task in that project
```

which Claude Code shows to the agent. For `Bash` only a leading `cd <dir>` is inspected (resolved against the hook's
`cwd`); every other command passes. The hook never resolves paths itself -- it asks `GET /api/locks/check`.

The hook commands find their task and server through `NTASKER_TASK_ID` and `NTASKER_URL`, which the runner puts into
the session's environment (`ntasker serve` sets `NTASKER_URL` from its bind). The runner also puts its own `bin`
directory first on the session's `PATH`, so the bare `ntasker` the hooks call is always the server's version -- a stale
install elsewhere on `PATH` would otherwise fail every hook. Outside such a session, with the server
unreachable, or on any error the hooks exit 0 silently -- a hook must never break a session. Your
`~/.claude/settings.json` is never touched; `--settings` layers the file on top for that process only. OpenCode and Pi
have no settings flag and keep the heuristic.

## Where the code lives

| File | Role |
|---|---|
| `src/ntasker/locks.py` | `parse`/`dump`/`normalize`, `resolve_dir`, `task_dirs`, `held_dirs`, `conflict`, `dirty_dir`, `project_for_path`. |
| `src/ntasker/lockqueue.py` | `lock add --wait`: `request`, `grant` (worker), `deadlock`. |
| `src/ntasker/taskqueue.py` | `_lock_reason` (shared by the start step and `skipped`), the gate and the grant in `tick`. |
| `src/ntasker/app.py` | `locks` on create/update, `/api/tasks/<id>/locks`, `/api/locks/check`, the release in `/api/tasks/<id>/outcome`, `skipped` in `/api/queue`. |
| `src/ntasker/cli.py` | `ntasker lock add\|rm\|list`, `--locks` on `add`/`patch`, `ntasker hook waiting\|running\|ask\|limit\|pretooluse`. |
| `src/ntasker/claude_assets/hooks/*.json`, `claude_assets.hooks_settings_path` | The two `--settings` files and which one a spawn gets. |
| `src/ntasker/agents.py`, `plugins/claude` | `AgentSpec.settings_flag`, `build_spawn(settings_path=)`. |
| `src/ntasker/claude_runner.py` | `TermSession.hook_waiting`, `set_hook_state`, `NTASKER_TASK_ID`/`NTASKER_URL` in the child env. |
| `src/ntasker/static/app.js` | Lock chips (`lockSuggestions`, `selectLock`, `commitLockInput`, `onChipDrop`), badge, `queueSkipped`. |
