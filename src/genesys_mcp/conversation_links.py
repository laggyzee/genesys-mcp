"""Deep-link helpers for embedding clickable Genesys-Cloud links in reports.

Skills that render HTML reports (cc-monthly-report, cc-coaching-prep,
cc-daily-brief) used to show conversation ids as non-clickable truncated
strings. v0.8 turns each conversation id into a clickable link to the
Genesys Cloud conversation detail view, so supervisors reading the brief
can click straight through to the call.

The base URL differs per region. We use either:

1. ``cfg.tenant.genesys_app_base_url`` — explicit override in tenant.yaml
   (highest priority; useful for tenants with custom domains)
2. The ``GENESYS_REGION`` env var, mapped via :data:`REGION_TO_APP_HOST`

If neither resolves, the helper returns ``None`` and callers should fall
back to the v0.7 non-clickable ``<code>`` rendering.
"""
from __future__ import annotations

import os

# Mapping is hardcoded — the Genesys regional app-host URLs change roughly
# never, and an extra API call to discover them per-report run isn't
# justified. Add new regions here as they come up.
REGION_TO_APP_HOST: dict[str, str] = {
    "ap-southeast-2": "apps.mypurecloud.com.au",
    "us-east-1": "apps.mypurecloud.com",
    "us-east-2": "apps.use2.us-gov-pure.cloud",
    "us-west-2": "apps.usw2.pure.cloud",
    "eu-west-1": "apps.mypurecloud.ie",
    "eu-west-2": "apps.euw2.pure.cloud",
    "eu-central-1": "apps.mypurecloud.de",
    "eu-central-2": "apps.euc2.pure.cloud",
    "ap-northeast-1": "apps.mypurecloud.jp",
    "ap-northeast-2": "apps.apne2.pure.cloud",
    "ap-northeast-3": "apps.apne3.pure.cloud",
    "ap-south-1": "apps.aps1.pure.cloud",
    "ap-southeast-1": "apps.apse1.pure.cloud",
    "ca-central-1": "apps.cac1.pure.cloud",
    "sa-east-1": "apps.sae1.pure.cloud",
    "me-central-1": "apps.mec1.pure.cloud",
}


def resolve_app_base_url(
    tenant_base_url: str | None = None,
    region: str | None = None,
) -> str | None:
    """Resolve the Genesys Cloud app base URL (e.g. ``https://apps.mypurecloud.com.au``).

    Priority:
    1. Explicit ``tenant_base_url`` argument (from ``cfg.tenant.genesys_app_base_url``)
    2. ``region`` argument mapped via :data:`REGION_TO_APP_HOST`
    3. ``GENESYS_REGION`` env var, same mapping
    4. ``None`` (caller falls back to non-clickable rendering)

    Returns the base URL with scheme; never a trailing slash.
    """
    if tenant_base_url:
        return tenant_base_url.rstrip("/")
    if region is None:
        region = os.environ.get("GENESYS_REGION")
    if not region:
        return None
    host = REGION_TO_APP_HOST.get(region)
    if not host:
        return None
    return f"https://{host}"


def conversation_url(
    conversation_id: str,
    *,
    tenant_base_url: str | None = None,
    region: str | None = None,
) -> str | None:
    """Resolve a deep link to the Genesys Cloud conversation detail view.

    The Genesys Cloud UI route for a single conversation is::

        {app_base}/directory/#/analytics/interactions/{conversation_id}/admin/details

    Returns ``None`` when no base URL is resolvable — callers should fall
    back to a non-clickable rendering.
    """
    base = resolve_app_base_url(tenant_base_url=tenant_base_url, region=region)
    if not base or not conversation_id:
        return None
    return f"{base}/directory/#/analytics/interactions/{conversation_id}/admin/details"


def render_conversation_cell(
    conversation_id: str | None,
    *,
    tenant_base_url: str | None = None,
    region: str | None = None,
    truncate: int = 8,
) -> str:
    """Render a conversation-id table cell as a clickable link when possible.

    Falls back to the pre-v0.8 non-clickable ``<code>`` form when no base
    URL is resolvable — preserves backwards compatibility for tenant
    configs that omit the new ``genesys_app_base_url`` field.

    Args:
        conversation_id: the conversation id (or None for an empty cell)
        tenant_base_url: explicit override from tenant config
        region: Genesys region (defaults to GENESYS_REGION env var)
        truncate: how many characters of the id to show in the label
    """
    if not conversation_id:
        return ""
    short = conversation_id[:truncate] + "…" if len(conversation_id) > truncate else conversation_id
    url = conversation_url(
        conversation_id, tenant_base_url=tenant_base_url, region=region,
    )
    if url:
        return (
            f'<a href="{url}" target="_blank" rel="noopener" '
            f'style="font-family:monospace;font-size:11px;color:var(--accent);">'
            f'{short}</a>'
        )
    return f'<code style="font-size:11px;color:var(--muted);">{short}</code>'
