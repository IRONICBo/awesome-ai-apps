"""Route a read-only social research goal through Jev and the local socai CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

DECISION_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"
PLATFORM_HOSTS = {
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
    "linkedin": ("linkedin.com",),
}
ROUTES = {f"{platform}_search": platform for platform in PLATFORM_HOSTS}
PUBLIC_FIELDS = (
    "title",
    "caption",
    "description",
    "text",
    "author",
    "username",
    "nickname",
    "media_type",
    "type",
    "likes",
    "like_count",
    "comments",
    "comment_count",
    "views",
    "view_count",
)
URL_FIELDS = ("url", "web_url", "share_url", "post_url", "video_url")
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
PROJECT_DIR = Path(__file__).resolve().parent


class JevSocialError(Exception):
    """A concise, user-actionable integration failure."""


def decision_request(goal: str, requested_platform: str, model: str) -> dict[str, Any]:
    """Build the bounded Jev decision request."""
    return {
        "model": model,
        "state": {
            "request": goal,
            "requested_platform": requested_platform,
            "supported_workflows": [
                "Read-only Instagram search via the local socai CLI",
                "Read-only TikTok search via the local socai CLI",
                "Read-only LinkedIn search via the local socai CLI",
            ],
        },
        "questions": {
            "route": {
                "type": "choice",
                "instructions": {
                    "task": "Choose the one supported workflow that should execute this request.",
                    "rules": [
                        "Honor an explicit requested_platform.",
                        "Choose unsupported unless this is a read-only social research request.",
                        "Never choose a workflow for posting, engagement, or messaging.",
                    ],
                },
                "criteria": {
                    "instagram_search": "Research Instagram posts, profiles, or reels.",
                    "tiktok_search": "Research TikTok creators or videos.",
                    "linkedin_search": "Research LinkedIn people, companies, or posts.",
                    "unsupported": "Anything else, including remote account changes.",
                },
            }
        },
    }


def validate_decision(payload: Any, requested_platform: str) -> dict[str, Any]:
    """Validate Jev output before it can influence a subprocess command."""
    if not isinstance(payload, dict):
        raise JevSocialError("Jev returned an invalid response object.")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JevSocialError("Jev returned an invalid answers object.")
    answer = answers.get("route")
    if not isinstance(answer, dict):
        raise JevSocialError("Jev returned an invalid route answer.")

    route = answer.get("choice")
    confidence = answer.get("confidence")
    if answer.get("type") != "choice" or route not in {*ROUTES, "unsupported"}:
        raise JevSocialError("Jev returned an invalid route.")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise JevSocialError("Jev returned an invalid confidence value.")
    if not 0 <= confidence <= 1:
        raise JevSocialError("Jev confidence was outside the supported range.")

    platform = ROUTES.get(route)
    if platform is None:
        raise JevSocialError("Jev rejected this request as unsupported or not read-only.")
    if requested_platform != "auto" and platform != requested_platform:
        raise JevSocialError(
            f"Jev selected {platform}, which conflicts with explicit platform "
            f"{requested_platform}."
        )

    probabilities = answer.get("probabilities")
    if probabilities is not None and (
        not isinstance(probabilities, dict)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
            for value in probabilities.values()
        )
    ):
        raise JevSocialError("Jev returned invalid route probabilities.")

    return {
        "route": route,
        "platform": platform,
        "confidence": float(confidence),
        "model": str(payload.get("model") or "unknown"),
    }


def call_jev(
    goal: str,
    requested_platform: str,
    api_key: str,
    model: str,
    timeout: int = 20,
) -> tuple[dict[str, Any], int]:
    """Call the Jev Decisions API and return a validated decision plus latency."""
    if not api_key:
        raise JevSocialError("OPENROUTER_API_KEY is not set.")

    request = urllib.request.Request(
        DECISION_URL,
        data=json.dumps(decision_request(goal, requested_platform, model)).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "awesome-ai-apps-jev-social-research-agent",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_OUTPUT_BYTES + 1)
            if len(raw) > MAX_OUTPUT_BYTES:
                raise JevSocialError("OpenRouter response exceeded the safe size limit.")
            payload = json.loads(raw)
    except urllib.error.HTTPError as error:
        raise JevSocialError(
            f"OpenRouter rejected the Jev decision (HTTP {error.code})."
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise JevSocialError("Could not reach OpenRouter for the Jev decision.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise JevSocialError("OpenRouter returned an invalid Jev response.") from error

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return validate_decision(payload, requested_platform), elapsed_ms


def resolve_socai(candidate: str) -> str:
    """Resolve an explicit socai binary or fall back to PATH."""
    if candidate:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        resolved = shutil.which(candidate)
    else:
        resolved = shutil.which("socai")
    if not resolved:
        raise JevSocialError("The local socai CLI is not installed or not executable.")
    return resolved


def build_socai_command(
    executable: str, platform: str, goal: str, limit: int
) -> list[str]:
    """Build one fixed read-only command without invoking a shell."""
    if platform not in PLATFORM_HOSTS:
        raise JevSocialError("Refusing to build a command for an unsupported platform.")
    if not 1 <= limit <= 20:
        raise JevSocialError("Result limit must be between 1 and 20.")
    return [
        executable,
        platform,
        "search",
        "--num",
        str(limit),
        "--pretty",
        "--",
        goal,
    ]


def collect_bounded(
    command: list[str], child_env: dict[str, str], timeout: int, max_output_bytes: int
) -> tuple[int, str, str]:
    """Capture both streams without waiting on pipe handles inherited by grandchildren."""

    def terminate_tree(process: subprocess.Popen[Any]) -> None:
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if result.returncode != 0 and process.poll() is None:
                    process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                process.kill()

    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_options["start_new_session"] = True

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                env=child_env,
                **popen_options,
            )
        except OSError as error:
            raise JevSocialError("Could not start the local socai CLI.") from error

        deadline = time.monotonic() + timeout
        timed_out = False
        overflow = False
        while process.poll() is None:
            total = os.fstat(stdout_file.fileno()).st_size + os.fstat(
                stderr_file.fileno()
            ).st_size
            if total > max_output_bytes:
                overflow = True
                terminate_tree(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                terminate_tree(process)
                break
            time.sleep(min(0.05, remaining))

        # The subprocess owns a fresh process group. Clean it even after the
        # leader exits so descendants cannot retain output handles or keep
        # writing to the temporary capture files.
        terminate_tree(process)
        process.wait()
        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        total = stdout_size + stderr_size
        if total > max_output_bytes:
            overflow = True
        if timed_out:
            raise JevSocialError("socai timed out before the read-only search finished.")
        if overflow:
            raise JevSocialError("socai output exceeded the safe size limit.")

        stdout_file.seek(0)
        stderr_file.seek(0)
        try:
            stdout = stdout_file.read(stdout_size).decode("utf-8")
            stderr = stderr_file.read(stderr_size).decode("utf-8")
        except UnicodeDecodeError as error:
            raise JevSocialError("socai returned output that was not valid UTF-8.") from error
        return process.returncode, stdout, stderr


def run_socai(
    executable: str,
    platform: str,
    goal: str,
    limit: int,
    timeout: int = 180,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[Any, int]:
    """Execute one bounded local search and parse its JSON response."""
    command = build_socai_command(executable, platform, goal, limit)
    child_env = os.environ.copy()
    child_env.pop("OPENROUTER_API_KEY", None)
    started = time.monotonic()
    returncode, stdout, _stderr = collect_bounded(
        command, child_env, timeout, max_output_bytes
    )
    if returncode != 0:
        raise JevSocialError("socai could not complete the read-only search.")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise JevSocialError("socai returned invalid JSON.") from error
    return payload, int((time.monotonic() - started) * 1000)


def candidate_records(payload: Any) -> list[Any]:
    """Find the first non-empty known result list in a socai response."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "posts", "videos"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return value
    for key in ("result", "data"):
        found = candidate_records(payload.get(key))
        if found:
            return found
    return []


