import logging

import pytest


@pytest.fixture
def root():
    """Restores the root logger the test's `install` replaced, closing what it installed."""
    logger = logging.getLogger()
    saved, level = list(logger.handlers), logger.level
    yield logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in saved:
        logger.addHandler(handler)
    logger.setLevel(level)
