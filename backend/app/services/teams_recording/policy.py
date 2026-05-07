"""Customer-side compliance recording policy templates.

Once a customer's tenant admin has consented to the bot's app
permissions, they still must run a sequence of PowerShell cmdlets to
register the bot as a *compliance recording application* and bind it
to a *Teams compliance recording policy*. The full Microsoft article
is here:

    https://learn.microsoft.com/microsoftteams/teams-recording-policy

Without this PowerShell, even a fully-deployed media bot will never be
invited into calls — Teams calling policies are what tell the platform
"insert this bot into recorded users' calls". The cmdlets need:

* Our bot's Azure AD app id (``TEAMS_BOT_APP_ID``).
* The customer's Teams admin running them on a workstation with the
  MicrosoftTeams PowerShell module installed.
* A choice of which users / groups the policy applies to.

This module is a single helper that produces a customer-pasteable
PowerShell script. It is *not* run server-side — it's served back to
the customer's admin UI for them to copy. Keeping the template here (vs
inline in a doc) means the bot app id lives in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class CompliancePolicyTemplate:
    """Inputs for the PowerShell script."""

    bot_app_id: str
    """The Azure AD application (client) id of LINDA's compliance bot.
    Same value as the ``TEAMS_BOT_APP_ID`` env var."""

    display_name: str = "LINDA Compliance Recording"
    """Human-readable name shown to the customer's Teams admin in the
    Teams admin centre. Customers may rename this; we just supply a
    sensible default."""

    policy_name: str = "LINDA-CompliancePolicy"
    """The compliance recording policy name. The cmdlet creates this
    policy when first run; subsequent runs just attach the bot."""

    target_user_upns: Optional[List[str]] = None
    """Specific user UPNs to grant the policy. None = the admin will
    decide later via ``Grant-CsTeamsComplianceRecordingPolicy`` per
    user."""


_TEMPLATE_HEADER = """\
# LINDA — Microsoft Teams Compliance Recording bootstrap.
#
# Run this in a PowerShell session on a workstation with the
# MicrosoftTeams module installed (Install-Module MicrosoftTeams).
# Connect-MicrosoftTeams must succeed first, with a Teams admin account.
#
# What this does:
#   1. Registers LINDA's compliance recording app with your tenant.
#   2. Creates (or updates) a compliance recording policy that requires
#      the app to join recorded users' calls.
#   3. Attaches the app to the policy.
#
# After running, grant the policy to specific users:
#   Grant-CsTeamsComplianceRecordingPolicy -Identity user@example.com `
#       -PolicyName {policy_name}
"""


_TEMPLATE_BODY = """\
$BotAppId = "{bot_app_id}"
$DisplayName = "{display_name}"
$PolicyName = "{policy_name}"

# 1. Register the app as a compliance recording application.
New-CsTeamsComplianceRecordingApplication `
    -Identity "Tag:$PolicyName/$BotAppId" `
    -DisplayName $DisplayName `
    -RequiredBeforeMeetingJoin $true `
    -RequiredDuringMeeting $true `
    -ConcurrentInvitationCount 2

# 2. Create or update the policy that uses it.
if (-not (Get-CsTeamsComplianceRecordingPolicy -Identity $PolicyName -ErrorAction SilentlyContinue)) {{
    New-CsTeamsComplianceRecordingPolicy `
        -Identity $PolicyName `
        -Enabled $true `
        -Description "{display_name} policy"
}}

# 3. Attach the app to the policy.
Set-CsTeamsComplianceRecordingPolicy `
    -Identity $PolicyName `
    -ComplianceRecordingApplications @(
        New-CsTeamsComplianceRecordingApplication -Parent $PolicyName -Id $BotAppId
    )
"""

_TEMPLATE_GRANT_BLOCK = """\

# 4. (Optional) Grant the policy to specific users.
{grants}
"""


def render_powershell(template: CompliancePolicyTemplate) -> str:
    """Produce the PowerShell script the customer's Teams admin runs.

    The output is plain text, safe to drop into a docs page or admin
    UI ``<pre>`` block. We deliberately do not run this server-side —
    the cmdlets only work in an authenticated MicrosoftTeams PowerShell
    session, which can only be established by the customer's admin.
    """

    if not template.bot_app_id:
        raise ValueError("bot_app_id is required for the PowerShell template")

    body = _TEMPLATE_HEADER.format(policy_name=template.policy_name)
    body += "\n"
    body += _TEMPLATE_BODY.format(
        bot_app_id=template.bot_app_id,
        display_name=template.display_name,
        policy_name=template.policy_name,
    )
    if template.target_user_upns:
        grants = "\n".join(
            f"Grant-CsTeamsComplianceRecordingPolicy -Identity {upn!r} -PolicyName $PolicyName"
            for upn in template.target_user_upns
        )
        body += _TEMPLATE_GRANT_BLOCK.format(grants=grants)
    return body
