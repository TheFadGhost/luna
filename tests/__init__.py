"""Test suite for Luna.

Logging is silenced here: the daemon logs warnings on the error paths these
tests deliberately exercise, and that noise makes a green run look broken.
"""

import logging

logging.disable(logging.CRITICAL)
