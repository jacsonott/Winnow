"""The one place Winnow's version number lives.

Deliberately a module with nothing in it but the string: `update.py` has to
read this out of a *release archive* it has downloaded but not installed,
which means importing it can't drag in FastAPI or anything else that may
not be present in the archive's environment.

Bump this in the commit you tag. `updater` compares it against the latest
GitHub release, and it's what the UI shows in Settings → Updates — an
analyst who has to state their tooling in a report shouldn't have to go
digging in git for it.
"""

VERSION = "0.1.0"
