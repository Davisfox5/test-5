"""Guard: every Alembic revision id must fit alembic_version.version_num.

Alembic creates ``alembic_version.version_num`` as VARCHAR(32). A longer
revision id passes every test and applies its DDL, then blows up on the
final ``UPDATE alembic_version`` — aborting the deploy AFTER the
migration ops ran (this bit us on 2026-07-17 with a 39-char id; the
transaction rolled back, but only because that migration had no
autocommit blocks). Catch it at test time instead.
"""

import os
import re
from glob import glob

ALEMBIC_VERSION_NUM_MAX = 32

_VERSIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend",
    "alembic",
    "versions",
)

_REVISION_RE = re.compile(
    r"^revision(?::[^=]*)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)


def test_every_revision_id_fits_version_num_column():
    offenders = []
    files = glob(os.path.join(_VERSIONS_DIR, "*.py"))
    assert files, "no migration files found — wrong path?"
    for path in files:
        with open(path) as fh:
            match = _REVISION_RE.search(fh.read())
        if match is None:
            continue
        rev = match.group(1)
        if len(rev) > ALEMBIC_VERSION_NUM_MAX:
            offenders.append(
                "{0} ({1} chars): {2}".format(rev, len(rev), os.path.basename(path))
            )
    assert not offenders, (
        "revision ids longer than alembic_version.version_num VARCHAR(32) — "
        "the deploy release command will fail at the version stamp: "
        + "; ".join(offenders)
    )
