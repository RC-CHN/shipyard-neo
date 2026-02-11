"""Browser skill learning scheduler lifecycle helpers."""

from __future__ import annotations

import structlog

from app.config import get_settings
from app.services.skills.scheduler import BrowserLearningScheduler

logger = structlog.get_logger()

_browser_learning_scheduler: BrowserLearningScheduler | None = None


async def init_browser_learning_scheduler() -> BrowserLearningScheduler:
    """Initialize browser learning scheduler and optionally start it."""
    global _browser_learning_scheduler

    settings = get_settings()
    config = settings.browser_learning
    _browser_learning_scheduler = BrowserLearningScheduler(config=config)

    logger.info(
        "skills.browser.scheduler.init",
        enabled=config.enabled,
        run_on_startup=config.run_on_startup,
        interval_seconds=config.interval_seconds,
        batch_size=config.batch_size,
        auto_release_enabled=settings.browser_auto_release_enabled,
    )

    if not config.enabled:
        return _browser_learning_scheduler

    if config.run_on_startup:
        try:
            await _browser_learning_scheduler.run_once()
        except Exception as exc:
            logger.exception(
                "skills.browser.scheduler.startup_cycle_failed",
                error=str(exc),
            )

    await _browser_learning_scheduler.start()
    return _browser_learning_scheduler


async def shutdown_browser_learning_scheduler() -> None:
    """Stop browser learning scheduler."""
    global _browser_learning_scheduler
    if _browser_learning_scheduler is not None:
        await _browser_learning_scheduler.stop()
        _browser_learning_scheduler = None


def get_browser_learning_scheduler() -> BrowserLearningScheduler | None:
    """Get global browser learning scheduler instance."""
    return _browser_learning_scheduler
