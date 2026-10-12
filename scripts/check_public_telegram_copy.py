"""Check actual public post copy without exporting HTML, credentials or config URIs.

Pass current public message IDs obtained from read-only PublishedPost queries.
A post absent from Telegram's bounded preview is unobserved, not proof of failure.
"""

import argparse
import html
import json
import re
from datetime import UTC, datetime

import httpx


def message_ids(value):
    result = [int(x) for x in value.split(",") if x]
    if not result or len(result) > 3 or any(not 1 <= x < 2**31 for x in result):
        raise argparse.ArgumentTypeError("one to three positive public message IDs required")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owned-messages", type=message_ids, default=[])
    parser.add_argument("--community-messages", type=message_ids, default=[])
    parser.add_argument("--community-direct", action="store_true")
    args = parser.parse_args()
    requested = set(args.owned_messages + args.community_messages)
    if not requested or len(requested) > 5 or set(args.owned_messages) & set(args.community_messages):
        parser.error("provide up to five distinct current public post IDs")
    with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
        with client.stream("GET", "https://t.me/s/pvnetwork_freeconfig") as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > 1024**2:
                    raise ValueError("public preview exceeds limit")
                content.extend(chunk)
    page = content.decode("utf-8", errors="replace")
    items = list(re.finditer(r'data-post="pvnetwork_freeconfig/(\d+)"', page))
    results = []
    for i, match in enumerate(items):
        mid = int(match.group(1))
        if mid not in requested:
            continue
        segment = page[match.end() : items[i + 1].start() if i + 1 < len(items) else len(page)]
        body = html.unescape(re.sub(r"<[^>]+>", " ", segment))
        owned = mid in args.owned_messages
        direct = not owned and args.community_direct
        raw_public = bool(re.search(r"(?:vless|vmess|trojan|ss)://", body))
        checks = {
            "real_https_test_mentioned": "HTTPS" in body,
            "purchase_bot_link": "https://t.me/pvnetwork_bot" in segment,
            "public_raw_config_policy_correct": raw_public if direct else not raw_public,
        }
        if direct:
            checks["copy_instruction_clear"] = "کپی" in body
            checks["no_membership_purchase_gate_claim"] = "۴۵" not in body and "عضویت" not in body
        else:
            checks["45_day_rule_mentioned"] = "۴۵" in body
            checks["membership_mentioned"] = "عضویت" in body
            checks["delivery_bot_link"] = (
                "https://t.me/pvgrowthos_bot?start=freecfg_shared_"
                if owned
                else "https://t.me/pvgrowthos_bot?start=freecfg_community_"
            ) in segment
        if owned:
            checks["shared_quota_clear"] = "۱ گیگ مشترک" in body
            checks["validity_clear"] = "۲۴ ساعت" in body
        else:
            checks["external_source_clear"] = "متعلق به PV Network نیست" in body
        results.append({"public_message": mid, "owned": owned, "checks": checks})
    missing = sorted(requested - {r["public_message"] for r in results})
    failed = [r["public_message"] for r in results if not all(r["checks"].values())]
    print(
        json.dumps(
            {
                "observed_at_utc": datetime.now(UTC).isoformat(),
                "public_preview_only": True,
                "posts": results,
                "unobserved_messages": missing,
                "failed_copy_messages": failed,
                "raw_html_or_config_uris_saved": False,
            },
            ensure_ascii=False,
        )
    )
    return 1 if failed else 2 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
