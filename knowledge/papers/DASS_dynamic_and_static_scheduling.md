# DASS: Combining Dynamic & Static Scheduling in High-level Synthesis
[cite_start]**Source:** Jianyi Cheng, Lana Josipović, George A. Constantinides, Paolo Ienne, and John Wickerson, "DASS: Combining Dynamic & Static Scheduling in High-level Synthesis," IEEE TCAD 2020. [cite: 1, 4, 7]

## Core Idea
[cite_start]The classic approach to High-Level Synthesis (HLS) scheduling is static, offering simpler circuitry and better resource sharing, while emerging dynamic scheduling offers faster hardware for non-trivial control flows. [cite: 9, 10] [cite_start]DASS combines these by identifying program regions where dynamic scheduling provides no performance advantage and statically scheduling them. [cite: 12] [cite_start]These statically-scheduled sections are then integrated as black-box components into the dynamically-scheduled dataflow circuit for the rest of the program. [cite: 13]

## Key Concepts

### 1. Static vs. Dynamic Scheduling Trade-offs
* [cite_start]**Static Scheduling (SS):** Schedules operations at compile-time, allowing for resource sharing and smaller area. [cite: 24, 43] [cite_start]However, it must make conservative worst-case assumptions for variable-latency operations and control flow. [cite: 44]
* [cite_start]**Dynamic Scheduling (DS):** Schedules operations at run-time using a dataflow architecture with handshaking signals. [cite: 45, 46] [cite_start]It executes operations as soon as inputs are valid, achieving higher throughput, but suffers from area overhead and lacks resource sharing. [cite: 47, 50, 51]

### 2. The SS Wrapper (Component Integration)
[cite_start]To integrate an SS FSM-based circuit into a DS dataflow circuit, a specialized wrapper is required to bridge the incompatible interfaces. [cite: 314, 320, 321]
* [cite_start]**Handling Validity ("Bubbles"):** A shift register synchronizes data and tracks whether inputs processed by the SS hardware are valid or invalid "bubbles." [cite: 330, 331] [cite_start]It ensures only valid data triggers the downstream valid signal. [cite: 338]
* [cite_start]**Handling Backpressure:** If a downstream component is not ready (nReady = 0), the wrapper disables the SS circuit's clock enable signal (ap_ce = 0), stalling the SS pipeline to preserve the output data. [cite: 351, 352]

### 3. Shared Memory Architecture
[cite_start]DS typically relies on expensive Load-Store Queues (LSQs) to resolve unpredictable memory conflicts at run-time. [cite: 236, 237]
* [cite_start]DASS statically analyzes memory nodes. [cite: 435] 
* [cite_start]If a complete set of memory accesses belongs to the SS hardware, it can bypass the LSQ and connect directly to a memory controller. [cite: 455, 457]
* [cite_start]An arbiter serializes accesses between the SS and DS hardware, granting priority to the SS circuit in a round-robin fashion. [cite: 468, 469, 470]

### 4. Optimal II Selection (Rate Analysis)
[cite_start]The effectiveness of the SS component depends heavily on selecting the correct Initiation Interval (II). [cite: 701]
* [cite_start]The optimal II ($II_{opt}$) is determined by the input data distribution (fraction of long-latency operations) and the maximum data processing rates allowed by the component's predecessors and successors. [cite: 721]
* [cite_start]Rate for a merge component: $r_{out} = r_{in1} + r_{in2}$ [cite: 772]
* [cite_start]Rate for a branch component: $r_{in1} = r_{in2} = r_{out1} + r_{out2}$ [cite: 773]

## Application to HLS Toolchains
* [cite_start]**Tool Integration:** The prototype implementation uses Xilinx Vivado HLS to synthesize the SS components and Dynamatic to synthesize the DS surroundings. [cite: 114, 477]
* [cite_start]**Pragma-Driven:** Users annotate the target SS functions and desired II constraints using pragmas. [cite: 53, 500]
* [cite_start]**Automated Flow:** The DASS tool automatically splits the code, synthesizes the SS functions, wraps them in handshaking logic, and merges them back into Dynamatic's backend scheduling graph. [cite: 501, 505, 507, 512]

## Advantages
* [cite_start]Retrieves 74% of the area savings typically achieved by switching entirely from DS to SS. [cite: 14]
* [cite_start]Achieves 135% of the performance benefits typically seen when switching entirely from SS to DS (meaning it can outperform both pure SS and pure DS approaches). [cite: 14]
* [cite_start]The shared memory interface successfully removed all LSQs in the evaluated benchmarks, significantly reducing area and memory latency overhead. [cite: 474, 849]