#![feature(type_inference_v2)]

// 4×4 integer matrix multiply: C = A × B
//
// A and B are 4×4 matrices of s16, stored row-major in flat 16-element arrays.
// C is a flat 16-element array of s32 (wide enough to avoid overflow).
//
// Computation: C[i*4+j] = sum_k A[i*4+k] * B[k*4+j]
//
// Structure:
//   - 64 independent multiply operations (4 per output element, 16 elements)
//   - 48 additions (3 additions per output element to reduce 4 products)
//   - All 16 output values are independent — high parallelism
//
// Scheduling interest: 64 multiplies with no inter-output dependencies give
// the scheduler freedom to place them across stages. The 16 independent
// reduction chains create heavy register pressure when many are in-flight.

pub fn main(a: s16[16], b: s16[16]) -> s32[16] {
    // Row 0 outputs
    let c00 = (a[ 0] as s32)*(b[ 0] as s32) + (a[ 1] as s32)*(b[ 4] as s32)
            + (a[ 2] as s32)*(b[ 8] as s32) + (a[ 3] as s32)*(b[12] as s32);
    let c01 = (a[ 0] as s32)*(b[ 1] as s32) + (a[ 1] as s32)*(b[ 5] as s32)
            + (a[ 2] as s32)*(b[ 9] as s32) + (a[ 3] as s32)*(b[13] as s32);
    let c02 = (a[ 0] as s32)*(b[ 2] as s32) + (a[ 1] as s32)*(b[ 6] as s32)
            + (a[ 2] as s32)*(b[10] as s32) + (a[ 3] as s32)*(b[14] as s32);
    let c03 = (a[ 0] as s32)*(b[ 3] as s32) + (a[ 1] as s32)*(b[ 7] as s32)
            + (a[ 2] as s32)*(b[11] as s32) + (a[ 3] as s32)*(b[15] as s32);

    // Row 1 outputs
    let c10 = (a[ 4] as s32)*(b[ 0] as s32) + (a[ 5] as s32)*(b[ 4] as s32)
            + (a[ 6] as s32)*(b[ 8] as s32) + (a[ 7] as s32)*(b[12] as s32);
    let c11 = (a[ 4] as s32)*(b[ 1] as s32) + (a[ 5] as s32)*(b[ 5] as s32)
            + (a[ 6] as s32)*(b[ 9] as s32) + (a[ 7] as s32)*(b[13] as s32);
    let c12 = (a[ 4] as s32)*(b[ 2] as s32) + (a[ 5] as s32)*(b[ 6] as s32)
            + (a[ 6] as s32)*(b[10] as s32) + (a[ 7] as s32)*(b[14] as s32);
    let c13 = (a[ 4] as s32)*(b[ 3] as s32) + (a[ 5] as s32)*(b[ 7] as s32)
            + (a[ 6] as s32)*(b[11] as s32) + (a[ 7] as s32)*(b[15] as s32);

    // Row 2 outputs
    let c20 = (a[ 8] as s32)*(b[ 0] as s32) + (a[ 9] as s32)*(b[ 4] as s32)
            + (a[10] as s32)*(b[ 8] as s32) + (a[11] as s32)*(b[12] as s32);
    let c21 = (a[ 8] as s32)*(b[ 1] as s32) + (a[ 9] as s32)*(b[ 5] as s32)
            + (a[10] as s32)*(b[ 9] as s32) + (a[11] as s32)*(b[13] as s32);
    let c22 = (a[ 8] as s32)*(b[ 2] as s32) + (a[ 9] as s32)*(b[ 6] as s32)
            + (a[10] as s32)*(b[10] as s32) + (a[11] as s32)*(b[14] as s32);
    let c23 = (a[ 8] as s32)*(b[ 3] as s32) + (a[ 9] as s32)*(b[ 7] as s32)
            + (a[10] as s32)*(b[11] as s32) + (a[11] as s32)*(b[15] as s32);

    // Row 3 outputs
    let c30 = (a[12] as s32)*(b[ 0] as s32) + (a[13] as s32)*(b[ 4] as s32)
            + (a[14] as s32)*(b[ 8] as s32) + (a[15] as s32)*(b[12] as s32);
    let c31 = (a[12] as s32)*(b[ 1] as s32) + (a[13] as s32)*(b[ 5] as s32)
            + (a[14] as s32)*(b[ 9] as s32) + (a[15] as s32)*(b[13] as s32);
    let c32 = (a[12] as s32)*(b[ 2] as s32) + (a[13] as s32)*(b[ 6] as s32)
            + (a[14] as s32)*(b[10] as s32) + (a[15] as s32)*(b[14] as s32);
    let c33 = (a[12] as s32)*(b[ 3] as s32) + (a[13] as s32)*(b[ 7] as s32)
            + (a[14] as s32)*(b[11] as s32) + (a[15] as s32)*(b[15] as s32);

    s32[16]:[c00, c01, c02, c03,
             c10, c11, c12, c13,
             c20, c21, c22, c23,
             c30, c31, c32, c33]
}
