"""Best-effort GitHub workflow dispatches: beerdev when Beerius hands it an ask, ops when a message
fails. Optional: without BEERDEV_GITHUB_TOKEN, scheduled runs catch up."""

import asyncio
import logging
import time

import httpx

from .config import settings

logger = logging.getLogger(__name__)
_last: dict[str, float] = {}
_tasks: set[asyncio.Task] = set()


def dispatch(workflow: str, *, every: float, enable: bool = False, **inputs: str) -> bool:
    """Start `workflow` in the background, at most once per `every` seconds per workflow+inputs.
    `enable` first turns it back on, in case it was disabled (GitHub disables schedules in a
    repository without commits for 60 days)."""
    if not settings.github_token:
        return False
    key = workflow + repr(sorted(inputs.items()))
    now = time.monotonic()
    if now - _last.get(key, float("-inf")) < every:
        return False
    _last[key] = now
    task = asyncio.create_task(_post(workflow, inputs, enable))
    _tasks.add(task)  # The event loop keeps only weak references to tasks.
    task.add_done_callback(_tasks.discard)
    return True


async def _post(workflow: str, inputs: dict[str, str], enable: bool) -> None:
    repo = settings.github_repository
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}"
    headers = {"Authorization": f"Bearer {settings.github_token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            if enable:
                enabled = await http.put(f"{url}/enable", headers=headers)
                if enabled.status_code != 204:
                    logger.warning("Enabling %s failed: HTTP %s", workflow, enabled.status_code)
            response = await http.post(
                f"{url}/dispatches", headers=headers, json={"ref": "main", "inputs": inputs}
            )
        if response.status_code != 204:
            logger.warning("Dispatch of %s failed: HTTP %s", workflow, response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("Dispatch of %s failed: %s", workflow, type(exc).__name__)
