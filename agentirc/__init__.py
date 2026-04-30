from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("agentirc-cli")
except PackageNotFoundError:
    # Source checkout without an installed dist (e.g. running tests against
    # a non-editable tree). Fall back to a sentinel so `agentirc version`
    # still works.
    __version__ = "0.0.0+local"
