# Marker file so pytest can import ``tests.db_fixtures`` via the
# ``pytest_plugins`` entry in tests/conftest.py. Implicit namespace
# packages worked here for a while but newer pytest plugin loaders
# require the parent dir to be a real package — the bare directory
# resolves to a namespace package without callable plugin hooks.
