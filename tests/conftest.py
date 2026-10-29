"""Shared pytest fixtures for the drift_manager test suite.

Exposes the :class:`FakeClock` and :class:`FakeCfnClient` test doubles (see
``tests/fakes.py``) as fixtures so component tests can drive the orchestration
logic deterministically without AWS. Added by task 1.4.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeCfnClient, FakeClock


@pytest.fixture
def clock() -> FakeClock:
    """A fresh, deterministic clock starting at t=0."""
    return FakeClock()


@pytest.fixture
def cfn(clock: FakeClock) -> FakeCfnClient:
    """A fake CloudFormation client backed by the shared ``clock`` fixture."""
    return FakeCfnClient(clock=clock)
