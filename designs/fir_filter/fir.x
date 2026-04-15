// Benchmark Design: 8-tap FIR Filter (Direct Form I)
// DSLX source
// Computes: y = sum(coeff[i] * x[i]) for i in 0..7
// Good for: high register pressure, many multiplications, good pipeline spread

fn fir8(
    x0: s16, x1: s16, x2: s16, x3: s16,
    x4: s16, x5: s16, x6: s16, x7: s16,
    h0: s16, h1: s16, h2: s16, h3: s16,
    h4: s16, h5: s16, h6: s16, h7: s16,
) -> s32 {
    let p0 = (x0 as s32) * (h0 as s32);
    let p1 = (x1 as s32) * (h1 as s32);
    let p2 = (x2 as s32) * (h2 as s32);
    let p3 = (x3 as s32) * (h3 as s32);
    let p4 = (x4 as s32) * (h4 as s32);
    let p5 = (x5 as s32) * (h5 as s32);
    let p6 = (x6 as s32) * (h6 as s32);
    let p7 = (x7 as s32) * (h7 as s32);
    // Adder tree: two levels of reduction
    let t01 = p0 + p1;
    let t23 = p2 + p3;
    let t45 = p4 + p5;
    let t67 = p6 + p7;
    let t0123 = t01 + t23;
    let t4567 = t45 + t67;
    t0123 + t4567
}
