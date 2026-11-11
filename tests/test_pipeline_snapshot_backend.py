from pathlib import Path

from utils.pipeline_db import uses_management_api
from utils.service_endpoints import MONITOR_DEPLOY_DIR


def test_snapshot_backend_uses_management_api_on_monitor_host():
    assert uses_management_api(MONITOR_DEPLOY_DIR)
    assert not uses_management_api(Path('/Users/wyl/BrandRap/costfinder'))
