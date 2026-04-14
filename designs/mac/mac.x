// Benchmark Design: Multiply-Accumulate (MAC)
// DSLX source — verified working with XLS toolchain
// Usage: 32-bit unsigned MAC: out = a*b + c
// Good for: testing basic pipeline formation (1 mul + 1 add)

fn my_mac(a: u32, b: u32, c: u32) -> u32 {
    a * b + c
}
