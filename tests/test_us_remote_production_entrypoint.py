from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from scripts import run_us_remote_production


def test_production_entrypoint_binds_us_remote_acquirer() -> None:
    assert run_us_remote_production.USRemoteAcquirer is USRemoteAcquirer
