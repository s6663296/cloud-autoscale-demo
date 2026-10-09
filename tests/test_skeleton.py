import importlib

import pytest


@pytest.mark.parametrize("name", ["shared", "dispatch", "web", "loadtest"])
def test_package_importable(name):
    assert importlib.import_module(name).__name__ == name
