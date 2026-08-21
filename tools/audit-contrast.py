#!/usr/bin/env python3
"""Measure text contrast on every REACH screen, in a real browser.

``tests/test_a11y.py`` is the CI-runnable proxy for this: it measures each text
colour against the three flat surfaces REACH paints text on. This is the
authority, because it composites the background actually behind every element —
a badge's tinted panel, a translucent row over a card over the page — and reads
the computed style the browser resolved rather than the class that requested it.

    python tools/audit-contrast.py            # seeds its own throwaway database
    python tools/audit-contrast.py path.db    # audit an existing one

Exits non-zero if any text fails WCAG 2.1 AA (4.5:1 normal, 3:1 large).
Needs playwright and a Chromium build; neither is a runtime dependency.
"""

import os
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AA_NORMAL, AA_LARGE = 4.5, 3.0

PROBE = """() => {
  const lum = (c) => { const s = c.map(v => { v /= 255;
      return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); });
    return 0.2126*s[0] + 0.7152*s[1] + 0.0722*s[2]; };
  const parse = (s) => { const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x));
    return { rgb: p.slice(0,3), a: p.length > 3 ? p[3] : 1 }; };
  const over = (fg, bg) => fg.rgb.map((c, i) => c*fg.a + bg[i]*(1-fg.a));
  // Walk up compositing every translucent layer until an opaque one is hit.
  const bgOf = (el) => { let n = el, stack = [];
    while (n && n.nodeType === 1) { const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) { stack.push(c); if (c.a === 1) break; } n = n.parentElement; }
    let base = [10,10,10];
    for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
    return base; };
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    const text = [...el.childNodes].filter(n => n.nodeType === 3)
      .map(n => n.textContent.trim()).join(' ').trim();
    if (!text) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) return;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) return;
    const fg = parse(cs.color); if (!fg) return;
    const bg = bgOf(el);
    const L1 = lum(over(fg, bg)), L2 = lum(bg);
    const ratio = (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const px = parseFloat(cs.fontSize);
    const large = px >= 24 || (parseInt(cs.fontWeight, 10) >= 700 && px >= 18.66);
    const need = large ? 3 : 4.5;
    if (ratio < need) out.push({ text: text.slice(0, 44), ratio: +ratio.toFixed(2),
      need, px: +px.toFixed(1), color: cs.color });
  });
  return out;
}"""


