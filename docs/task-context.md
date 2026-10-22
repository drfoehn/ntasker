# Task context attachments

Plugin `task_context` (see [plugins.md](plugins.md)). A task can carry **attachments**: pointers to files, notes,
personas, skills and MCP servers the agent should have in hand when it starts. Attachments are pointers, not copies --
the file stays the single source of truth. The agent receives the list in its briefing at spawn, which is the whole
point: otherwise the user re-types the same three paths every run.

## Kinds

| Kind | Points at | Boundary |
|---|---|---|
| `file` | any file or folder on this machine | none -- named explicitly by the user |
| `note`, `doc`, `skill`, `member` | a file under a configured workspace root | `workspace_*_dir` settings |
| `mcp` | an MCP server declared in `~/.claude.json` (`mcp://<name>`) | must exist in that file |
| `task` | another tracker task (`task://<id>`; `#12` and `12` are accepted too) | must exist; not the task itself |

`member` files are Claude Code subagent definitions (`~/.claude/agents/<name>.md`); the briefing tells the agent to
delegate to them via the Agent tool rather than read them as prose. A `task` link tells the agent to read the linked
task with `ntasker show <id>` and leave it unchanged. Rows of any other kind (a fork's `brain` notes,
say) stay in the table but are ignored on read.

Because a `file` attachment escapes the workspace roots by design, the write endpoints rely on the origin guard
(`OriginGuardMiddleware`: `Origin` must match the host) so a foreign page cannot plant one.

## Where they show up

- **Create form** -- *Attach* opens the picker; picks collect in the draft and go with `POST /api/tasks` (`context`
  list), validated before the insert so a bad path never leaves a half-created task.
- **Edit modal** -- attach/detach writes straight through to the server, independent of Save.
- **Pasted images** -- `Ctrl+V` of an image (a screenshot from the clipboard) into either description field stores it
  under the user-data dir (`nTasker/uploads/`, via `POST /api/context/upload`) and attaches that file, exactly as if
  picked by hand. Text pastes are untouched.
- **Running task** -- one way for everything handed over mid-run: the reply box of the conversation pane.
  - The paperclip left of it opens the picker on the running task. Each entry attached there also becomes a chip of
    the next reply; on send, its briefing lines (`POST /api/tasks/{id}/context/brief`) go along with the text. A
    chip's close button detaches the entry again.
  - Files pasted or dropped onto the reply box (images, PDFs, anything) ship with the reply over the session's
    WebSocket like a terminal drop. The plugin's upload hook stores each under the user-data dir and attaches it as
    a `file` entry before its path is pasted into the session.
  - Everything attached mid-run also reaches later runs through the spawn briefing.
- **Upload cleanup** -- an hourly sweep (and one on boot) deletes files in the uploads dir that no unarchived task
  points at any more and that are older than a day (the grace period covers a paste into the create form). Archiving
  or deleting a task releases its uploads; *done* does not, a task can be reopened. Files outside the uploads dir are
  never touched.
- **Task cards** (list and board) -- chips; red when the file has moved away. Clicking a chip opens the entry in the
  desktop's default application (or in the workspace viewer when that plugin is enabled).
- **Briefing** -- `## Attached context` block in the compact seed and in the `/task` loader output; a `note` on an
  attachment ("why is this here") is passed verbatim.
- **CLI** -- `ntasker show <id>` prints an *Attached context* block.

## Picker

The *Files* tab opens the OS file dialog on this desktop (`POST /api/fs/pick`; macOS `osascript`, Linux `zenity`,
otherwise 501 and the buttons stay hidden) or takes pasted paths, one per line, each checked via `GET /api/fs/resolve`.
The *MCP servers* tab lists `~/.claude.json`, the *Tasks* tab the tracker's unarchived tasks (open first). Team /
Skills / Knowledge / Documents tabs appear only with the workspace plugin enabled and the directory
configured. A row in any list **toggles**: one click attaches the entry (check mark),
the next click detaches it again.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks/{id}/context` | attachments of one task |
| POST | `/api/tasks/{id}/context` | attach `{kind, path, label?, note?}`; re-attaching a path updates its note |
| DELETE | `/api/tasks/{id}/context/{cid}` | detach (never touches the file) |
| GET | `/api/tasks/{id}/context/{cid}/file` | preview payload of the attached file |
| POST | `/api/tasks/{id}/context/{cid}/reveal` | open the file in the desktop's default app |
| POST | `/api/tasks/{id}/context/brief` | `{ids: [...]}` -> `{text}`: briefing for a running session |
| POST | `/api/context/upload` | store an image pasted into a description `{name, data(base64)}`; returns `{path, name}` |
| GET | `/api/context/mcp` | MCP servers for the picker (no secrets) |
| GET / POST | `/api/fs/pick` | native dialog availability / open it |
| GET | `/api/fs/resolve?path=` | normalise a typed path, report existence |

`GET /api/tasks[...]` responses carry `context: [...]` per task while the plugin is enabled.

## Schema

Table `task_context(id, task_id, kind, path, label, note, created_at)`, unique per `(task_id, path)`, cascades with
the task. Created by the plugin's schema hook on every `init_db()` -- also while the plugin is disabled, so data
survives a toggle.
