"""HTTP routes of the Claude plugin.

``GET /api/claude/usage`` feeds the topbar limits widget;
``POST /api/claude/sessions/{task_id}/limit`` is the ``StopFailure`` hook's
report that a usage limit stopped a session. Mounted with a
``require_enabled`` dependency, so it 404s while the plugin is off.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ntasker import claude_runner, usagelimit
from ntasker.db import get_conn
from ntasker.i18n import _
from ntasker.plugins.claude import usage

router = APIRouter()


@router.get("/api/claude/usage")
def get_usage(fresh: bool = False) -> JSONResponse:
    """``{"usage": {five_hour, seven_day} | null}`` -- ``null`` without a subscription login.

    ``fresh=1`` asks past the usual cache window (the topbar sends it the
    moment a Claude session starts or ends), rate-limited server-side.
    """
    return JSONResponse({"usage": usage.snapshot(force=fresh)})


@router.post("/api/claude/sessions/{task_id}/limit")
def post_limit(task_id: int) -> JSONResponse:
    """A usage limit ended the session's turn: pause the run until the limit resets.

    Called by ``ntasker hook limit`` (``StopFailure`` with ``rate_limit``).
    The reset time comes from the subscription's usage windows, else from the
    limit message in the transcript (:func:`ntasker.usagelimit.reset_time`);
    the queue worker resumes the run then
    (:func:`ntasker.taskqueue.resume_paused`). Answers ``{"resume_at": ISO}``
    -- with no known reset, the moment nTasker looks again. 404 when the task
    has no live session.
    """
    if task_id not in claude_runner.active_session_ids():
        raise HTTPException(status_code=404, detail=_("No live session for this task"))
    with get_conn() as conn:
        row = conn.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    limit = usagelimit.detect(row["session_id"]) if row else None
    reason = (limit or {}).get("text", "")
    at = usagelimit.park(task_id, usagelimit.reset_time(reason), reason)
    return JSONResponse({"resume_at": datetime.fromtimestamp(at).astimezone().isoformat()})
