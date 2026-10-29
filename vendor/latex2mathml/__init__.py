try:
    from importlib import metadata

    __version__ = metadata.version("latex2mathml")
except Exception:  # vendored copy without dist-info
    __version__ = "3.77.0"
