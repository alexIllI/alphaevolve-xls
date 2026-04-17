"""
alphaevolve/islands.py
──────────────────────
Island-based population management for AlphaEvolve.

Each island maintains an independent population of candidates.
Periodically, the best candidates migrate between islands to
share discoveries while maintaining diversity.

Islands avoid the "premature convergence" problem of a single global
population by allowing different mutation strategies to evolve independently.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from alphaevolve.database import Candidate, CandidateDB


@dataclass
class Island:
    id: int
    mutation_type: str          # which function this island specializes in
    population: list[Candidate] = field(default_factory=list)

    def best(self) -> Candidate | None:
        successful = [c for c in self.population if c.build_status == "success"]
        return min(successful, key=lambda c: c.ppa_score) if successful else None

    def add(self, candidate: Candidate) -> None:
        self.population.append(candidate)
        # Keep population bounded
        self.population.sort(key=lambda c: c.ppa_score)
        self.population = self.population[:50]


class IslandManager:
    """
    Manages N islands, each with its own population.

    Island assignment:
      - Each island specializes in a mutation type (rotating)
      - After every `migration_interval` iterations, the global best
        migrates to all other islands as a new parent
    """

    def __init__(
        self,
        db: CandidateDB,
        num_islands: int = 4,
        migration_interval: int = 5,
        mutation_types: list[str] | None = None,
        seed: int | None = None,
        pinned_island_id: int | None = None,  # if set, always use this island
    ):
        self.db = db
        self.migration_interval = migration_interval
        self._rng = random.Random(seed)
        self.pinned_island_id = pinned_island_id

        _mutation_types = mutation_types or ["agent_scheduler"]

        # Create islands, cycling through mutation types
        self.islands: list[Island] = [
            Island(
                id=i,
                mutation_type=_mutation_types[i % len(_mutation_types)],
            )
            for i in range(num_islands)
        ]

        if pinned_island_id is not None and pinned_island_id >= len(self.islands):
            raise ValueError(
                f"--island_id {pinned_island_id} out of range "
                f"(only {len(self.islands)} islands, ids 0–{len(self.islands)-1})"
            )

    def select_island(self, iteration: int) -> Island:
        """Return the island for this iteration.

        - pinned_island_id set → always return that island (no rotation)
        - num_islands == 1    → always island 0 (linear evolution)
        - otherwise           → round-robin by iteration
        """
        if self.pinned_island_id is not None:
            return self.islands[self.pinned_island_id]
        return self.islands[iteration % len(self.islands)]

    def select_parent(self, island: Island) -> Candidate | None:
        """
        Tournament selection: pick the better of two random candidates,
        or the global best if the island has no successful candidates.
        """
        successful = [c for c in island.population if c.build_status == "success"]
        if not successful:
            # Fall back to global best
            global_best = self.db.best(n=1)
            return global_best[0] if global_best else None

        if len(successful) == 1:
            return successful[0]

        # Tournament of size 2
        a, b = self._rng.sample(successful, 2)
        return a if a.ppa_score <= b.ppa_score else b

    def record(self, candidate: Candidate, island: Island) -> None:
        """Store candidate in DB and update island population."""
        cid = self.db.insert(candidate)
        candidate.id = cid
        island.add(candidate)

    def maybe_migrate(self, iteration: int) -> None:
        """
        Every migration_interval iterations, copy the global best
        into each island's population to spread good discoveries.
        """
        if iteration == 0 or iteration % self.migration_interval != 0:
            return

        global_bests = self.db.best(n=1)
        if not global_bests:
            return

        best = global_bests[0]
        for island in self.islands:
            if not any(c.id == best.id for c in island.population):
                island.add(best)

    def mutation_instruction_for(self, island: Island, iteration: int) -> str:
        """
        Returns a mutation instruction string for the AI, varying by island
        and iteration to encourage exploration of different algorithmic ideas.
        """
        # Mutation target: AgentGeneratedScheduler() in
        # xls/scheduling/agent_generated_scheduler.cc.
        # Available APIs the variants can lean on:
        #   TopoSort(f), IsUntimed(node)
        #   bounds->lb(node), bounds->ub(node)
        #   bounds->TightenNodeLb/Ub(node, cycle), bounds->PropagateBounds()
        #   delay_estimator.GetOperationDelayInPs(node)
        #   node->operands(), node->users(), node->GetType()->GetFlatBitCount()
        instructions = {
            "agent_scheduler": [
                # Variation 0: register-pressure-aware list scheduler
                (
                    "Implement a register-pressure-aware list scheduler. Visit nodes via "
                    "TopoSort(f), skip IsUntimed(node), and for each node pick a cycle in "
                    "[bounds->lb(node), bounds->ub(node)] that minimises boundary register "
                    "cost — operands that would be registered across stage boundaries "
                    "weighted by node->GetType()->GetFlatBitCount(), plus users that would "
                    "be forced to later stages. After each placement call "
                    "bounds->TightenNodeLb/Ub and bounds->PropagateBounds."
                ),
                # Variation 1: ASAP with delay-model tie-break
                (
                    "Implement an ASAP-first heuristic with delay-model tie-breaking. For "
                    "each node in TopoSort order pick the earliest cycle in "
                    "[bounds->lb(node), bounds->ub(node)]; among equally early cycles, "
                    "prefer the one whose same-stage operands leave the most slack under "
                    "clock_period_ps using delay_estimator.GetOperationDelayInPs(node). "
                    "Always honour IsUntimed(node) and tighten bounds after each choice."
                ),
                # Variation 2: mobility-driven greedy
                (
                    "Implement a mobility-aware greedy scheduler. For each node compute "
                    "mobility = bounds->ub(node) - bounds->lb(node) and handle low-mobility "
                    "(critical) nodes first — schedule them ASAP for timing. For "
                    "high-mobility wide nodes (big GetFlatBitCount, many users), push them "
                    "to cycles that reduce live-out bits. Use bounds->TightenNodeLb/Ub and "
                    "bounds->PropagateBounds() after every assignment."
                ),
                # Variation 3: lookahead over candidate cycles
                (
                    "Implement a deterministic multistage heuristic with lightweight "
                    "lookahead: for each node iterate candidate_cycle in "
                    "[bounds->lb(node), bounds->ub(node)] and score not just this node's "
                    "placement but the pressure it adds for its users (use node->users() "
                    "and their assigned cycles). Pick the lowest-score cycle, record it in "
                    "the ScheduleCycleMap, and propagate bounds."
                ),
            ],
        }

        variants = instructions.get(
            island.mutation_type,
            instructions["agent_scheduler"],
        )
        return variants[iteration % len(variants)]
