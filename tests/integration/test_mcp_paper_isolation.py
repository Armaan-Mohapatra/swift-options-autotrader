"""The real MCP adapter cannot be attached to the paper execution engine."""

import pytest

from swift_autotrader.broker.robinhood_mcp import RobinhoodMCPBroker
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight


def test_paper_engine_refuses_mcp_broker_even_if_connected(tmp_path):
    db = tmp_path / "synthetic.sqlite3"
    apply_migrations(db)
    with pytest.raises(TypeError, match="PaperBroker"):
        PaperOrderEngine(db, RobinhoodMCPBroker(), preflight=PaperPreflight())
