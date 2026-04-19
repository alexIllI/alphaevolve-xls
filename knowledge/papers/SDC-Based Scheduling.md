# SDC-Based Scheduling
[cite_start]**Source:** Jason Cong & Zhiru Zhang, "An Efficient and Versatile Scheduling Algorithm Based On SDC Formulation," DAC 2006[cite: 955, 957, 974].

## Core Idea
[cite_start]Convert a rich set of scheduling constraints (data/control dependencies, relative timing, and resource limits) into a System of Difference Constraints (SDC)[cite: 961]. [cite_start]By formulating scheduling as a special class of Linear Programming (LP) where the constraint matrix is totally unimodular, the algorithm guarantees optimal, mathematically sound integer solutions in polynomial time[cite: 1009, 1138].

## Key Concepts

### 1. Scheduling Variables
[cite_start]Each operation node $v$ is assigned scheduling variables $sv_i(v)$ that capture its relative temporal position (control state) in the final schedule[cite: 1053, 1058].
* [cite_start]$sv_{beg}(v)$: Indicates the start state[cite: 1056].
* [cite_start]$sv_{end}(v)$: Indicates the end state (for multicycle or pipelined operations)[cite: 1056].

### 2. Dependency Constraints
[cite_start]Structural dependencies are modeled as exact difference constraints[cite: 1069].
* **Data Dependency:** If node $v_j$ depends on $v_i$, it cannot start until $v_i$ finishes: 
    [cite_start]$sv_{end}(v_i) - sv_{beg}(v_j) \le 0$ [cite: 1076, 1077]
* **Control Dependency:** Operations in a target basic block $bb_j$ cannot start before the source block $bb_i$ finishes: 
    [cite_start]$sv_{end}(ssnk(bb_i)) - sv_{beg}(ssrc(bb_j)) \le 0$ [cite: 1081, 1082]

### 3. Timing Constraints
[cite_start]Timing requirements are directly mapped into the mathematical model[cite: 1084]:
* **Maximum Timing Constraint:** Limits the latency distance between two operations:
    [cite_start]$sv_{beg}(v_j) - sv_{beg}(v_i) \le u_{ij}$ [cite: 1090, 1091]
* **Minimum Timing Constraint:** Ensures an operation follows another by at least a set number of cycles:
    [cite_start]$sv_{beg}(v_i) - sv_{beg}(v_j) \le -l_{ij}$ [cite: 1088]
* **Cycle Time (Frequency):** Combinational paths exceeding the target clock period $T_{clk}$ are forced to span multiple clock cycles: 
    [cite_start]$sv_{beg}(v_i) - sv_{beg}(v_j) \le -(\lceil D(ccp(v_i, v_j)) / T_{clk} \rceil - 1)$ [cite: 1105, 1107]

### 4. Resource Constraints
[cite_start]Because exact resource-constrained scheduling is an NP-hard problem, resources are managed heuristically by generating feasible linear orders[cite: 1110, 1111].
* [cite_start]If there are $c_{res}$ functional units available, the algorithm enforces precedence edges between nodes of the same resource type to ensure no more than $c_{res}$ operations execute concurrently[cite: 1113, 1116, 1120].
* Constraint applied to sequence nodes: 
    [cite_start]$sv_{beg}(v_i^\pi) - sv_{beg}(v_j^\pi) \le -Latency(v_i^\pi)$ [cite: 1114]

### 5. Solving the SDC
[cite_start]The SDC constraint graph can be checked for feasibility efficiently using a single-source shortest path algorithm (like Bellman-Ford) to find negative cycles[cite: 1135, 1136]. [cite_start]Because the underlying matrix is totally unimodular, solving the LP naturally yields exact integer solutions, translating directly into a valid schedule[cite: 1138, 1211].

## Application to Synthesis Objectives
[cite_start]Implemented in the xPilot behavioral synthesis system [cite: 1244][cite_start], the SDC framework allows the scheduler to optimize various linear objective functions under one unified system[cite: 1142]:
* [cite_start]**ASAP / ALAP Scheduling:** Minimize or maximize the sum of all starting scheduling variables[cite: 1145, 1150].
* [cite_start]**Longest Path Latency:** Minimize the schedule variable of the exit basic block's super-sink to reduce the worst-case path[cite: 1155, 1156].
* [cite_start]**Expected Overall Latency:** Use branching probabilities and estimated loop iteration counts from profiling data to create a weighted linear objective, improving average-case performance[cite: 1184, 1188].
* [cite_start]**Slack Distribution:** Maximize total edge slacks or node slacks to allow for downstream area/power reduction or to safely accommodate long interconnect delays without violating timing[cite: 1195, 1201, 1202, 1204].

## Advantages
* [cite_start]Handles both data-flow-intensive and control-flow-intensive designs efficiently[cite: 1231, 1232].
* [cite_start]Guarantees optimal mathematical solutions in polynomial time, avoiding the exponential worst-case complexity of traditional Control-Flow-based heuristics[cite: 1208, 1211].
* [cite_start]Natively supports relative I/O timing limits, operation chaining, and physical interconnect delays[cite: 1234, 1237, 1240].