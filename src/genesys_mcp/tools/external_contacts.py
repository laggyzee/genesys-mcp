"""External contacts (CRM) tools — phone/email → customer profile lookup.

Requires the OAuth client to have ``external-contacts:readonly``.
"""

from __future__ import annotations

import logging

import PureCloudPlatformClientV2 as gc
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from genesys_mcp.client import get_api, to_dict, with_retry

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def lookup_external_contact(
        value: str = Field(
            description="The identifier value to look up (e.g. '+61412345678' or 'jane@example.com').",
        ),
        identifier_type: str = Field(
            default="Phone",
            description="Identifier kind: 'Phone' (default), 'Email', 'Twitter', 'Facebook', 'Line', 'WhatsApp', 'Sms'.",
        ),
    ) -> dict:
        """Look up a customer in the external CRM by phone, email, or social handle.

        Returns the matching ExternalContact with all custom fields. CustomFields
        on the contact record typically surface useful retention/triage signals
        (plan, status, last order, account tier, etc. — depending on what your
        tenant has configured). Pair with repeat_caller_report ANIs to instantly
        understand the customer behind a phone number.

        Returns ``{"status": 404, ...}`` if no match (not an error — common for
        new prospects or numbers not in the CRM yet).
        """
        api = gc.ExternalContactsApi(get_api())
        body = {"value": value, "type": identifier_type}
        try:
            resp = with_retry(api.post_externalcontacts_identifierlookup_contacts)(identifier=body)
            return to_dict(resp)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                return {"status": 404, "value": value, "type": identifier_type, "match": None}
            raise
