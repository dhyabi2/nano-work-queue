"""The consent page, as HTML.

Three constraints shape every line of this file:

  * **It must render with JavaScript disabled.** Operators review in hardened
    environments and a blank page is a no. There is no script tag here at all,
    so there is nothing to disable: the two submit buttons carry their own
    `decision` value, which is why the form needs no hidden field and no
    client-side code to tell approve from decline.
  * **The order is the specification.** An operator reads the first screen and
    decides, so the demand evidence is above everything else -- above the
    scope, above the form, above the reassurance. A page that opens by
    explaining itself and buries the payer answers a question the operator is
    not asking.
  * **The form has exactly two inputs and two buttons.** Every added field is
    a reason to close the tab, so there is no email, no account, no terms
    checkbox, and no hidden field carrying a token.

`esc` is applied to every value that came from outside. The agent name, the
operator hint and the signer's name are all free text somebody else typed.
"""

from . import consent as consent_mod

CLONE_COMMAND = (
    "git clone https://github.com/dhyabi2/nano-wallet-xno && "
    "python3 nano-wallet-xno/cli.py new"
)

NO_CUSTODY_LINES = (
    "No custody. We never hold, generate, escrow, or see your agent's "
    "private key.",
    "No access to any other account, repo, or credential.",
    "No recurring authority: this expires {expires_at} and cannot be "
    "extended without a new signature.",
    "No automatic renewal. No silent scope change. A changed scope is a new "
    "page and a new signature.",
    "Revocable at any time at {revoke_url}, with no notice and no penalty.",
)


