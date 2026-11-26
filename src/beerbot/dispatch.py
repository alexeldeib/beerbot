"""Best-effort GitHub workflow dispatches: a beerdev build when beerdev files or approves a request,
ops when a message or reply fails or ops goes silent. Without BEERDEV_GITHUB_TOKEN nothing is
started: ops still runs on its schedule, and builds wait for the next start."""

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
    `enable` first turns it back on if GitHub disabled it for inactivity (60 days without
    commits). A workflow the owner disabled by hand stays off and isn't started."""
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
                state = (await http.get(url, headers=headers)).json().get("state")
                if state == "disabled_manually":  # Paused on purpose: leave it paused.
                    logger.warning("%s is disabled by hand, so it wasn't started", workflow)
                    return
                if state == "disabled_inactivity":
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
