"""Brand free config display names without changing transport or panel identity."""

import base64
import json
from urllib.parse import quote

FREE_CONFIG_LABEL = "@pvnetwork0 | جهت خرید به @pvnetwork_bot مراجعه کنید"


def brand_uri(uri: str) -> str:
    scheme, separator, payload = uri.partition("://")
    if not separator:
        raise ValueError("unsupported free config URI")
    if scheme in {"vless", "trojan", "ss"}:
        return uri.split("#", 1)[0] + "#" + quote(FREE_CONFIG_LABEL, safe="")
    if scheme == "vmess":
        try:
            data = json.loads(
                base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
            )
            if not isinstance(data, dict):
                raise ValueError("invalid vmess object")
            data["ps"] = FREE_CONFIG_LABEL
            return "vmess://" + base64.b64encode(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid free vmess config") from exc
    raise ValueError("unsupported free config URI")
