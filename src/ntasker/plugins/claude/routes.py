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

from ntasker import claude_runner
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
    """A usage limit ended the session's turn: park it until the limit resets.

    Called by ``ntasker hook limit`` (``StopFailure`` with ``rate_limit``).
    The reset time comes from the subscription's usage windows
    (:func:`usage.limit_reset`); the queue worker resumes the session then
    (:func:`ntasker.claude_runner.resume_limited`). Answers ``{"resume_at":
    ISO | null}`` -- ``null`` when no reset time is known (no auto-resume).
    404 when the task has no live session.
    """
    if not claude_runner.set_limited(task_id, usage.limit_reset()):
        raise HTTPException(status_code=404, detail=_("No live session for this task"))
    at = claude_runner.limit_resume_at(task_id)
    return JSONResponse({"resume_at": datetime.fromtimestamp(at).astimezone().isoformat() if at else None})
