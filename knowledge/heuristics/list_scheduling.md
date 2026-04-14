# List Scheduling and ASAP/ALAP Heuristics
**Sources:** Classic HLS textbook algorithms (De Micheli 1994, Gajski 1992).

## ASAP Scheduling (As Soon As Possible)
Schedule each node in the earliest possible cycle, respecting only data dependences.

```
ASAP[source] = 0
For each node v in topological order:
    ASAP[v] = max(ASAP[u] + 1) for all predecessors u of v
              (or 0 if v has no predecessors)
```

## ALAP Scheduling (As Late As Possible)
Schedule each node in the latest possible cycle without delaying any output.

```
ALAP[sink] = pipeline_length - 1
For each node v in reverse topological order:
    ALAP[v] = min(ALAP[u] - 1) for all successors u of v
              (or pipeline_length - 1 if v has no successors)
```

## Mobility (Slack)
```
mobility[v] = ALAP[v] - ASAP[v]
```
- mobility = 0: critical path, zero scheduling freedom
- mobility > 0: flexible, can be shifted to reduce register pressure

## List Scheduling
A greedy algorithm that schedules one node per cycle based on a priority function:

```
ready_list = nodes with no unscheduled predecessors
For each cycle t = 0, 1, ...:
    scheduled = select from ready_list by priority(v)
    Assign: c[v] = t
    Update ready_list with newly unblocked nodes
```

### Priority Functions
1. **Critical Path** (most common): `priority[v] = -ALAP[v]` (schedule critical nodes first)
2. **Slack-based**: `priority[v] = mobility[v]` (reverse: tight nodes first)
3. **Urgency**: `priority[v] = 1 / (ALAP[v] - t + 1)` (urgency increases as deadline approaches)
4. **Fan-out weighted**: `priority[v] = fan_out[v] / mobility[v]` (prioritize nodes that unlock many successors)

## Register Minimization via Lifetime-Aware Scheduling

Key insight: Pipeline registers hold values that are produced in one stage and consumed in a later stage. The number of register bits for node v is:
```
reg_bits[v] = bits[v] × (c[last_user[v]] - c[v])   if c[last_user[v]] > c[v]
            = 0                                       otherwise
```

To minimize total register bits, prefer scheduling strategies that:
1. Schedule producers close to (or in the same stage as) their consumers
2. Avoid long-lived values crossing many stage boundaries
3. Group nodes with shared consumers in the same stage

## Application to XLS
In the SDC objective, the `lifetime_var_[node]` already models this. However, the current ASAP bias `Σ c[v]` conflicts with lifetime minimization: scheduling early (ASAP) sometimes forces consumers to be far from producers.

**Improvement opportunity**: Replace the flat ASAP bias with a slack-aware term:
```
// Instead of: objective += c[v]   (flat ASAP bias)
// Use:        objective += mobility_weight[v] * c[v]
// Where:      mobility_weight[v] = 1.0 / (ALAP[v] - ASAP[v] + 1)
// Effect: critical nodes (mobility=0) get max ASAP pressure;
//         flexible nodes get low pressure → can be moved to reduce register cost
```
