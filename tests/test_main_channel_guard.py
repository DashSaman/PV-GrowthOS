import pytest

from pv_growth.core.errors import ValidationError
from pv_growth.telegram.client import FakeTelegramTransport, TelegramClient


@pytest.mark.parametrize("channel", ["-1003855234264", "@pvnetwork0", "pvnetwork0"])
def test_main_channel_post_is_refused_before_api(settings, channel):
    transport = FakeTelegramTransport()
    client = TelegramClient(transport, settings)
    with pytest.raises(ValidationError):
        client.send_channel_post(channel, "test")
    assert not transport.calls
