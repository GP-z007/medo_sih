"""Durable operator loop. Only the worker calls browser validation."""
import asyncio
from decimal import Decimal

from sqlalchemy import select

from udaan.config import settings
from udaan.contracts import DomainError, Inspection, PageState
from udaan.db import Job, JobEvent, OperatorCommand, event, now, session
from udaan.services import get


def elapsed(job: Job, instant=None) -> float:
    instant = instant or now()
    return float(job.wait_used_seconds or 0) + (
        max(0, (instant - job.wait_started_at).total_seconds()) if job.wait_started_at else 0)


def end_wait(db, job: Job, state: str, code: str | None, reason: str, instant=None):
    instant = instant or now()
    challenge = get(db, JobEvent, job.challenge_id) if job.challenge_id else None
    used = elapsed(job, instant)
    if challenge:
        details = dict(challenge.details)
        key = {"RUNNING": "resumed_at", "CANCELLED": "cancelled_at"}.get(state, "ended_at")
        details.update({key: instant.isoformat(), "total_wait_seconds": used})
        if code:
            details["timeout_reason" if code == "OPERATOR_WAIT_TIMEOUT" else "terminal_reason"] = code
        challenge.details = details
    job.wait_used_seconds = Decimal(str(used))
    job.wait_started_at = None
    job.challenge_id = None
    job.state, job.error_code, job.reason = state, code, reason
    if state != "RUNNING":
        job.finished_at = instant
    if state == "CANCELLED":
        db.query(OperatorCommand).filter(OperatorCommand.job_id == job.id,
            OperatorCommand.challenge_id == (challenge.id if challenge else ""),
            OperatorCommand.action == "CANCEL", OperatorCommand.state == "PENDING").update(
                {"state": "DONE", "result": "Job cancelled"})
    db.query(OperatorCommand).filter(OperatorCommand.job_id == job.id, OperatorCommand.state == "PENDING").update(
        {"state": "STALE", "result": "Challenge has ended"})
    event(db, job, "JOB_RESUMED" if state == "RUNNING" else f"JOB_{state}", reason=reason, code=code)


class ChallengeFlow:
    def __init__(self, job_id: str, owner: str, browser, inspect, session_factory=session,
                 max_wait: float | None = None, poll_seconds=0.25):
        self.job_id, self.owner, self.browser, self.inspect = job_id, owner, browser, inspect
        self.db_session = session_factory
        self.max_wait = settings().operator_wait_seconds if max_wait is None else max_wait
        self.poll_seconds = poll_seconds

    def owned(self, db):
        job = get(db, Job, self.job_id, lock=True)
        if (job.owner != self.owner or job.state not in {"RUNNING", "WAITING_FOR_OPERATOR"}
                or job.lease_until is None or job.lease_until <= now()):
            raise DomainError("WORKER_LEASE_LOST", "Worker no longer owns this active job")
        return job

    async def wait(self, detected: Inspection):
        with self.db_session() as db:
            job = self.owned(db)
            if job.state != "WAITING_FOR_OPERATOR":
                start = now()
                job.state, job.wait_started_at = "WAITING_FOR_OPERATOR", start
                job.reason = detected.reason
                challenge = event(db, job, "CHALLENGE_DETECTED", detected_at=start.isoformat(),
                                  challenge_type=detected.challenge_type, wait_started_at=start.isoformat(),
                                  confirmation_attempts=0, checkpoint=job.checkpoint,
                                  reason=detected.reason, retry_after=detected.retry_after.isoformat() if detected.retry_after else None,
                                  response_host=getattr(self.browser, "rate_limit_host", None) if detected.challenge_type == "RATE_LIMIT" else None)
                job.challenge_id = challenge.id
        # Browser remains owned by the enclosing collection context throughout this loop.
        while True:
            if await self.tick(detected):
                return
            await asyncio.sleep(self.poll_seconds)

    async def tick(self, detected: Inspection) -> bool:
        with self.db_session() as db:
            job = self.owned(db)
            failure = None
            if job.cancel_requested:
                failure = ("CANCELLED", "OPERATOR_CANCELLED", "Operator cancelled job")
            elif not self.browser.alive:
                failure = ("FAILED", "BROWSER_SESSION_LOST", "Udaan Browser session closed")
            elif elapsed(job) >= self.max_wait:
                failure = ("BLOCKED", "OPERATOR_WAIT_TIMEOUT", "Configured total operator wait exceeded")
            if failure:
                end_wait(db, job, *failure)
            else:
                command = db.scalar(select(OperatorCommand).where(
                    OperatorCommand.job_id == job.id, OperatorCommand.state == "PENDING")
                    .order_by(OperatorCommand.created_at, OperatorCommand.id).with_for_update().limit(1))
                if command is None:
                    return False
                if command.challenge_id != job.challenge_id:
                    command.state, command.result = "STALE", "Challenge no longer active"
                    return False
                if command.action == "CANCEL":
                    command.state, command.result = "DONE", "Job cancelled"
                    failure = ("CANCELLED", "OPERATOR_CANCELLED", "Operator cancelled job")
                    end_wait(db, job, *failure)
                else:
                    challenge = get(db, JobEvent, job.challenge_id)
                    details = dict(challenge.details)
                    if command.action == "CONFIRM":
                        details["confirmation_attempts"] += 1
                    challenge.details = details
                    command.state = "PROCESSING"
                    command_id, challenge_id = command.id, job.challenge_id
        if failure:
            raise DomainError(failure[1], failure[2])
        try:
            if command.action == 'OPEN_TAB':
                await self.browser.page.bring_to_front()
                with self.db_session() as db:
                    record = get(db, OperatorCommand, command_id)
                    record.state, record.result = 'DONE', 'Opened this child job in Udaan Browser'
                return False
            if detected.retry_after and now() < detected.retry_after:
                result = Inspection(state=PageState.CHALLENGE, challenge_type="RATE_LIMIT",
                                    reason="Server cooldown is still active", retry_after=detected.retry_after)
            else:
                result = await self.inspect()
        except Exception:
            result = Inspection(state=PageState.INVALID, reason="Page inspection failed; recheck browser state")
        with self.db_session() as db:
            job = self.owned(db)
            command = get(db, OperatorCommand, command_id)
            if job.challenge_id != challenge_id:
                command.state, command.result = "STALE", "Challenge changed"
                return False
            command.state, command.result = "DONE", result.reason
            event(db, job, "CHALLENGE_RECHECKED", challenge_id=challenge_id,
                  action=command.action, state=result.state, reason=result.reason)
            if job.cancel_requested:
                failure = ("CANCELLED", "OPERATOR_CANCELLED", "Operator cancelled job")
            elif not self.browser.alive:
                failure = ("FAILED", "BROWSER_SESSION_LOST", "Udaan Browser session closed")
            elif elapsed(job) >= self.max_wait:
                failure = ("BLOCKED", "OPERATOR_WAIT_TIMEOUT", "Configured total operator wait exceeded")
            if failure:
                end_wait(db, job, *failure)
            elif result.state == PageState.READY:
                if result.checkpoint:
                    job.checkpoint = result.checkpoint
                end_wait(db, job, "RUNNING", None, "Page validated; safe checkpoint available")
                return True
            else:
                job.reason = result.reason
        if failure:
            raise DomainError(failure[1], failure[2])
        return False
