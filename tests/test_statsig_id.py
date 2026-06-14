import base64
from unittest.mock import patch

from app.dataplane.proxy.adapters import headers


class _DynamicStatsigConfig:
    def get_bool(self, key: str, default: bool = False) -> bool:
        if key == "features.dynamic_statsig":
            return True
        return default


def test_dynamic_statsig_uses_x1_prefix() -> None:
    with patch.object(headers, "get_config", return_value=_DynamicStatsigConfig()):
        with patch.object(headers.random, "choice", return_value=True):
            value = headers._statsig_id()

    decoded = base64.b64decode(value).decode()
    assert decoded.startswith("x1:TypeError:")
