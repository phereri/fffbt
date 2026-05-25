"""Tests that MobileWorker interface is properly abstract and cannot be instantiated."""

import pytest

from src.worker.session.interface import MobileWorker


def test_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        MobileWorker()
