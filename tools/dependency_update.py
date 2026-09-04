"""Scheduled dependency update: refresh uv.lock and open the MR. Not shipped.

Runs from the ``dependency-update`` CI schedule (see .gitlab-ci.yml). It records the
upgrade plan from ``uv lock --upgrade --dry-run``, applies the upgrade, and, when the
lock actually changed, pushes a ``deps-update-*`` branch and opens a merge request
whose description carries the plan. The MR's own pipeline then runs the gate, audit
included, over the new set. With ``--dry-run`` it only prints the plan.

Environment (CI provides all but the first): ``LITMUS_BOT_TOKEN`` (api +
write_repository), ``CI_API_V4_URL``, ``CI_PROJECT_ID``, ``CI_PROJECT_PATH``,
``CI_SERVER_HOST``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
BRANCH_PREFIX = "deps-update-"
ASSIGNEE_IDS = (1880678,)  # Ty
REVIEWER_IDS = (41761657,)  # Commander Zavala; the hourly MR watch keys on this
COMMIT_AUTHOR = (
    "Commander Zavala",
    "41761657-project_85833370_bot_c3b403cd44cf84b01f8e249c41b6f8d9@users.noreply.gitlab.com",
)


class DependencyUpdate:
    """One scheduled pass over the locked dependency set.

    ``run`` returns the process exit code. 0 means the set is current, an MR was
    opened, or an earlier update MR is still open; anything the update could not
    finish raises, so the scheduled pipeline goes red and somebody looks.
    """

    def __init__(self, *, dry_run: bool) -> None:
        self._dry_run = dry_run
        self._stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")

    def run(self) -> int:
        """Plan, upgrade, and publish; print what happened at each step."""
        plan = self._plan()
        if not plan:
            print("locked set is current; nothing to update")
            return 0
        print("upgrade plan:\n" + plan)
        if self._dry_run:
            return 0
        token = os.environ["LITMUS_BOT_TOKEN"]
        existing = self._open_update_mr(token)
        if existing is not None:
            print(f"update MR !{existing} is still open; not opening another")
            return 0
        _git("checkout", "-b", f"{BRANCH_PREFIX}{self._stamp}")
        _run("uv", "lock", "--upgrade")
        if _git("diff", "--quiet", "--", "uv.lock", check=False).returncode == 0:
            print("uv lock --upgrade left uv.lock unchanged after a non-empty plan")
            return 0
        _git(
            "-c",
            f"user.name={COMMIT_AUTHOR[0]}",
            "-c",
            f"user.email={COMMIT_AUTHOR[1]}",
            "commit",
            "-q",
            "-m",
            "Update the locked dependency set\n\n" + plan,
            "--",
            "uv.lock",
        )
        self._push(token)
        url = self._open_mr(token, plan)
        print(f"opened {url}")
        return 0

    def _plan(self) -> str:
        """The ``Update``/``Add``/``Remove`` lines of a dry-run upgrade, or empty."""
        result = _run("uv", "lock", "--upgrade", "--dry-run")
        lines = [
            line.strip()
            for line in (result.stdout + result.stderr).splitlines()
            if line.strip().startswith(("Update ", "Add ", "Remove "))
        ]
        return "\n".join(lines)

    def _open_update_mr(self, token: str) -> int | None:
        """The iid of an open MR from an earlier run, if one is still waiting."""
        payload = cast(
            "list[dict[str, Any]]",
            self._api(token, "GET", "merge_requests?state=opened&per_page=100"),
        )
        for mr in payload:
            if str(mr["source_branch"]).startswith(BRANCH_PREFIX):
                return int(mr["iid"])
        return None

    def _push(self, token: str) -> None:
        host = os.environ["CI_SERVER_HOST"]
        path = os.environ["CI_PROJECT_PATH"]
        # The token rides in the URL for this one push only; GitLab masks the variable
        # in job logs, and the remote is never saved to the checkout's config.
        _git("push", "-q", f"https://oauth2:{token}@{host}/{path}.git", "HEAD")

    def _open_mr(self, token: str, plan: str) -> str:
        body: dict[str, object] = {
            "source_branch": f"{BRANCH_PREFIX}{self._stamp}",
            "target_branch": "main",
            "title": f"Update the locked dependency set ({self._stamp[:8]})",
            "description": (
                "Scheduled dependency update (#72). Plan from `uv lock --upgrade --dry-run`:"
                f"\n\n```\n{plan}\n```\n\n"
                "The gate on this MR runs `make audit` over the new set."
            ),
            "remove_source_branch": True,
            "labels": "type::tooling",
            "assignee_ids": list(ASSIGNEE_IDS),
            "reviewer_ids": list(REVIEWER_IDS),
        }
        created = cast("dict[str, Any]", self._api(token, "POST", "merge_requests", body))
        return str(created["web_url"])

    @staticmethod
    def _api(
        token: str, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> object:
        url = f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}/{path}"
        data = None if body is None else json.dumps(body).encode()
        req = request.Request(
            url,
            data=data,
            method=method,
            headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=60) as response:
            return json.load(response)


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a tool from the repo root, capturing text output."""
    return subprocess.run(args, cwd=ROOT, check=check, capture_output=True, text=True)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run("git", *args, check=check)


if __name__ == "__main__":
    raise SystemExit(DependencyUpdate(dry_run="--dry-run" in sys.argv[1:]).run())
