"""One headed process, isolated per-job contexts/pages, bounded by durable claims."""
import asyncio
import logging

from udaan.contracts import DomainError

log = logging.getLogger(__name__)


class ParallelBrowserSlots:
    def __init__(self):
        self.manager = self.browser = None
        self.lock = asyncio.Lock()
        self.slots = set()

    async def _dispose(self):
        manager = self.manager
        self.manager = self.browser = None
        if manager:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                # A dead browser may reject protocol cleanup. It must never remain cached.
                log.warning("browser_pool_cleanup_failed process_state=discarded")

    async def acquire(self, slot):
        async with self.lock:
            if self.browser is not None and not self.browser.is_connected():
                if self.slots:
                    # Never replace a process underneath another job or a human challenge.
                    raise DomainError('BROWSER_SESSION_LOST', 'The shared Udaan Browser process stopped')
                await self._dispose()
            if self.browser is None:
                from camoufox.async_api import AsyncCamoufox

                from udaan.browser import ensure_browser_branding
                from udaan.config import settings
                from udaan.health import browser_health
                health = await browser_health()
                if health['status'] != 'READY':
                    raise DomainError('BROWSER_UNAVAILABLE', health['reason'])
                await asyncio.to_thread(ensure_browser_branding)
                self.manager = AsyncCamoufox(headless=False, env={'DISPLAY': settings().display},
                    humanize=False, block_images=False, disable_coop=False,
                    firefox_user_prefs={'javascript.enabled': True})
                self.browser = await self.manager.__aenter__()
            if not self.browser.is_connected():
                raise DomainError('BROWSER_SESSION_LOST', 'The shared Udaan Browser process stopped')
            self.slots.add(slot)
            return self.browser

    async def release(self, slot):
        async with self.lock:
            self.slots.discard(slot)
            if not self.slots:
                await self._dispose()

    async def close(self):
        async with self.lock:
            await self._dispose()
            self.slots.clear()
