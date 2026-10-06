"""Compatibility entrypoint for the stronger portable source acceptance check.

The fresh venv now installs only the verified extracted package and starts the
real launcher/API/worker from an unrelated working directory.
"""
from check_portable_release import main


if __name__ == '__main__':
    raise SystemExit(main())
