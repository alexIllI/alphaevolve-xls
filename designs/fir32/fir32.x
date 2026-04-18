#![feature(type_inference_v2)]

// 32-tap FIR filter (Direct Form I) with balanced binary adder tree.
//
// Computation: y = sum(samples[i] * coeffs[i]) for i in 0..31
//
// Structure:
//   - 32 independent multiplies (fully parallel — high register pressure)
//   - 5-level balanced adder tree (log2(32) depth)
//
// Scheduling interest: the scheduler must decide how to spread 32 wide-input
// multiplies and 31 adder nodes across pipeline stages. Tight clock periods
// force multi-stage solutions with very different area/delay trade-offs.

pub fn main(samples: s16[32], coeffs: s16[32]) -> s64 {
    // Level 0: 32 independent multiply operations
    let p00 = (samples[ 0] as s64) * (coeffs[ 0] as s64);
    let p01 = (samples[ 1] as s64) * (coeffs[ 1] as s64);
    let p02 = (samples[ 2] as s64) * (coeffs[ 2] as s64);
    let p03 = (samples[ 3] as s64) * (coeffs[ 3] as s64);
    let p04 = (samples[ 4] as s64) * (coeffs[ 4] as s64);
    let p05 = (samples[ 5] as s64) * (coeffs[ 5] as s64);
    let p06 = (samples[ 6] as s64) * (coeffs[ 6] as s64);
    let p07 = (samples[ 7] as s64) * (coeffs[ 7] as s64);
    let p08 = (samples[ 8] as s64) * (coeffs[ 8] as s64);
    let p09 = (samples[ 9] as s64) * (coeffs[ 9] as s64);
    let p10 = (samples[10] as s64) * (coeffs[10] as s64);
    let p11 = (samples[11] as s64) * (coeffs[11] as s64);
    let p12 = (samples[12] as s64) * (coeffs[12] as s64);
    let p13 = (samples[13] as s64) * (coeffs[13] as s64);
    let p14 = (samples[14] as s64) * (coeffs[14] as s64);
    let p15 = (samples[15] as s64) * (coeffs[15] as s64);
    let p16 = (samples[16] as s64) * (coeffs[16] as s64);
    let p17 = (samples[17] as s64) * (coeffs[17] as s64);
    let p18 = (samples[18] as s64) * (coeffs[18] as s64);
    let p19 = (samples[19] as s64) * (coeffs[19] as s64);
    let p20 = (samples[20] as s64) * (coeffs[20] as s64);
    let p21 = (samples[21] as s64) * (coeffs[21] as s64);
    let p22 = (samples[22] as s64) * (coeffs[22] as s64);
    let p23 = (samples[23] as s64) * (coeffs[23] as s64);
    let p24 = (samples[24] as s64) * (coeffs[24] as s64);
    let p25 = (samples[25] as s64) * (coeffs[25] as s64);
    let p26 = (samples[26] as s64) * (coeffs[26] as s64);
    let p27 = (samples[27] as s64) * (coeffs[27] as s64);
    let p28 = (samples[28] as s64) * (coeffs[28] as s64);
    let p29 = (samples[29] as s64) * (coeffs[29] as s64);
    let p30 = (samples[30] as s64) * (coeffs[30] as s64);
    let p31 = (samples[31] as s64) * (coeffs[31] as s64);

    // Level 1: 16 pairwise sums
    let t00 = p00 + p01;  let t01 = p02 + p03;
    let t02 = p04 + p05;  let t03 = p06 + p07;
    let t04 = p08 + p09;  let t05 = p10 + p11;
    let t06 = p12 + p13;  let t07 = p14 + p15;
    let t08 = p16 + p17;  let t09 = p18 + p19;
    let t10 = p20 + p21;  let t11 = p22 + p23;
    let t12 = p24 + p25;  let t13 = p26 + p27;
    let t14 = p28 + p29;  let t15 = p30 + p31;

    // Level 2: 8 sums
    let u00 = t00 + t01;  let u01 = t02 + t03;
    let u02 = t04 + t05;  let u03 = t06 + t07;
    let u04 = t08 + t09;  let u05 = t10 + t11;
    let u06 = t12 + t13;  let u07 = t14 + t15;

    // Level 3: 4 sums
    let v00 = u00 + u01;  let v01 = u02 + u03;
    let v02 = u04 + u05;  let v03 = u06 + u07;

    // Level 4: 2 sums
    let w00 = v00 + v01;
    let w01 = v02 + v03;

    // Level 5: final accumulation
    w00 + w01
}
