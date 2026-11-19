"""Shared by the render tests under deploy/, which run `tilt alpha tiltfile-result`."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def private_tilt_runtime_dir(tmp_path_factory):
    # Every Tilt on the default port writes the kubeconfig it hands to local commands to one file
    # under XDG_RUNTIME_DIR. The fixture kubeconfig of a render test written there would replace
    # the one a running `tilt up` or `tilt ci` gave its local resources, so the tests' Tilt keeps
    # a runtime directory of its own.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("xdg-runtime")))
        yield
