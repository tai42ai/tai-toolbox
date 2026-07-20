"""The ``generate_uuid`` tool: generate a random UUID.

Registered through the global ``tai_app`` handle. Carries no heavy backing
dependency, so this module imports cleanly in the base install.
"""

from __future__ import annotations

import uuid

from tai_contract.app import tai_app


@tai_app.tools.tool
def generate_uuid() -> str:
    """Generate a random UUID (version 4)."""
    return str(uuid.uuid4())
