import fcntl

import pytest

from utils.crawl_schedule_state import schedule_lock


def test_schedule_lock_excludes_other_writers(tmp_path):
    with schedule_lock(tmp_path):
        with (tmp_path / ".lock").open("a+") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with (tmp_path / ".lock").open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