def seed(db_path):
    """A campaign carried through its whole lifecycle, so no screen is empty.

    An audit that only visits empty states measures the chrome and calls the
    product accessible.
    """
    os.environ.update({
        "REACH_DB_PATH": db_path,
        "REACH_ENCRYPTION_KEY": "reach-audit-key-0123456789abcdef",
        "REACH_SENDER_FROM": "reach@outreach.streetbanker.example",
        "REACH_SENDER_NAME": "REACH audit",
        "REACH_SENDER_POSTAL_ADDRESS": "12 Example Street, Berlin, Germany",
        "REACH_SENDER_POSTAL": "12 Example Street, Berlin, Germany",
        "REACH_SENDER_COUNTRY": "DE",
        "REACH_EMAIL_API_KEY": "audit", "REACH_EMAIL_WEBHOOK_SECRET": "audit",
        "REACH_SENDER_DKIM_SELECTOR": "selector1", "REACH_SENDER_DOMAIN_VERIFIED": "1",
    })
    from reach import (analytics, approvals, campaigns, catalog, dns_checks, drafts,
                       fetcher, humanactions, outcomes, pipeline, policy, profile, rbac)
    from reach.providers import email as email_provider

    domain = "outreach.streetbanker.example"
    dns_checks.set_resolver(dns_checks.offline_resolver({
        (domain, "TXT"): ["v=spf1 include:provider.example -all"],
        (f"selector1._domainkey.{domain}", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjAN"],
        (f"_dmarc.{domain}", "TXT"): ["v=DMARC1; p=reject"],
    }))
    fetcher.set_transport(fetcher.FixtureTransport())
    email_provider.set_transport(email_provider.RecordingTransport())

    rbac.ensure_default_tenant()
    policy.seed_policies()
    catalog.ensure_catalog()
    recording = catalog.recording_by_slug("midnight-drive")
    profile_id = profile.get_or_create(recording["id"])
    for field, value in [("primary_genre", "dark electronic"),
                         ("microgenres", ["dark electronic", "industrial"]),
                         ("mood", ["brooding", "nocturnal"]), ("language", "English"),
                         ("comparable_artists", ["HEALTH", "Boy Harsher"])]:
        profile.set_field(profile_id, field, value)
    catalog.attest_rights(recording["id"])
    campaign_id = campaigns.create(recording["id"], mode=campaigns.COPILOT, territories=["DE"])
    pipeline.start(campaign_id)
    pipeline.run_to_completion(campaign_id)
    analytics.capture_baseline(campaign_id)
    humanactions.build_dsp_tasks(campaign_id)
    ready = campaigns.targets(campaign_id, status=campaigns.READY)
    if ready:
        target_id = ready[0]["id"]
        drafts.generate(target_id)
        approvals.approve(target_id)
        approvals.send_approved(target_id)
        outcomes.record_response(target_id, outcomes.ACCEPT)
        outcomes.record_placement(target_id, outcomes.EVIDENCE_URL,
                                  url="https://bassforge.example/promo")
    target = campaigns.targets(campaign_id)[0]
    return campaign_id, target["id"], recording["id"]


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        tempfile.mkdtemp(prefix="reach-audit-"), "audit.db")
    campaign_id, target_id, recording_id = seed(db_path)

    from werkzeug.serving import make_server

    from app import create_app

    server = make_server("127.0.0.1", 5097, create_app())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(1)

    screens = {
        "campaigns": "/reach", "catalog": "/reach/catalog",
        "wizard": f"/reach/new?recording_id={recording_id}",
        "overview": f"/reach/campaigns/{campaign_id}",
        "discover": f"/reach/campaigns/{campaign_id}/discover",
        "opportunities": f"/reach/campaigns/{campaign_id}/opportunities",
        "opportunity": f"/reach/campaigns/{campaign_id}/targets/{target_id}",
        "review": f"/reach/campaigns/{campaign_id}/review",
        "outreach": f"/reach/campaigns/{campaign_id}/outreach",
        "responses": f"/reach/campaigns/{campaign_id}/responses",
        "placements": f"/reach/campaigns/{campaign_id}/placements",
        "needs-you": "/reach/needs-you", "providers": "/reach/providers",
        "relationships": "/reach/relationships", "settings": "/reach/settings",
    }

    from playwright.sync_api import sync_playwright

    chromium = os.environ.get("REACH_CHROMIUM")
    findings, checked = {}, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=chromium or None,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        for name, path in screens.items():
            page.goto(f"http://127.0.0.1:5097{path}", wait_until="networkidle")
            checked += 1
            for item in page.evaluate(PROBE):
                key = (item["color"], item["px"])
                entry = findings.setdefault(key, {"count": 0, "worst": item["ratio"],
                                                  "need": item["need"],
                                                  "sample": item["text"], "screens": set()})
                entry["count"] += 1
                entry["worst"] = min(entry["worst"], item["ratio"])
                entry["screens"].add(name)
        browser.close()
    server.shutdown()

    if not findings:
        print(f"PASS — {checked} screens, no text below WCAG AA.")
        return 0

    total = sum(v["count"] for v in findings.values())
    print(f"FAIL — {total} text elements below WCAG AA across {checked} screens.\n")
    print(f"{'worst':>6} {'need':>5} {'px':>5} {'count':>6}  colour")
    print("-" * 78)
    for (color, px), v in sorted(findings.items(), key=lambda kv: kv[1]["worst"]):
        print(f"{v['worst']:>6} {v['need']:>5} {px:>5} {v['count']:>6}  {color}")
        print(f"{'':>25}{v['sample']!r}")
        print(f"{'':>25}{', '.join(sorted(v['screens']))}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
