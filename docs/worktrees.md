# Worktree runs

A task in a **git worktree** does not run in its project directory but in a `git worktree` of it, on a branch of its
own. A worktree is a tracked object of its own: several tasks can share one (a follow-up continues on the same branch),
and every worktree ends with **your decision** -- merge it, keep it, or discard it. ntasker never leaves one behind
unnoticed.

## Putting a task into a worktree

| Where | How |
|---|---|
| Task menu (`...`) | **New worktree** -- creates one and puts the task in it. **Join ntasker/N (#a, #b)** -- one entry per open worktree of the project. **Leave worktree** -- the task runs in the project directory again. |
| CLI | `ntasker add ... --worktree new\|<id>`, `ntasker patch <id> --worktree new\|<id>\|none`; `ntasker show` names it. |
| API | `POST /api/tasks/<id>/worktree` (new), `worktree_id` on `POST /api/tasks` / `PATCH /api/tasks/<id>` (join; `null` leaves). |

The project directory must be a git repository with at least one commit. A worktree only takes tasks of its own
project, and a task in a worktree cannot move to another project without leaving it first. Joining a *kept* worktree
puts it back in use.

The checkout is created right away: `<repo>/.claude/worktrees/ntasker-<id>` on branch `ntasker/<id>`, forked from the
main checkout's `HEAD` (both derived from the worktree's id). Inside the repo the agent inherits the repo's trust and
its `CLAUDE.md`; under `.claude/` project discovery never lists it as a project. `/.claude/worktrees/` is added to the
repo's local `info/exclude` -- no tracked `.gitignore` is touched and the main checkout stays clean. A checkout deleted
by hand is re-created from the branch on the next run.

Card and row show a `ntasker/<id>` badge.

## Runs

* **Where:** the run spawns in the checkout; `session_cwd` points there, so a resume continues there too.
* **Seed:** a *Workspace* section names the branch and the worktree's other tasks (with the `git log` range of their
  commits) and says: commit on this branch only, no checkout of other branches, no merge, no push -- the merge is your
  decision. A fasttrack task's commit therefore lands on the branch.
* **Lanes:** a worktree is a lane of its own. Its tasks run one after the other; they run in parallel to the project's
  ordinary runs and to other worktrees (see [task-queue.md](task-queue.md)).
* **Directory locks:** a task holds its worktree instead of the project directory; extra `locks` apply as usual. The
  `PreToolUse` hook lets it write inside its checkout and refuses the shared project directory (see
  [directory-locks.md](directory-locks.md)). `require_clean` checks the checkout.
* **Diff view:** the run's baseline is recorded for the checkout, so the Diff view shows the worktree's changes. After
  a merge or discard the checkout is gone; the Diff view then reads the branch's commits (`baseline..tip`) from the
  main repo. For a worktree shared by several tasks that range includes the commits of the tasks after it.
* **Failure:** if the checkout cannot be provided, the run does not start in the shared project directory -- the queue
  entry is flagged *ended* (the server log names the reason) and waits for you.

## The end decision

A worktree is **pending** while it is in use (`active`) and none of its tasks is open any more -- however the last one
was closed: the UI, `ntasker done`, a fasttrack run that finished itself, the API. Pending is derived, never stored, so
nothing can slip past it.

* Closing a task **in the UI** that leaves its worktree pending opens the decision dialog right away: branch, number
  of commits, uncommitted changes, the worktree's tasks.
* Everything else waits in the **Worktrees** list in the queue panel header (badge = pending count). Each entry has a
  decide button; a done task's `...` menu offers **Decide worktree** too.

| Decision | Effect |
|---|---|
| **Merge and remove** | Merges the branch into the main checkout's current branch (`git merge --no-ff`), then removes checkout and branch. Only when none of its tasks is open or running and the branch has commits. |
| **Keep for now** | Parks it (`kept`) for rework or a follow-up task that joins it. Leaves the pending list; joining puts it back in use. |
| **Discard** | Removes checkout **and** branch (uncommitted changes included) after 20 seconds. Until then the toast's **Undo** (or the list's undo button, `ntasker worktree undo <id>`) takes it back. Refused while one of its tasks runs. |

