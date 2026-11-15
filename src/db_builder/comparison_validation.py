"""P2P comparative research validation."""
from __future__ import annotations

import re


def build_peer_groups(candidates: list[dict]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for c in candidates:
        groups.setdefault(str(c.get("sector") or "Unknown"), []).append(c["ticker"])
    return groups


def validate_comparisons(selected: list[dict], comparisons: list[dict], candidates: list[dict]) -> list[str]:
    flags = []
    peers = build_peer_groups(candidates)
    sector = {c["ticker"]: c.get("sector") for c in candidates}
    by_ticker = {}
    for x in comparisons or []:
        if not isinstance(x, dict):
            flags.append("malformed_comparison"); continue
        t, p = x.get("ticker"), x.get("peer")
        if t not in sector: flags.append(f"unknown_selected:{t}"); continue
        if p == t: flags.append(f"self_comparison:{t}"); continue
        if p not in sector: flags.append(f"unknown_peer:{t}:{p}"); continue
        if sector[t] != sector[p]: flags.append(f"unrelated_peer:{t}:{p}"); continue
        dim = str(x.get("dimension") or "").strip().lower()
        if not dim or dim in {"generic", "other", "n/a", "none"}:
            flags.append(f"generic_dimension:{t}:{p}")
        if not x.get("evidence_ids"):
            flags.append(f"comparison_without_evidence:{t}:{p}")
        txt = str(x.get("thesis") or "")
        if len(txt.strip()) < 80:
            flags.append(f"generic_comparison:{t}:{p}")
        by_ticker.setdefault(t, []).append(p)
    for x in selected:
        t = x.get("ticker")
        if t in sector and peers.get(sector[t], []) and not by_ticker.get(t):
            flags.append(f"missing_comparison:{t}")
    return sorted(set(flags))
