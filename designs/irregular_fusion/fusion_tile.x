#![feature(type_inference_v2)]

// Irregular arithmetic benchmark for scheduler evolution.
//
// Why this is useful:
// - mixes adds, subtracts, shifts, compares, mux-like control, and multiplies
// - reuses shared features across several outputs to create fanout pressure
// - creates uneven reduction depth and lifetime pressure across outputs
// - stays as a pure function, so it fits the current AlphaEvolve-XLS flow
//
// This is intended to sit between "regular" dense kernels like GEMM and very
// structured transforms like IDCT: it is arithmetic-heavy, but intentionally
// non-uniform.

fn clamp_i16(x: s32) -> s16 {
  if x > s32:32767 {
    s16:32767
  } else if x < s32:-32768 {
    s16:-32768
  } else {
    x as s16
  }
}

fn max_s16(a: s16, b: s16) -> s16 {
  if a > b { a } else { b }
}

fn min_s16(a: s16, b: s16) -> s16 {
  if a < b { a } else { b }
}

fn mul16(a: s16, b: s16) -> s32 {
  (a as s32) * (b as s32)
}

pub fn main(x: s16[16], k: s16[12], bias: s32[4]) -> s16[4] {
  // Shared feature extraction with uneven dependency depth.
  let t0 = (x[u32:0] as s32) + (x[u32:5] as s32) - ((x[u32:10] as s32) >> u32:1);
  let t1 = (x[u32:1] as s32) - (x[u32:6] as s32) + ((x[u32:11] as s32) << u32:1);
  let t2 = (x[u32:2] as s32) + (x[u32:7] as s32) + (((x[u32:12] as s32) - (x[u32:13] as s32)) >> u32:1);
  let t3 = (x[u32:3] as s32) - (x[u32:4] as s32) + (((x[u32:14] as s32) + (x[u32:15] as s32)) >> u32:2);
  let t4 = (x[u32:0] as s32) + (x[u32:1] as s32) + (x[u32:2] as s32) - (x[u32:8] as s32) - (x[u32:9] as s32);
  let t5 = (x[u32:3] as s32) + (x[u32:6] as s32) + (x[u32:12] as s32) + ((x[u32:15] as s32) >> u32:1);
  let t6 = (x[u32:5] as s32) - (x[u32:7] as s32) + (x[u32:10] as s32) + (x[u32:14] as s32);
  let t7 = (x[u32:8] as s32) + (x[u32:11] as s32) - ((x[u32:13] as s32) >> u32:1) + (x[u32:4] as s32);

  // Quantize features back to s16 before the weighted stage.
  let f0 = clamp_i16(t0);
  let f1 = clamp_i16(t1);
  let f2 = clamp_i16(t2);
  let f3 = clamp_i16(t3);
  let f4 = clamp_i16(t4);
  let f5 = clamp_i16(t5);
  let f6 = clamp_i16(t6);
  let f7 = clamp_i16(t7);

  // Gated feature mixing introduces compare/select structure.
  let g0 = clamp_i16(
    if f0 > f1 {
      (f0 as s32) + (min_s16(f2, f3) as s32)
    } else {
      (f1 as s32) + (max_s16(f4, f5) as s32)
    }
  );
  let g1 = clamp_i16(
    if f2 > f5 {
      (f2 as s32) - (f4 as s32)
    } else {
      (f5 as s32) - (f1 as s32)
    }
  );
  let g2 = clamp_i16(
    if f6 < f7 {
      (f6 as s32) + (f0 as s32)
    } else {
      (f7 as s32) + (f3 as s32)
    }
  );
  let g3 = if ((f4 as s32) + (f6 as s32)) > ((f1 as s32) + (f7 as s32)) {
    max_s16(f4, f6)
  } else {
    min_s16(f1, f7)
  };

  // Weighted stage with deliberate feature reuse.
  let m00 = mul16(f0, k[u32:0]);
  let m01 = mul16(f1, k[u32:1]);
  let m02 = mul16(g0, k[u32:2]);
  let m03 = mul16(g1, k[u32:3]);
  let m10 = mul16(f2, k[u32:4]);
  let m11 = mul16(f3, k[u32:5]);
  let m12 = mul16(g2, k[u32:6]);
  let m13 = mul16(g3, k[u32:7]);
  let m20 = mul16(max_s16(f4, f6), k[u32:8]);
  let m21 = mul16(min_s16(f5, f7), k[u32:9]);
  let m22 = mul16(clamp_i16((g0 as s32) + (g2 as s32)), k[u32:10]);
  let m23 = mul16(clamp_i16((g1 as s32) - (g3 as s32)), k[u32:11]);

  // Irregular output reductions. Each output sees a different shape.
  let acc0 =
      bias[u32:0] + m00 + m02 + ((m10 + m20) >> u32:1) +
      (if g0 > g1 { m22 } else { -m22 });
  let acc1 =
      bias[u32:1] + m01 + m03 + m11 + ((m21 - m23) >> u32:1) +
      (if f6 > f2 { m20 } else { -m10 });
  let acc2 =
      bias[u32:2] + m12 + m13 + ((m00 + m01 + m22) >> u32:1) +
      (if g2 < g3 { m23 } else { -m21 });
  let acc3 =
      bias[u32:3] + m20 + m21 + m22 + ((m02 + m03 + m12 + m13) >> u32:2) +
      (if f4 > f5 { m11 } else { -m01 });

  s16[4]:[
    clamp_i16(acc0),
    clamp_i16(acc1),
    clamp_i16(acc2),
    clamp_i16(acc3),
  ]
}

#[test]
fn zero_input_bias_only_test() {
  let x = s16[16]:[0, ...];
  let k = s16[12]:[0, ...];
  let bias = s32[4]:[11, -7, 23, -19];
  assert_eq(main(x, k, bias), s16[4]:[11, -7, 23, -19])
}
