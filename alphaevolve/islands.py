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

    # ── Bootstrap instructions (iterations 0, 1, 2) ───────────────────────────
    # These three are intentionally maximally diverse so that the first few
    # design points on a PPA graph cover very different regions of the
    # area/delay/stages trade-off space before evolution starts mixing them.
    # Each targets a completely different algorithmic family.
    # Runtime-complexity warning prepended to every bootstrap instruction.
    # sha256 produces ~800 IR nodes after opt_main; larger designs can have 1000+.
    # benchmark_main has a configurable timeout (default 30 min); a timed-out run
    # records runtime_s=3600 as a score *penalty*, so slow algorithms are penalised
    # even if they would eventually produce valid PPA.
    _RUNTIME_WARNING = (
        "\n\nCOMPLEXITY CONSTRAINT — READ BEFORE WRITING CODE:\n"
        "sha256 has ~5000 IR nodes (4919 measured). Larger benchmarks have even more. "
        "Your scheduler runs inside benchmark_main with a configurable timeout (default 30 min). "
        "A timeout records runtime_s=3600 as a score penalty — slow algorithms lose even if "
        "they would produce good PPA.\n\n"
        "CRITICAL — bounds->PropagateBounds() IS O(n) PER CALL:\n"
        "PropagateBounds() walks the entire constraint graph to re-tighten lb/ub for all "
        "unscheduled nodes. Calling it after every node assignment = O(n) × O(n) = O(n²). "
        "On 5000 nodes that is ~25 million propagation steps — this is why agent schedulers "
        "run 15× slower than SDC even on identical schedules.\n\n"
        "CORRECT PATTERN — call PropagateBounds() ONCE, then track lb manually:\n"
        "  XLS_RETURN_IF_ERROR(bounds->PropagateBounds());  // ONCE before the main loop\n"
        "  for (Node* node : topo_nodes) {\n"
        "    if (IsUntimed(node)) continue;\n"
        "    int64_t lb = bounds->lb(node);  // set by initial propagation\n"
        "    // Tighten lb using predecessor assignments — O(degree), not O(n)\n"
        "    for (Node* pred : node->operands()) {\n"
        "      if (!IsUntimed(pred) && assigned_cycles.count(pred))\n"
        "        lb = std::max(lb, assigned_cycles.at(pred));\n"
        "    }\n"
        "    int64_t ub = bounds->ub(node);  // from initial propagation — valid upper bound\n"
        "    lb = std::min(lb, ub);\n"
        "    // ... pick best cycle in [lb, ub] ...\n"
        "    assigned_cycles[node] = best_cycle;\n"
        "    cycle_map[node] = best_cycle;\n"
        "    XLS_RETURN_IF_ERROR(bounds->TightenNodeLb(node, best_cycle));  // O(1)\n"
        "    XLS_RETURN_IF_ERROR(bounds->TightenNodeUb(node, best_cycle));  // O(1)\n"
        "    // DO NOT call bounds->PropagateBounds() here — it is O(n) per call\n"
        "  }\n\n"
        "You MUST implement a single-pass O(n × W) algorithm where n = timed nodes and "
        "W = candidate-cycle window per node (W = ub - lb + 1, typically small). "
        "No unbounded iteration, no repeated full-graph passes, no exponential search."
    )

    _BOOTSTRAP_INSTRUCTIONS: dict[str, list[str]] = {
        "agent_scheduler": [
            # Bootstrap 0: DP-on-DAG scheduler.
            # The scheduling problem on a DAG has optimal substructure: the best
            # cycle for node v depends only on which cycles its operands occupy
            # (already committed by the time v is processed in TopoSort order).
            # This bootstrap asks the AI to exploit that structure by building a
            # memoised cost table over the DAG and making each node's decision
            # in a single forward pass — a true DP, not just a greedy heuristic.
            (
                "Implement a DAG-DP pipeline scheduler.\n\n"
                "The key insight: processed in topological order, each node v's "
                "placement decision is a self-contained subproblem whose optimal "
                "solution depends only on the already-committed placements of v's "
                "operands. This gives the scheduling problem optimal substructure "
                "suitable for dynamic programming.\n\n"
                "Step 1 — Build a memoised cost table.\n"
                "Allocate absl::flat_hash_map<Node*, int64_t> best_cost, initialised "
                "to 0 for all nodes. Process every timed node v in TopoSort(f) order "
                "(skip IsUntimed). For each candidate cycle c in "
                "[bounds->lb(v), bounds->ub(v)] compute:\n"
                "  cost(v, c) = sum over each operand u of v:\n"
                "                 max(0, c - assigned_cycle[u]) "
                "                 * u->GetType()->GetFlatBitCount()\n"
                "This counts the total register-bit-stages consumed if v is placed "
                "at c: each operand u whose value must be pipelined across "
                "(c - assigned_cycle[u]) stage boundaries contributes its bit-width "
                "per crossing. Select c* = argmin cost(v, c) over the legal range "
                "(tie-break: prefer the smallest c to minimise downstream pressure). "
                "Record best_cost[v] = cost(v, c*) and assigned_cycle[v] = c*, "
                "then call bounds->TightenNodeLb(v, c*) and bounds->TightenNodeUb(v, c*) "
                "(both O(1) — do NOT call bounds->PropagateBounds() here; that is O(n) "
                "per call and would make the whole scheduler O(n²) on 5000-node designs). "
                "Track lb for the next node by reading assigned_cycle of its predecessors "
                "directly — O(degree), not O(n).\n\n"
                "Step 2 — Handle fan-out pressure.\n"
                "After assigning c* to v, scan v->users(). For any user w that is "
                "already in the TopoSort prefix (already assigned), check whether "
                "best_cost[w] increases under the new placement of v; if so, update "
                "best_cost[w] in the table (the assignment itself is final — this is "
                "a bookkeeping update only, not a reassignment).\n\n"
                "Step 3 — Return the schedule.\n"
                "Build and return the ScheduleCycleMap from assigned_cycle. "
                "The result minimises the total pipeline register pressure across "
                "all data edges in a single O(nodes × candidate_cycles) forward pass."
            ),
            # Bootstrap 1: Stage-load balancing — equalize node count per stage.
            # Neither ASAP nor ALAP: instead track how many nodes have been placed
            # in each stage and steer each new node toward the most under-loaded
            # valid stage. This produces a flat histogram across stages, minimising
            # peak combinational complexity and giving a distinct area profile.
            (
                "Implement a stage-load-balancing scheduler. "
                "Maintain a per-stage node count array of size pipeline_stages. "
                "Call bounds->PropagateBounds() ONCE before the loop. "
                "For each timed node in TopoSort(f) order, compute lb = "
                "max(bounds->lb(node), max assigned_cycle of timed operands) and "
                "ub = bounds->ub(node). Examine every candidate cycle c in [lb, ub] "
                "and choose the c with the smallest current node count "
                "(tie-break: prefer earlier cycle). Increment the count for "
                "the chosen stage, record the assignment, call "
                "bounds->TightenNodeLb/Ub(node, c) (O(1) each). "
                "DO NOT call bounds->PropagateBounds() inside the loop — "
                "it is O(n) per call, making the scheduler O(n²) on 5000 nodes. "
                "The objective is a flat, balanced stage histogram — minimising "
                "the max-stage node count — which yields a distinct "
                "combinational-depth profile compared to ASAP or ALAP."
            ),
            # Bootstrap 2: Critical-path ALAP then ASAP fill.
            # Two-pass approach: identify nodes on the critical path (zero
            # mobility: ub==lb) and schedule them ASAP for timing closure.
            # All other nodes are pushed ALAP to reduce unnecessary register
            # pressure. This produces a distinct 'tight-critical-path' profile.
            (
                "Implement a two-pass critical-path-first scheduler. "
                "Call bounds->PropagateBounds() ONCE before both passes. "
                "Pass 1 — schedule CRITICAL nodes (bounds->ub(node) == "
                "bounds->lb(node), i.e. zero mobility) in TopoSort(f) order, "
                "assigning each to bounds->lb(node); call TightenNodeLb/Ub (O(1)). "
                "Pass 2 — schedule all remaining timed nodes in TopoSort(f) "
                "order. For each node compute lb = max(bounds->lb(node), max "
                "assigned_cycle of its timed operands) — O(degree). "
                "Assign to bounds->ub(node) (ALAP, clamped to [lb, ub]) so that "
                "non-critical operations are deferred as late as possible; "
                "call TightenNodeLb/Ub (O(1)). "
                "DO NOT call bounds->PropagateBounds() inside either pass — "
                "it is O(n) per call and causes O(n²) total on 5000 nodes. "
                "This two-pass strategy keeps the critical path tight while "
                "minimising register pressure for non-critical values."
            ),
        ],
    }

    def mutation_instruction_for(self, island: Island, iteration: int) -> str:
        """
        Returns a mutation instruction string for the AI.

        Iterations 0, 1, 2 always use the bootstrap instructions — three
        maximally-diverse algorithmic families (ALAP, stage-balanced,
        critical-path-first) designed to seed distinct PPA design points.

        From iteration 3 onwards the normal per-island variant rotation
        takes over, encouraging exploration around whatever the bootstrap
        runs discovered.
        """
        bootstrap = self._BOOTSTRAP_INSTRUCTIONS.get(
            island.mutation_type,
            self._BOOTSTRAP_INSTRUCTIONS["agent_scheduler"],
        )
        if iteration < len(bootstrap):
            return bootstrap[iteration] + self._RUNTIME_WARNING

        # Normal rotation for iteration >= 3
        # Mutation target: AgentGeneratedScheduler() in
        # xls/scheduling/agent_generated_scheduler.cc.
        # Available APIs the variants can lean on:
        #   TopoSort(f), IsUntimed(node)
        #   bounds->lb(node), bounds->ub(node)        ← read from initial propagation
        #   bounds->TightenNodeLb/Ub(node, cycle)     ← O(1), call after each assign
        #   bounds->PropagateBounds()                  ← O(n), call ONCE before loop only
        #   delay_estimator.GetOperationDelayInPs(node)
        #   node->operands(), node->users(), node->GetType()->GetFlatBitCount()
        #
        # KEY: compute per-node lb as max(bounds->lb(node), max assigned_cycle of
        # timed operands) — O(degree) — instead of calling PropagateBounds() per node.
        variants = {
            "agent_scheduler": [
                # Variation 0: register-pressure-aware list scheduler
                (
                    "Implement a register-pressure-aware list scheduler. "
                    "Call bounds->PropagateBounds() ONCE before the loop. "
                    "Visit nodes via TopoSort(f), skip IsUntimed(node). "
                    "For each node compute lb = max(bounds->lb(node), max assigned_cycle "
                    "of its timed operands) — O(degree) — and ub = bounds->ub(node). "
                    "Pick a cycle in [lb, ub] that minimises boundary register cost: "
                    "operands registered across stage boundaries weighted by "
                    "node->GetType()->GetFlatBitCount(), plus users forced to later stages. "
                    "After each placement call bounds->TightenNodeLb/Ub (O(1) each). "
                    "DO NOT call bounds->PropagateBounds() inside the loop — O(n) per call."
                ),
                # Variation 1: ASAP with delay-model tie-break
                (
                    "Implement an ASAP-first heuristic with delay-model tie-breaking. "
                    "Call bounds->PropagateBounds() ONCE before the loop. "
                    "For each node in TopoSort order compute lb = max(bounds->lb(node), "
                    "max assigned_cycle of timed operands) — O(degree). "
                    "Pick the earliest cycle in [lb, bounds->ub(node)]; among equally "
                    "early cycles prefer the one whose same-stage operands leave the most "
                    "slack under clock_period_ps using delay_estimator.GetOperationDelayInPs. "
                    "Call TightenNodeLb/Ub after each choice (O(1)). "
                    "DO NOT call bounds->PropagateBounds() inside the loop."
                ),
                # Variation 2: mobility-driven greedy
                (
                    "Implement a mobility-aware greedy scheduler. "
                    "Call bounds->PropagateBounds() ONCE before the loop. "
                    "For each node compute mobility = bounds->ub(node) - bounds->lb(node) "
                    "and handle low-mobility (critical) nodes first — schedule them ASAP. "
                    "For high-mobility wide nodes (big GetFlatBitCount, many users), push "
                    "them to cycles that reduce live-out bits. "
                    "Compute per-node lb = max(bounds->lb(node), max assigned_cycle of "
                    "timed operands) — O(degree). "
                    "Call TightenNodeLb/Ub after each assignment (O(1)). "
                    "DO NOT call bounds->PropagateBounds() inside the loop — it is O(n) "
                    "per call and makes the scheduler O(n²) on 5000-node designs."
                ),
                # Variation 3: lookahead over candidate cycles
                (
                    "Implement a deterministic multistage heuristic with lightweight "
                    "lookahead. Call bounds->PropagateBounds() ONCE before the loop. "
                    "For each node in TopoSort order compute lb = max(bounds->lb(node), "
                    "max assigned_cycle of timed operands) — O(degree). "
                    "Iterate candidate_cycle in [lb, bounds->ub(node)] and score not just "
                    "this node's placement but the pressure it adds for its users (use "
                    "node->users() and their assigned cycles). "
                    "Pick the lowest-score cycle, record it in the ScheduleCycleMap, "
                    "call TightenNodeLb/Ub (O(1)). "
                    "DO NOT call bounds->PropagateBounds() inside the loop."
                ),
            ],
        }

        v = variants.get(island.mutation_type, variants["agent_scheduler"])
        # Offset by len(bootstrap) so iteration 3 → variant 0, not a re-use of it
        return v[(iteration - len(bootstrap)) % len(v)] + self._RUNTIME_WARNING
