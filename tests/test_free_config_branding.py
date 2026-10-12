import base64
import json
from urllib.parse import unquote, urlsplit

import pytest

from pv_growth.free_config.branding import FREE_CONFIG_LABEL, brand_uri


@pytest.mark.parametrize(
    "uri",
    [
        "vless://test-id@8.8.8.8:443?type=ws&path=%2Ffoo%3Fx%3D1&security=tls#old",
        "trojan://test-password@8.8.8.8:443?security=tls&sni=example.com#old",
        "ss://YWVzLTEyOC1nY206dGVzdA@8.8.8.8:443/?plugin=v2ray-plugin%3Btls#old",
    ],
)
def test_only_display_fragment_changes(uri):
    result = brand_uri(uri)
    assert result.split("#", 1)[0] == uri.split("#", 1)[0]
    assert unquote(urlsplit(result).fragment) == FREE_CONFIG_LABEL
    assert brand_uri(result) == result


def test_vmess_preserves_every_field_except_display_name():
    original = {
        "v": "2",
        "ps": "old",
        "add": "8.8.8.8",
        "port": "443",
        "id": "test",
        "net": "ws",
        "path": "/test",
        "tls": "tls",
    }
    uri = "vmess://" + base64.b64encode(json.dumps(original).encode()).decode()
    result = brand_uri(uri)
    decoded = json.loads(base64.b64decode(result[8:]))
    assert decoded.pop("ps") == FREE_CONFIG_LABEL
    original.pop("ps")
    assert decoded == original
    assert brand_uri(result) == result


def test_unrecognized_or_malformed_uri_fails_closed():
    for uri in ("https://example.com/sub", "vmess://broken", "vmess://W10="):
        with pytest.raises(ValueError):
            brand_uri(uri)