The rows stay as history (`GET /api/worktrees?all=true`); merge and discard record the branch tip (`tip_sha`). A
discarded branch can still be recovered with `git branch ntasker/<id> <tip_sha>` until git garbage-collects it.

### Merge: by ntasker or by an agent

ntasker merges by itself when that is clean: the main checkout is on a branch, has no uncommitted changes and is not
in use by a run, the worktree has no uncommitted changes, and the merge has no conflicts (a conflicting merge is aborted
at once -- the main checkout is as before).

Otherwise it creates a **merge task** -- `Merge ntasker/<id>: <first task's title>`, fasttrack with *continue on
failure*, the job and the reason in its description -- and queues it in the project's lane, so it waits until the
project directory is free. The worktree is `merging` meanwhile. Once that task is over (done, failed and out of the
queue, or deleted) the queue worker checks whether the branch is now part of the main checkout's `HEAD`: if so, the
worktree is cleaned up and `merged`; if not, it is pending again and the merge task's report says why.

## CLI

```
ntasker worktree list [--all] [--json]      # pending first; tasks still open are marked *
ntasker worktree merge|keep|discard|undo <id>
```

These talk to the running server (like `ntasker lock`).

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/tasks/{id}/worktree` | New worktree for the task -> 201 worktree; 400 no git repo, 409 already in one |
| GET | `/api/worktrees[?all=true]` | Open worktrees (all with `all`), pending first: `{id, project, branch, state, pending, base_branch, base_sha, tip_sha, merge_task_id, discard_at, tasks[], ...}`. No git calls. |
| GET | `/api/worktrees/{id}` | The same plus `facts: {commits, dirty, exists}` |
| POST | `/api/worktrees/{id}/merge` | 200 `{state: merged}` or 202 `{state: merging, task_id}`; 409 with the reason |
| POST | `/api/worktrees/{id}/keep` | `{state: kept}` |
| POST | `/api/worktrees/{id}/discard` | `{state: discarding, discard_at, delay}` |
| POST | `/api/worktrees/{id}/undo` | Takes a discard back: `{state: active}` |

States: `active` | `kept` | `merging` | `discarding` | `merged` | `discarded`.

## Edge cases

* **Project directory moved:** paths are derived fresh from the project; git's own worktree metadata may need
  `git worktree repair` -- not automated.
* **Main checkout on another branch than at creation, or detached:** the merge targets the checked-out branch; a
  detached `HEAD` goes to the agent.
* **Task deleted or archived:** it no longer counts as open, so the worktree may become pending.
* **Server restart while merging or discarding:** the state is stored; the worker picks it up on its first tick.

## Where the code lives

| File | Role |
|---|---|
| `src/ntasker/worktrees.py` | The entity: `create`, `ensure`, `check_join`, `summary`, `facts`, `keep`, `discard`, `undo_discard`, `merge`, `merge_job`, `sweep`. |
| `src/ntasker/claude_runner.py` | `_start_session` spawns in the checkout; `_workspace_section` for the seed. |
| `src/ntasker/locks.py` | `run_dirs` -- the checkout replaces the project directory. |
| `src/ntasker/taskqueue.py` | `_bucket` -- `wt:<id>` lane; `worktrees.sweep` on every tick. |
| `src/ntasker/rundiff.py` | `run_diff(gone=...)` -- the Diff view after the checkout is gone. |
| `src/ntasker/app.py` | `worktree_id` on create/update, `/api/tasks/{id}/worktree`, `/api/worktrees*`, the merge task, lock check. |
| `src/ntasker/cli.py` | `--worktree` on `add` / `patch`, `ntasker worktree ...`. |
| `src/ntasker/static/app.js`, `templates/index.html` | Menu, badge, Worktrees list, decision dialog, Undo toast. |
