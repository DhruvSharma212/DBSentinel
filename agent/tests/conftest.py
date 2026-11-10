"""Agent test fixtures. Database tests need PostgreSQL: set TEST_DATABASE_URL (CREATE ROLE/DATABASE rights)
or install the optional `pixeltable-pgserver`; otherwise they are skipped."""
import os
import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory):
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pixeltable_pgserver as pgserver
    except ImportError:
        pytest.skip("set TEST_DATABASE_URL or install pixeltable-pgserver to run the PostgreSQL tests")
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()
