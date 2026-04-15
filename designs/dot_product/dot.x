// Benchmark Design: 8-element Dot Product
// DSLX source
// Computes: out = sum(a[i] * b[i]) for i in 0..7
// Good for: testing scheduling of parallel multiply paths + accumulation tree

fn dot_product(
    a0: u16, a1: u16, a2: u16, a3: u16,
    a4: u16, a5: u16, a6: u16, a7: u16,
    b0: u16, b1: u16, b2: u16, b3: u16,
    b4: u16, b5: u16, b6: u16, b7: u16,
) -> u32 {
    let p0 = (a0 as u32) * (b0 as u32);
    let p1 = (a1 as u32) * (b1 as u32);
    let p2 = (a2 as u32) * (b2 as u32);
    let p3 = (a3 as u32) * (b3 as u32);
    let p4 = (a4 as u32) * (b4 as u32);
    let p5 = (a5 as u32) * (b5 as u32);
    let p6 = (a6 as u32) * (b6 as u32);
    let p7 = (a7 as u32) * (b7 as u32);
    // Balanced adder tree (log2 depth = 3)
    let t01 = p0 + p1;
    let t23 = p2 + p3;
    let t45 = p4 + p5;
    let t67 = p6 + p7;
    let t0123 = t01 + t23;
    let t4567 = t45 + t67;
    t0123 + t4567
}
