"""Layer 1 — Output rendering.

Turns the assembled itinerary (validated bookings + whitelisted links) into the
HTML the traveler receives. Every link here was minted by the LinkBuilder from a
validated ID — none came from an agent.
"""

from __future__ import annotations

import html
from typing import Any


def render_html(itinerary: dict[str, Any]) -> str:
    dest = html.escape(str(itinerary.get("destination", "")))
    nights = itinerary.get("nights", 0)
    travelers = itinerary.get("travelers", 1)
    bookings = itinerary.get("bookings", {})
    cost = itinerary.get("cost_summary", {})
    total = cost.get("estimated_total_usd", 0)
    budget = cost.get("budget_usd", 0)
    status = cost.get("status", "")

    rows = []
    for agent, entry in bookings.items():
        proposal = entry.get("proposal", {})
        if agent in ("experience", "food"):
            links = entry.get("links", [])
            if agent == "experience":
                verified = entry.get("verified", {}).get("pois", [])
                ids = proposal.get("poi_ids", [])
                heading = "Experiences"
            else:
                verified = entry.get("verified", {}).get("restaurants", [])
                ids = proposal.get("restaurant_ids", [])
                heading = "Dining"
            items = "".join(
                f'<li><a href="{html.escape(l)}">{html.escape(v.get("name", p))}</a>'
                f' — {html.escape(str(v.get("cuisine") or v.get("kind") or ""))}</li>'
                for l, p, v in zip(links, ids, verified + [{}] * len(links))
            )
            rows.append(f"<tr><th>{heading}</th><td><ul>{items}</ul></td></tr>")
        else:
            link = html.escape(entry.get("link", ""))
            label = _label(agent, proposal, entry.get("verified", {}))
            rows.append(
                f'<tr><th>{agent.title()}</th><td>{label} — '
                f'<a href="{link}">book</a></td></tr>'
            )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Itinerary — {dest}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:.5rem;border-bottom:1px solid #ddd}}
.total{{font-size:1.2rem;font-weight:700;margin-top:1rem}}</style></head>
<body>
<h1>Trip to {dest}</h1>
<p>{nights} night(s) · {travelers} traveler(s) · all links verified & whitelisted by the harness</p>
<table>{''.join(rows)}</table>
<p class="total">Estimated total: ${total} / budget ${budget} — {html.escape(str(status))}</p>
</body></html>"""


def _label(agent: str, proposal: dict, verified: dict) -> str:
    if agent == "flight":
        return html.escape(f"{proposal.get('carrier')} — ${verified.get('price')}")
    if agent == "hotel":
        return html.escape(
            f"{proposal.get('name')} — ${verified.get('price')}/night, {verified.get('rating')}★"
        )
    if agent == "car":
        return html.escape(f"{proposal.get('vendor')} — ${verified.get('daily_price')}/day")
    return ""
