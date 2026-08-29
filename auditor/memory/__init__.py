"""Learned memory: the ledger, retrieval, calibration and learned rules.

One principle governs every module here:

    Memory adjusts priors. The gate still decides.

Nothing in this package can drop a claim. It changes what the agent is shown
before it looks, and where a finding ranks after the gate has ruled. If the
model still makes the claim and the test still fails, the drift is real, and
yesterday's statistics do not get a veto over today's evidence.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ledger  # noqa: E402,F401
import recall  # noqa: E402,F401
import rules  # noqa: E402,F401

Memory = recall.Memory
EPSILON = recall.EPSILON