def public_url(record: dict[str, Any], platform: str) -> str:
    """Return only an HTTPS source URL for the selected platform."""
    allowed_hosts = PLATFORM_HOSTS[platform]
    for key in URL_FIELDS:
        value = record.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = urlparse(value)
            hostname = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if parsed.scheme == "https" and any(
            hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts
        ):
            return value
    return ""


def scalar(value: Any) -> str:
    """Extract a short public scalar from common nested structures."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, dict):
        for key in ("name", "username", "nickname", "title", "text"):
            nested = value.get(key)
            if isinstance(nested, (str, int, float)) and not isinstance(nested, bool):
                return str(nested).strip()
    return ""


def safe_text(value: str, width: int = 180) -> str:
    """Bound and neutralize untrusted text before Markdown rendering."""
    cleaned = " ".join(value.split())
    shortened = cleaned if len(cleaned) <= width else f"{cleaned[: width - 1].rstrip()}…"
    for marker in ("\\", "|", "[", "]", "*", "_", "`", "<", ">", "~", "#"):
        shortened = shortened.replace(marker, f"\\{marker}")
    shortened = shortened.replace("://", "&#58;//")
    return shortened


def markdown_url(value: str) -> str:
    """Percent-encode characters that could terminate a Markdown link."""
    return quote(value, safe=":/?#@!$&'*+,;=%-._~")


def project_records(payload: Any, platform: str, limit: int) -> list[dict[str, str]]:
    """Project untrusted CLI data into the report evidence contract."""
    projected: list[dict[str, str]] = []
    for record in candidate_records(payload):
        if not isinstance(record, dict):
            continue
        url = public_url(record, platform)
        if not url:
            continue
        item = {"url": url}
        for key in PUBLIC_FIELDS:
            value = scalar(record.get(key))
            if value:
                item[key] = safe_text(value)
        projected.append(item)
        if len(projected) >= limit:
            break
    return projected


def outcome_note(payload: Any) -> str:
    """Preserve a small allowlisted status without exposing raw diagnostics."""
    if not isinstance(payload, dict):
        return ""
    states = [payload]
    for key in ("search_state", "result", "data"):
        if isinstance(payload.get(key), dict):
            states.append(payload[key])
    labels: list[str] = []
    for state in states:
        for key, label in (
            ("login_required", "login required"),
            ("challenge_required", "platform challenge"),
            ("rate_limited", "rate limited"),
        ):
            if state.get(key) is True and label not in labels:
                labels.append(label)
    if labels:
        return ", ".join(labels)
    allowed = {
        "ok",
        "success",
        "complete",
        "completed",
        "partial",
        "empty",
        "no_results",
        "login_required",
        "challenge_required",
        "rate_limited",
        "failed",
        "error",
    }
    for state in states:
        status = state.get("status")
        if isinstance(status, str):
            normalized = status.strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in allowed:
                return normalized.replace("_", " ")
    return ""


def first(item: dict[str, str], keys: tuple[str, ...], default: str = "—") -> str:
    for key in keys:
        if item.get(key):
            return item[key]
    return default


def build_report(
    goal: str,
    decision: dict[str, Any],
    items: list[dict[str, str]],
    jev_ms: int,
    socai_ms: int,
    note: str = "",
) -> str:
    """Render an evidence-only Markdown brief."""
    safe_goal = safe_text(goal, 240)
    lines = [
        "# Jev × socai social research brief",
        "",
        f"**Goal:** {safe_goal}",
        f"**Route:** {decision['platform']} at {decision['confidence'] * 100:.0f}% confidence",
        "",
        "## Evidence snapshot",
        "",
        (
            f"Captured {len(items)} source-linked record{'s' if len(items) != 1 else ''}. "
            "The notes below restate only text and metrics present in the captured evidence."
        ),
        "",
    ]

    if items:
        lines.extend(["## Findings", ""])
        for index, item in enumerate(items, 1):
            author = first(item, ("author", "username", "nickname"))
            evidence = first(item, ("title", "caption", "description", "text"))
            lines.append(
                f"{index}. **{author}:** {evidence} "
                f"([source]({markdown_url(item['url'])}))"
            )
        lines.extend(
            [
                "",
                "## Comparison",
                "",
                "| # | Author | Format | Likes | Comments | Views | Source |",
                "|---:|---|---|---:|---:|---:|---|",
            ]
        )
        for index, item in enumerate(items, 1):
            lines.append(
                "| {index} | {author} | {format} | {likes} | {comments} | {views} | "
                "[Open]({url}) |".format(
                    index=index,
                    author=first(item, ("author", "username", "nickname")),
                    format=first(item, ("media_type", "type")),
                    likes=first(item, ("likes", "like_count")),
                    comments=first(item, ("comments", "comment_count")),
                    views=first(item, ("views", "view_count")),
                    url=markdown_url(item["url"]),
                )
            )
    else:
        lines.extend(
            [
                "No previewable source-linked records were returned. No findings were inferred.",
                "",
            ]
        )

    lines.extend(
        [
            "",
            "## Limits",
            "",
            "- This is a bounded search result, not a representative survey of the platform.",
            "- Missing text or metrics mean unavailable evidence, not a zero value.",
            "- Open each source before using a finding in a consequential decision.",
        ]
    )
    if note:
        lines.append(f"- Run status: {safe_text(note, 100)}.")
    lines.extend(
        [
            "",
            "## Timing",
            "",
            f"Jev {jev_ms / 1000:.2f}s · socai {socai_ms / 1000:.2f}s",
            "",
        ]
    )
    return "\n".join(lines)


def load_fixture(path: Path, requested_platform: str) -> tuple[dict[str, Any], Any]:
    """Load a bounded local fixture for a no-key demo and CI."""
    if not path.is_absolute():
        path = PROJECT_DIR / path
    try:
        if path.stat().st_size > MAX_OUTPUT_BYTES:
            raise JevSocialError("Fixture exceeded the safe size limit.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise JevSocialError(f"Could not read fixture: {path}") from error
    except json.JSONDecodeError as error:
        raise JevSocialError("Fixture contained invalid JSON.") from error
    if not isinstance(payload, dict) or "decision" not in payload or "socai" not in payload:
        raise JevSocialError("Fixture must contain decision and socai objects.")
    return validate_decision(payload["decision"], requested_platform), payload["socai"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an evidence-only social research brief with Jev and socai."
    )
    parser.add_argument("goal", help="Natural-language social research goal")
    parser.add_argument(
        "--platform", choices=("auto", *PLATFORM_HOSTS), default="auto"
    )
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--fixture",
        nargs="?",
        const="fixtures/sample_socai.json",
        help="Run without network or browser; optionally provide a fixture path.",
    )
    parser.add_argument("--output", type=Path, help="Also write the Markdown report here.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    goal = args.goal.strip()
    if not goal:
        print("error: research goal cannot be empty", file=sys.stderr)
        return 2
    try:
        if not 1 <= args.limit <= 20:
            raise JevSocialError("Result limit must be between 1 and 20.")
        if not 1 <= args.timeout <= 600:
            raise JevSocialError("Timeout must be between 1 and 600 seconds.")

        if args.fixture:
            decision, payload = load_fixture(Path(args.fixture), args.platform)
            jev_ms = 0
            socai_ms = 0
        else:
            decision, jev_ms = call_jev(
                goal,
                args.platform,
                os.environ.get("OPENROUTER_API_KEY", "").strip(),
                os.environ.get("OPENROUTER_JEV_MODEL", DEFAULT_MODEL).strip()
                or DEFAULT_MODEL,
            )
            executable = resolve_socai(os.environ.get("SOCAI_BIN", "").strip())
            payload, socai_ms = run_socai(
                executable,
                decision["platform"],
                goal,
                args.limit,
                timeout=args.timeout,
            )

        items = project_records(payload, decision["platform"], args.limit)
        report = build_report(
            goal, decision, items, jev_ms, socai_ms, outcome_note(payload)
        )
        print(report)
        if args.output:
            args.output.expanduser().write_text(report, encoding="utf-8")
        return 0 if items else 3
    except JevSocialError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: could not write report: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
