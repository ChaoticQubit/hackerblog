import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run every anyio-marked test on asyncio only."""
    return "asyncio"
