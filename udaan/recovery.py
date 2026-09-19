"""Bounded ordinary-page recovery, explicitly excluded after security evidence."""
import asyncio

from udaan.contracts import DomainError

RECOVERABLE = {'NAVIGATION_TIMEOUT', 'EXTRACTION_STRUCTURE_FAILED', 'BROWSER_SESSION_LOST'}


async def recover_ordinary(browser, error, attempt, guard):
    profile = browser.profile
    if (not isinstance(error, DomainError) or error.code not in RECOVERABLE
            or attempt >= profile.recovery_attempts or browser.barrier_seen):
        return False
    if browser.alive:
        await guard()
    if browser.barrier_seen:
        return False
    await asyncio.sleep(min(8, profile.backoff_seconds * 2 ** attempt))
    # Recheck immediately before replacing any page/context.
    if browser.alive:
        await guard()
    await browser.recover_page()
    return True
