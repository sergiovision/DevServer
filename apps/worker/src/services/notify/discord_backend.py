"""Discord NotifyBackend — posts to a channel webhook URL.

Uses Discord's embed objects for color-coded task cards. One webhook =
one channel. Set up at Server settings → Integrations → Webhooks.

https://discord.com/developers/docs/resources/webhook
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from config import settings

from .base import NotifyBackend

logger = logging.getLogger(__name__)

# Discord colors are 0xRRGGBB integers.
_COLOR_OK = 0x36A64F
_COLOR_WARN = 0xF2C744
_COLOR_FAIL = 0xD32F2F
_COLOR_INFO = 0x1F77B4


def _field(name: str, value: str, inline: bool = True) -> dict[str, Any]:
    return {"name": name, "value": value, "inline": inline}


class DiscordBackend(NotifyBackend):
    channel = "discord"

    def is_configured(self) -> bool:
        return bool(settings.discord_webhook_url)

    async def _post(self, payload: dict[str, Any]) -> bool:
        if not self.is_configured():
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(settings.discord_webhook_url, json=payload)
            if resp.status_code >= 300:
                logger.warning("Discord webhook %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception:
            logger.exception("Discord webhook post failed")
            return False

    async def send_text(self, message: str) -> bool:
        # Discord truncates at 2000 chars for the content field.
        return await self._post({"content": message[:1990]})

    def _embed(self, color: int, title: str, fields: list[dict],
               description: str | None = None, footer: str | None = None) -> dict:
        embed: dict[str, Any] = {
            "title": title,
            "color": color,
            "fields": fields,
        }
        if description:
            embed["description"] = description[:4096]
        if footer:
            embed["footer"] = {"text": footer[:2048]}
        return embed

    async def send_task_start(self, **kwargs) -> bool:
        return await self._post({
            "embeds": [self._embed(
                _COLOR_INFO,
                f"🔧 Starting {kwargs['task_key']}",
                [
                    _field("Repo", f"`{kwargs['repo_name']}`"),
                    _field("Agent", f"{kwargs['vendor']}/{kwargs['model']}"),
                    _field("Mode", kwargs["mode"]),
                ],
                description=kwargs["title"],
            )],
        })

    async def send_task_success(self, **kwargs) -> bool:
        duration_min = (kwargs["duration_ms"] or 0) / 1000 / 60
        description = {
            "branch": f"🔗 [Open PR]({kwargs['pr_url']})" if kwargs.get("pr_url") else "Push failed",
            "commit": "💾 Committed directly",
            "patch": "📦 Patch generated",
        }.get(kwargs["git_flow"], "")
        return await self._post({
            "embeds": [self._embed(
                _COLOR_OK,
                f"✅ {kwargs['task_key']} completed",
                [
                    _field("Repo", f"`{kwargs['repo_name']}`"),
                    _field("Duration", f"{duration_min:.1f}m"),
                    _field("Attempts", str(kwargs["attempts"])),
                    _field("Turns", str(kwargs["turns"])),
                    _field("Cost", f"${kwargs['cost']:.4f}"),
                ],
                description=description or None,
            )],
        })

    async def send_task_failed(self, **kwargs) -> bool:
        excerpt = (kwargs["error_context"] or "").strip()[:1500]
        return await self._post({
            "embeds": [self._embed(
                _COLOR_FAIL,
                f"❌ {kwargs['task_key']} FAILED",
                [
                    _field("Repo", f"`{kwargs['repo_name']}`"),
                    _field("Attempts", str(kwargs["attempts"])),
                    _field("Cost", f"${kwargs['cost']:.4f}"),
                ],
                description=f"```\n{excerpt}\n```" if excerpt else None,
            )],
        })

    async def send_vendor_failover(self, **kwargs) -> bool:
        return await self._post({
            "embeds": [self._embed(
                _COLOR_WARN,
                f"🔄 Vendor failover — {kwargs['task_key']}",
                [
                    _field("Repo", f"`{kwargs['repo_name']}`"),
                    _field("From", f"{kwargs['from_vendor']}/{kwargs['from_model']}"),
                    _field("To", f"{kwargs['to_vendor']}/{kwargs['to_model']}"),
                ],
                footer="Primary vendor exhausted — switching to backup.",
            )],
        })

    async def send_budget_warning(self, **kwargs) -> bool:
        wall_s = (kwargs["cum_wall_ms"] or 0) / 1000
        return await self._post({
            "embeds": [self._embed(
                _COLOR_WARN,
                f"⚠️ Budget warning — {kwargs['task_key']}",
                [
                    _field("Repo", f"`{kwargs['repo_name']}`"),
                    _field("Cost", f"${kwargs['cum_cost']:.4f}"),
                    _field("Wall", f"{wall_s:.0f}s"),
                ],
                footer=kwargs["reason"],
            )],
        })

    async def send_budget_exceeded(self, **kwargs) -> bool:
        wall_s = (kwargs["cum_wall_ms"] or 0) / 1000
        return await self._post({
            "embeds": [self._embed(
                _COLOR_FAIL,
                f"🛑 Budget exceeded — {kwargs['task_key']} blocked",
                [
                    _field("Repo", f"`{kwargs['repo_name']}`"),
                    _field("Cost", f"${kwargs['cum_cost']:.4f}"),
                    _field("Wall", f"{wall_s:.0f}s"),
                ],
                footer=kwargs["reason"],
            )],
        })

    async def send_preflight_blocked(self, **kwargs) -> bool:
        violations = kwargs["violations"]
        lines = []
        for v in violations[:5]:
            lines.append(
                f"🔴 **{v.get('kind', 'unknown')}**: "
                f"`{(v.get('detail') or '')[:150]}`"
            )
        if len(violations) > 5:
            lines.append(f"_…and {len(violations) - 5} more violations_")
        return await self._post({
            "embeds": [self._embed(
                _COLOR_FAIL,
                f"🛑 Preflight blocked — {kwargs['task_key']}",
                [],
                description="\n".join(lines),
            )],
        })
