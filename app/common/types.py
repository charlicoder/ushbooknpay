"""
app/common/types.py
───────────────────
Shared type aliases used across the codebase.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TypeAlias

# Primary key type — all entities use UUID
EntityID: TypeAlias = uuid.UUID

# Monetary amounts — always Decimal, never float
Money: TypeAlias = Decimal

# JSON-serialisable dict
JsonDict: TypeAlias = dict[str, object]

# Customer identifier extracted from JWT
CustomerID: TypeAlias = uuid.UUID
