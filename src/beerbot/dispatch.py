"""Best-effort GitHub workflow dispatches: beerdev when Beerius hands it an ask, ops when a message
fails. Optional: without GITHUB_DISPATCH_TOKEN, scheduled runs catch up."""

import asyncio
import logging
import time

import httpx

from .config import settings

logger = logging.getLogger(__name__)
_last: dict[str, float] = {}
_tasks: set[asyncio.Task] = set()


def dispatch(workflow: str, *, every: float, **inputs: str) -> bool:
    """Start `workflow` in the background, at most once per `every` seconds per workflow+inputs."""
    if not settings.github_dispatch_token:
        return False
    key = workflow + repr(sorted(inputs.items()))
    now = time.monotonic()
    if now - _last.get(key, float("-inf")) < every:
        return False
    _last[key] = now
    task = asyncio.create_task(_post(workflow, inputs))
    _tasks.add(task)  # The event loop keeps only weak references to tasks.
    task.add_done_callback(_tasks.discard)
    return True


async def _post(workflow: str, inputs: dict[str, str]) -> None:
    repo = settings.github_repository
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.post(
                url,
                headers={"Authorization": f"Bearer {settings.github_dispatch_token}"},
                json={"ref": "main", "inputs": inputs},
            )
        if response.status_code != 204:
            logger.warning("Dispatch of %s failed: HTTP %s", workflow, response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("Dispatch of %s failed: %s", workflow, type(exc).__name__)
