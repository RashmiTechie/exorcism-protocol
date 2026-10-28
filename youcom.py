"""You.com lookup for containment playbooks, cached per anomaly type.

Endpoint per You.com docs: https://ydc-index.io/v1/search with an X-API-Key header.
Confirm parameters with the You.com team at the event. Without a key, it uses an
offline playbook so the demo never depends on the network.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

YOU_URL = os.getenv("YOU_SEARCH_URL", "https://ydc-index.io/v1/search")

QUERIES = {
    "zombie": "infectious outbreak containment protocol isolation quarantine steps",
    "phantom": "false alarm triage verify sensor reading before evacuation protocol",
    "poltergeist": "earthquake building safety secure loose objects protocol",
    "banshee": "noise hazard hearing protection evacuation protocol",
    "wraith": "power outage emergency lighting building evacuation protocol",
}

OFFLINE = {
    "zombie": ["Seal adjacent doors", "Isolate the room", "Deploy team with barriers"],
    "phantom": ["Verify with a second sensor", "Cut EMF sources", "Deploy team with salt line"],
    "poltergeist": ["Secure loose objects", "Clear the room", "Deploy team with dampers"],
    "banshee": ["Issue ear protection", "Evacuate first", "Deploy team with sound baffles"],
    "wraith": ["Restore lighting", "Guide civilians to exits", "Deploy team with floodlights"],
}

_cache: dict[str, dict] = {}


def lookup(kind: str) -> dict:
    if kind in _cache:
        return {**_cache[kind], "cached": True}
    key = os.getenv("YDC_API_KEY") or os.getenv("YOU_API_KEY")
    result = {"kind": kind, "steps": OFFLINE[kind], "sources": [], "live": False}
    if key:
        try:
            url = YOU_URL + "?" + urllib.parse.urlencode({"query": QUERIES[kind], "count": 3})
            req = urllib.request.Request(url, headers={"X-API-Key": key})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                web = json.load(resp).get("results", {}).get("web", [])
            result["sources"] = [{"title": w.get("title"), "url": w.get("url"),
                                  "snippet": (w.get("snippets") or [w.get("description", "")])[0]}
                                 for w in web[:3]]
            result["live"] = True
        except Exception as exc:  # demo safety: never stall on the network
            result["error"] = str(exc)[:120]
    _cache[kind] = result
    return {**result, "cached": False}
