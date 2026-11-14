"""Entry point for the standalone executable."""

from app.cli import serve


if __name__ == "__main__":
    raise SystemExit(serve())
