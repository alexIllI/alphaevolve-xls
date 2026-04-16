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
    ):
        self.db = db
        self.migration_interval = migration_interval
        self._rng = random.Random(seed)

        _mutation_types = mutation_types or ["sdc_objective", "delay_constraints"]

        # Create islands, cycling through mutation types
        self.islands: list[Island] = [
            Island(
                id=i,
                mutation_type=_mutation_types[i % len(_mutation_types)],
            )
            for i in range(num_islands)
        ]

    def select_island(self, iteration: int) -> Island:
        """Round-robin island selection."""
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
        instructions = {
            "sdc_objective": [
                # Variation 0: force-directed weighting
                (
                    "Implement a force-directed objective: compute each node's mobility "
                    "(ALAP - ASAP slack using distances_to_node_) and use it as an inverse "
                    "weight on the cycle_var term. Nodes with low mobility (critical) should "
                    "be scheduled ASAP; high-mobility nodes should be pushed toward stages "
                    "that minimize register pressure. Use kObjectiveScaling to prevent "
                    "the tie-breaker from dominating."
                ),
                # Variation 1: balance resource utilization
                (
                    "Implement a resource-balancing objective: iterate over graph_.nodes() "
                    "and, for each non-dead non-untimed node, add a term to the objective "
                    "that weights cycle_var_.at(node) by node->GetType()->GetFlatBitCount(). "
                    "This penalizes scheduling wide (expensive) nodes late, naturally "
                    "balancing resource usage across stages. Use kObjectiveScaling as an "
                    "overall coefficient. Also keep the last_stage minimization term."
                ),
                # Variation 2: minimize register bits weighted by fan-out
                (
                    "Revise the lifetime objective to weight register costs by the fan-out "
                    "of each node. A node with many users kept alive across stage boundaries "
                    "costs more in routing and area, so its lifetime penalty should be higher. "
                    "Use node->users().size() as a multiplier on the lifetime_var coefficient."
                ),
                # Variation 3: hierarchical objective
                (
                    "Implement a hierarchical objective using the existing LP variables: "
                    "(1) Primary: minimize last_stage (the final pipeline stage count), "
                    "weighted by 1e6 * kObjectiveScaling. "
                    "(2) Secondary: minimize total register pressure by summing "
                    "kObjectiveScaling * lifetime_var_.at(node) for all non-untimed nodes. "
                    "(3) Tertiary: add kObjectiveScaling * 1e-6 * cycle_var_.at(node) as "
                    "an ASAP tie-breaker. Combine all three into one LinearExpression and "
                    "call model_.Minimize()."
                ),
            ],
            "delay_constraints": [
                # Variation 0: slack-aware constraints
                (
                    "In ComputeCombinationalDelayConstraints, add a slack-proportional "
                    "relaxation: instead of a strict > clock_period boundary, also generate "
                    "constraints for node pairs whose critical path is within 10% of the "
                    "clock period (near-critical paths). This pre-emptively prevents "
                    "near-timing-violation paths from causing infeasibility."
                ),
                # Variation 1: transitive reduction
                (
                    "Implement a transitive reduction of the delay constraints: after computing "
                    "the minimal constraint set, remove any constraint (a→b) that is already "
                    "implied by a chain of other constraints. This reduces LP problem size "
                    "and can speed up the solver while keeping the solution quality identical."
                ),
            ],
        }

        variants = instructions.get(island.mutation_type, ["Improve this function's algorithm."])
        return variants[iteration % len(variants)]
