"""Optional local web adapter; the research runtime remains shared with the CLI."""

def create_app(*args, **kwargs):
    """Import HTTP dependencies only when the optional web interface is used."""
    from .app import create_app as factory
    return factory(*args, **kwargs)


def serve(*args, **kwargs):
    from .__main__ import serve as run
    return run(*args, **kwargs)
