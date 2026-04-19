# SAT-Based Scheduling Algorithms in High-Level Synthesis
**Sources:** * S. Ogrenci Memik & F. Fallah, "Accelerated SAT-based Scheduling of Control/Data Flow Graphs," ICCD 2002.
* H. Jiang et al., "SAT-based Scheduling Algorithm for High-level Synthesis Considering Resource Sharing," ISCAS 2022.

## Core Idea
Formulating the resource-constrained scheduling problem in High-Level Synthesis (HLS) as a Boolean Satisfiability (SAT) problem. Unlike traditional heuristics (like list scheduling) that yield sub-optimal results, SAT-based formulations guarantee mathematically optimal scheduling and binding solutions. To combat the NP-complete nature of scheduling and the large search space of SAT, these approaches employ advanced variable bounding, conflict learning, and graph-theory algorithms (like Min-Cost Network Flow) to drastically accelerate solver execution.

## Key Concepts

### 1. Boolean Formulation of Scheduling Constraints
The foundational SAT scheduling model introduces boolean variables $x_{ijk}$ which evaluate to true if operation $i$ is scheduled at control step $j$ on resource $k$.
* **Uniqueness:** Each operation must be assigned to exactly one valid control step (using mutually exclusive clauses).
* **Data Dependency:** If operation $v$ depends on $u$, constraints guarantee $v$ cannot start until $u$ completes its execution delay.
* **Resource Limits:** Clauses are generated for every control step to ensure the number of operations assigned to a specific resource type does not exceed its physical availability.

### 2. Search Space Pruning and Bounding
SAT solvers spend significantly more time proving a problem is *infeasible* than solving a feasible one. 
* **ASAP/ALAP Bounding:** Instead of creating variables for all possible control steps, As-Soon-As-Possible (ASAP) and As-Late-As-Possible (ALAP) limits are calculated first. This defines a tight execution interval, eliminating a massive number of variables and clauses before the solver even runs.
* **Linear Search Optimization:** When searching for optimal latency, a linear search (e.g., iteratively decreasing an upper bound) is preferred over binary search to minimize the number of computationally expensive *infeasible* instances the solver encounters.

### 3. Explicit Resource Sharing Constraints
Modern SAT formulations address scheduling and resource binding *simultaneously* by encoding resource-sharing directly.
* **Sharing Variables:** Introduces variables $RS_{ij}$ indicating if operations $i$ and $j$ share the exact same hardware instance.
* **Connectivity Constraints:** Formulates logic requiring a minimum number of edges ($n - \alpha_{r}$) to maintain valid connectedness in grouping graphs (where $n$ is the number of nodes and $\alpha_{r}$ is the number of resource instances).
* **Conflict Learning:** If a SAT grouping solution is found to be invalid during scheduling evaluation, the solver appends negative clauses to immediately prune that specific sub-space from future exploration.

### 4. Min-Cost Network Flow (MCNF) Acceleration
Because independent scheduling is called repeatedly during design space exploration to evaluate SAT grouping results, it can become a bottleneck.
* The system substitutes standard linear programming with a Min-Cost Network Flow (MCNF) algorithm.
* Difference constraints are mapped as cost variables on a directed acyclic graph.
* This leverages the native graph structure of the code, evaluating constraints and detecting negative cycles (using Bellman-Ford) much faster than standard solvers.

## Application to HLS Toolchains
* **Control/Data Flow Graph (CDFG) Transformation:** To handle conditional control nodes (like `if/else`), dependencies are transformed. Techniques like speculative execution remove strict control barriers and replace them with data dependencies at the merge points, allowing SAT solvers to schedule branches concurrently.
* **Iterative Tool Flow:** Frameworks utilize pure SAT solvers (like Chaff or MiniSat) combined with graph libraries (like LEMON) to iteratively refine latency and resource allocations until optimal criteria are met.

## Advantages
* **Mathematical Optimality:** Eliminates the guesswork of heuristic approaches, guaranteeing the lowest possible latency for a given set of constraints.
* **Speed Superiority:** With bounded variable domains, SAT-based scheduling can outperform leading commercial Integer Linear Program (ILP) solvers (like CPLEX) by up to 59x in CPU time. MCNF acceleration further provides up to a 33x speedup over standard System of Difference Constraints (SDC) scheduling.
* **Holistic Design Quality:** By solving scheduling and binding concurrently, the designs achieve higher maximum clock frequencies ($F_{max}$) and tighter utilization of logic and on-chip registers compared to traditional decoupled methods.