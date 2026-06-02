"""OEM EPC credential loader (v2.0 scope).

Per locked decision (kickoff §A-step 4): credentials stored as environment variables
on the Spinny VM, NOT in git. Convention: <BRAND>_USER, <BRAND>_PASS.

Set in /etc/environment on the VM, OR loaded from a local .env file (gitignored).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Credentials:
    user: str
    password: str
    brand: str

    @classmethod
    def load(cls, brand_key: str) -> "Credentials | None":
        env_brand = brand_key.upper()
        user = os.environ.get(f"{env_brand}_USER")
        password = os.environ.get(f"{env_brand}_PASS")
        if not user or not password:
            return None
        return cls(user=user, password=password, brand=brand_key)
