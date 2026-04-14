# SDC Scheduling: System of Difference Constraints
**Source:** Cong & Zhang, "An Efficient and Versatile Scheduling Algorithm Based on SDC Formulation," DAC 2006.

## Core Idea
Schedule each operation to a pipeline stage by assigning a continuous variable `c[v]` (cycle number) to each node `v`. The problem is formulated as an LP with difference constraints (SDC = System of Difference Constraints).

## Variables
- `c[v]` ∈ ℤ≥0 : pipeline stage for node v
- `L` : pipeline length (number of stages − 1), minimized

## Fundamental Constraints

### 1. Data Dependence (Causal)
For every edge (u → v) in the dataflow graph:
```
c[v] - c[u] >= 0
```
A node cannot execute before its operands.

### 2. Timing (Clock) Constraints
Let `d(u→v)` = critical-path delay from u to v (in ps), `T` = clock period.
If `d(u→v) > T`, then u and v must be in different stages:
```
c[v] - c[u] >= 1
```
More precisely, for each pair (a, b) where the combinational path from a to b exceeds T:
```
c[b] - c[a] >= 1
```

### 3. Register Lifetime
For node u with last user at stage c[user]:
```
lifetime[u] = c[user] - c[u]   (extra stages value is kept in a register)
```

## Objective Function (Current XLS Implementation)
Minimize:
```
Σ c[v]                          (ASAP scheduling bias — tie-breaker)
+ α × Σ bits[v] × lifetime[v]   (minimize pipeline register bits)
```
Where α = `kObjectiveScaling` = 1024.

## Key Properties
- The constraint matrix is **Totally Unimodular (TUM)**: LP relaxation always gives integer solutions.
- This means no branch-and-bound is needed; standard LP (e.g., GLOP) suffices.
- The LP is solved once per schedule; very fast.

## Algorithmic Opportunities for Improvement
1. **Objective function**: The ASAP bias + lifetime minimization may not be optimal. Alternative objectives (force-directed, slack-based, resource-balanced) could reduce stage count or register pressure.
2. **Constraint set**: The minimal timing constraint set can be further pruned via transitive reduction.
3. **Initialization**: Warm-starting the LP with estimates from ASAP/ALAP could speed convergence and guide the solver toward better solutions.
4. **Multi-objective**: Pareto-optimal trade-off between stage count and register pressure.