def esc(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def scope_sentence(consent):
    agent = esc(consent.agent)
    base = (
        f"{agent} may generate its own Nano address and receive payments to "
        f"it. It cannot send. No key of {agent}'s is ever shared with us, and "
        "we cannot move its funds."
    )
    if consent.scope == consent_mod.RECEIVE_AND_SEND:
        return (
            base[:-1]
            + f", and may send up to {esc(consent.max_send_xno)} XNO in total "
            "under this consent."
        )
    return base


def demand_block(demand):
    """Figures first, fetched live, every one of them a clickable public URL."""
    lines = [
        "<h2>Work posted and paid so far</h2>",
        "<p class=\"demand\">"
        f"<strong>{demand['settled']} jobs settled, "
        f"{esc(demand['paid_xno_total'])} XNO paid.</strong><br>"
        f"Open right now: {demand['open']} jobs. "
        f"See them: <a href=\"{esc(demand['jobs_url'])}\">"
        f"{esc(demand['jobs_url'])}</a>"
        "</p>",
    ]
    latest = demand.get("latest")
    if latest:
        lines.append(
            "<p class=\"demand\">Most recent payment: "
            f"<a href=\"{esc(latest['receipt_url'])}\">"
            f"{esc(latest['receipt_url'])}</a><br>"
            f"&mdash; {esc(latest['price_xno'])} XNO, block "
            f"{esc(latest['block_hash'])}, confirmed "
            f"{esc(latest['settled_at'])}</p>"
        )
    return "\n".join(lines)


def does_not_grant(consent, revoke_url):
    items = "\n".join(
        f"      <li>{esc(line.format(expires_at=consent.expires_at, revoke_url=revoke_url))}</li>"
        for line in NO_CUSTODY_LINES
    )
    return f"<h2>What this does not grant</h2>\n    <ul>\n{items}\n    </ul>"


def how_to_verify(demand, base_url):
    latest = demand.get("latest")
    receipt = (latest or {}).get("receipt_url") or f"{base_url}/v1/receipts/"
    return "\n".join([
        "<h2>How to check the claims above</h2>",
        "    <ul>",
        "      <li>The address is generated inside your agent's own process. "
        f"Command: <code>{esc(CLONE_COMMAND)}</code> &mdash; the seed it "
        "prints stays on your machine and is never sent anywhere.</li>",
        "      <li>Every payment we make is a public Nano block. Check any of "
        "them against any node.</li>",
        f"      <li>Our published receipts: <a href=\"{esc(receipt)}\">"
        f"{esc(receipt)}</a></li>",
        "    </ul>",
    ])


def form(consent, base_url):
    """One name field, one reason field, one Approve, one Decline. No more."""
    action = f"{base_url}/v1/consent/{esc(consent.id)}/sign"
    return "\n".join([
        f'<form method="post" action="{action}">',
        '      <label for="signed_by">Your name</label>',
        '      <input type="text" id="signed_by" name="signed_by" '
        'maxlength="120" required>',
        '      <button type="submit" name="decision" value="approve">'
        'Approve</button>',
        '      <label for="reason">If you decline, why? '
        '(required to decline)</label>',
        '      <input type="text" id="reason" name="reason" maxlength="2000">',
        '      <button type="submit" name="decision" value="decline">'
        'Decline</button>',
        "    </form>",
    ])


def signed_notice(consent):
    if consent.state == consent_mod.SIGNED:
        return (f"<p class=\"state\">Approved by {esc(consent.signed_by)} on "
                f"{esc(consent.signed_at)}. This consent expires "
                f"{esc(consent.expires_at)}.</p>")
    if consent.state == consent_mod.DECLINED:
        return (f"<p class=\"state\">Declined by {esc(consent.signed_by)} on "
                f"{esc(consent.signed_at)}. Reason on file: "
                f"{esc(consent.decline_reason)}. Nothing was granted, and "
                "nobody will follow up.</p>")
    if consent.state == consent_mod.REVOKED:
        return ("<p class=\"state\">This consent was revoked. It grants "
                "nothing, and no new signature can be taken on this page.</p>")
    if consent.state == consent_mod.EXPIRED:
        return (f"<p class=\"state\">This consent expired on "
                f"{esc(consent.expires_at)} without being signed. It grants "
                "nothing. A new page and a new signature would be needed.</p>")
    return ""


STYLE = """
    body { font: 16px/1.55 system-ui, sans-serif; max-width: 42rem;
           margin: 2rem auto; padding: 0 1rem; color: #111; }
    h1 { font-size: 1.4rem; } h2 { font-size: 1.05rem; margin-top: 1.6rem; }
    .demand { background: #f4f7f4; border-left: 4px solid #2c6e49;
              padding: .7rem .9rem; overflow-wrap: anywhere; }
    .state { background: #eef2f7; padding: .7rem .9rem; }
    code { background: #f2f2f2; padding: .1rem .3rem; overflow-wrap: anywhere; }
    label { display: block; margin-top: .9rem; font-weight: 600; }
    input { width: 100%; padding: .45rem; font: inherit; }
    button { margin-top: .6rem; padding: .5rem 1.1rem; font: inherit; }
    footer { margin-top: 2rem; color: #555; font-size: .9rem; }
"""


def render(consent, demand, base_url):
    revoke_url = f"{base_url}/v1/consent/{consent.id}/revoke"
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Consent for {esc(consent.agent)}</title>",
        f"<style>{STYLE}</style></head><body>",
        # 1. demand, above everything else
        demand_block(demand),
        # 2. what is being agreed
        "<h2>What you would be agreeing to</h2>",
        f"    <p>{scope_sentence(consent)}</p>",
        # 3. what this does not grant
        "    " + does_not_grant(consent, revoke_url),
        # 4. how to check it
        "    " + how_to_verify(demand, base_url),
    ]
    if consent.operator_hint:
        parts.append(f"    <p>Issued for: {esc(consent.operator_hint)}</p>")
    notice = signed_notice(consent)
    if notice:
        parts.append("    " + notice)
    else:
        # 5. the form -- only while this consent is still pending
        parts.append("<h2>Approve or decline</h2>")
        parts.append("    " + form(consent, base_url))
    parts.append(
        "    <footer>Consent "
        f"{esc(consent.id)} for {esc(consent.agent)}. Created "
        f"{esc(consent.created_at)}, expires {esc(consent.expires_at)}. "
        f"Machine-readable: <a href=\"{base_url}/v1/consent/{esc(consent.id)}"
        f".json\">{base_url}/v1/consent/{esc(consent.id)}.json</a>"
        "</footer>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def render_error(exc):
    """An error page with no form on it, whatever the error was."""
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{esc(exc.code)}</title>",
        f"<style>{STYLE}</style></head><body>",
        f"<h1>{esc(exc.code)}</h1>",
        f"<p>{esc(exc.message)}</p>",
        "</body></html>",
    ]) + "\n"
