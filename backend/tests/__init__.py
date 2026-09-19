"""Test config: force safe, dependency-free settings for every test."""

from __future__ import annotations

import os

os.environ.setdefault("POLYCOPY_ENVIRONMENT", "test")
os.environ.setdefault("POLYCOPY_DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost:1/unused")
os.environ.setdefault("POLYCOPY_REDIS_URL", "redis://localhost:1/0")
