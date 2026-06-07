"""TYO3 subscription bus — post-commit delta fan-out.

The bus turns the system from poll-and-re-derive into react-to-exactly-
what-changed (§12.1).  It lives in Python, fed from the write path
*after* the native commit returns, so it **cannot** block the writer.
"""

from tyo3.bus.interest import Interest
from tyo3.bus.delta import Delta
from tyo3.bus.subscription import Subscription

# Imported lazily by the session; will exist by Step 4.
# from tyo3.bus.bus import Bus

__all__ = ["Interest", "Delta", "Subscription"]
