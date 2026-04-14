# Force-Directed Scheduling
**Source:** Paulin & Knight, "Force-Directed Scheduling for the Behavioral Synthesis of ASICs," TCAD 1989.

## Core Idea
Model the scheduling problem as a physical system where each node exerts "forces" on the schedule. The goal is to distribute operations evenly across time steps by minimizing concentration of resource-conflicting operations in the same cycle.

## Key Concepts

### 1. Time Frame (Mobility)
For each node v, compute:
- `ASAP[v]`: earliest possible cycle (using critical-path from sources)
- `ALAP[v]`: latest possible cycle (using reverse critical-path from sinks)
- `mobility[v]` = ALAP[v] - ASAP[v]  (scheduling slack)

Nodes with zero mobility are on the critical path and must be scheduled in exactly one cycle.

### 2. Probability Distribution
If a node v has time frame [ASAP[v], ALAP[v]] and all schedules are equally likely:
```
P[v, t] = 1 / (ALAP[v] - ASAP[v] + 1)   for t ∈ [ASAP[v], ALAP[v]]
         = 0                               otherwise
```

### 3. Type Distribution
For each resource type R (e.g., multiplier, adder) and time step t:
```
q[R, t] = Σ P[v, t]   for all v of type R
```
This represents the expected number of operations of type R in step t.

### 4. Forces
When scheduling node v at time t, the **self-force** is:
```
SF[v, t] = Σ_{t' ∈ [ASAP,ALAP]} q[type(v), t'] × ΔP[v, t', t]
```
Where ΔP is the change in probability when v is fixed to step t.

**Predecessor/successor forces** account for how fixing v affects connected nodes.

Total force: `F[v, t] = SF[v, t] + Σ PredForce + Σ SuccForce`

### 5. Scheduling Decision
Schedule v at the time step t* that minimizes total force F[v, t*].

## Application to XLS SDC Objective

Instead of adding force directly to the LP (which would make it nonlinear), derive force-like weights for the LP objective:

```
// Force-inspired LP weight for node v's cycle variable:
// Nodes with high force gradient (steep force curve) need tighter scheduling.
// Use mobility as inverse weight: low-mobility nodes get large ASAP bias.
weight[v] = kScale / (mobility[v] + 1)

// Modified objective:
objective += weight[v] * cycle_var_[v]   // ASAP bias weighted by criticality
objective += kAlpha * bits[v] * lifetime_var_[v]  // register pressure
```

## Advantages
- Naturally handles resource balancing without explicit resource constraints
- Produces schedules with lower peak register demand
- Can be computed efficiently in O(V × T) time
- Complements LP-based scheduling (provides better initial guidance)
