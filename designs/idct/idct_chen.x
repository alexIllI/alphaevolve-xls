#![feature(type_inference_v2)]

// 2D 8×8 Inverse Discrete Cosine Transform (IDCT) — Chen et al. (1977)
//
// Port of the Go standard library JPEG IDCT:
//   https://golang.org/src/image/jpeg/idct.go
//
// Processes one JPEG Minimum Coded Unit (MCU): 64 DCT coefficients (s32[64])
// in two passes — first row-wise, then column-wise — yielding 64 pixel values.
//
// Structure:
//   - idct_rows: 8 calls to idct_row, each with ~20 multiply + ~25 add/shift ops
//   - idct_cols: 8 calls to idct_col, same structure
//   - Total: ~360 multiplies + ~400 additions/shifts = ~1500 IR nodes
//
// Scheduling interest: row and column passes are fully independent of each
// other internally but feed from one to the next. Mixed multiply/shift/add
// operations with very different latencies create rich scheduling trade-offs.
// Tighter clock periods force more pipeline stages with non-obvious placement.

const COEFF_PER_MCU   = u32:64;
const COEFF_PER_MCU_U8 = u8:64;
const W1 = s32:2841;    // 2048*sqrt(2)*cos(1*pi/16)
const W2 = s32:2676;    // 2048*sqrt(2)*cos(2*pi/16)
const W3 = s32:2408;    // 2048*sqrt(2)*cos(3*pi/16)
const W5 = s32:1609;    // 2048*sqrt(2)*cos(5*pi/16)
const W6 = s32:1108;    // 2048*sqrt(2)*cos(6*pi/16)
const W7 = s32:565;     // 2048*sqrt(2)*cos(7*pi/16)
const R2 = s32:181;     // 256/sqrt(2)

fn idct_row(s: s32[8]) -> s32[8] {
  let w1pw7 = W1 + W7;
  let w1mw7 = W1 - W7;
  let w2pw6 = W2 + W6;
  let w2mw6 = W2 - W6;
  let w3pw5 = W3 + W5;
  let w3mw5 = W3 - W5;
  let x0 = (s[u8:0] << u32:11) + s32:128;
  let x1 = s[u8:4] << u32:11;
  let x2 = s[u8:6];
  let x3 = s[u8:2];
  let x4 = s[u8:1];
  let x5 = s[u8:7];
  let x6 = s[u8:5];
  let x7 = s[u8:3];

  let x8 = W7 * (x4 + x5);
  let x4 = x8 + w1mw7 * x4;
  let x5 = x8 - w1pw7 * x5;
  let x8 = W3 * (x6 + x7);
  let x6 = x8 - w3mw5 * x6;
  let x7 = x8 - w3pw5 * x7;

  let x8 = x0 + x1;
  let x0 = x0 - x1;
  let x1 = W6 * (x3 + x2);
  let x2 = x1 - w2pw6 * x2;
  let x3 = x1 + w2mw6 * x3;
  let x1 = x4 + x6;
  let x4 = x4 - x6;
  let x6 = x5 + x7;
  let x5 = x5 - x7;

  let x7 = x8 + x3;
  let x8 = x8 - x3;
  let x3 = x0 + x2;
  let x0 = x0 - x2;
  let x2 = (R2 * (x4 + x5) + s32:128) >> u32:8;
  let x4 = (R2 * (x4 - x5) + s32:128) >> u32:8;

  s32[8]:[
    (x7 + x1) >> u32:8,
    (x3 + x2) >> u32:8,
    (x0 + x4) >> u32:8,
    (x8 + x6) >> u32:8,
    (x8 - x6) >> u32:8,
    (x0 - x4) >> u32:8,
    (x3 - x2) >> u32:8,
    (x7 - x1) >> u32:8,
  ]
}

fn get_row(a: s32[COEFF_PER_MCU], rowno: u8) -> s32[8] {
  s32[8]:[
    a[u8:8 * rowno + u8:0],
    a[u8:8 * rowno + u8:1],
    a[u8:8 * rowno + u8:2],
    a[u8:8 * rowno + u8:3],
    a[u8:8 * rowno + u8:4],
    a[u8:8 * rowno + u8:5],
    a[u8:8 * rowno + u8:6],
    a[u8:8 * rowno + u8:7],
  ]
}

fn idct_rows(f: s32[COEFF_PER_MCU]) -> s32[COEFF_PER_MCU] {
  let row0 = idct_row(get_row(f, u8:0));
  let row1 = idct_row(get_row(f, u8:1));
  let row2 = idct_row(get_row(f, u8:2));
  let row3 = idct_row(get_row(f, u8:3));
  let row4 = idct_row(get_row(f, u8:4));
  let row5 = idct_row(get_row(f, u8:5));
  let row6 = idct_row(get_row(f, u8:6));
  let row7 = idct_row(get_row(f, u8:7));
  row0 ++ row1 ++ row2 ++ row3 ++ row4 ++ row5 ++ row6 ++ row7
}

fn idct_col(s: s32[8]) -> s32[8] {
  let w1pw7 = W1 + W7;
  let w1mw7 = W1 - W7;
  let w2pw6 = W2 + W6;
  let w2mw6 = W2 - W6;
  let w3pw5 = W3 + W5;
  let w3mw5 = W3 - W5;

  let y0 = (s[u8:0] << u32:8) + s32:8192;
  let y1 = s[u8:4] << u32:8;
  let y2 = s[u8:6];
  let y3 = s[u8:2];
  let y4 = s[u8:1];
  let y5 = s[u8:7];
  let y6 = s[u8:5];
  let y7 = s[u8:3];

  let y8 = W7 * (y4 + y5) + s32:4;
  let y4 = (y8 + w1mw7 * y4) >> u32:3;
  let y5 = (y8 - w1pw7 * y5) >> u32:3;
  let y8 = W3 * (y6 + y7) + s32:4;
  let y6 = (y8 - w3mw5 * y6) >> u32:3;
  let y7 = (y8 - w3pw5 * y7) >> u32:3;

  let y8 = y0 + y1;
  let y0 = y0 - y1;
  let y1 = W6 * (y3 + y2) + s32:4;
  let y2 = (y1 - w2pw6 * y2) >> u32:3;
  let y3 = (y1 + w2mw6 * y3) >> u32:3;
  let y1 = y4 + y6;
  let y4 = y4 - y6;
  let y6 = y5 + y7;
  let y5 = y5 - y7;

  let y7 = y8 + y3;
  let y8 = y8 - y3;
  let y3 = y0 + y2;
  let y0 = y0 - y2;
  let y2 = (R2 * (y4 + y5) + s32:128) >> u32:8;
  let y4 = (R2 * (y4 - y5) + s32:128) >> u32:8;

  s32[8]:[
    (y7 + y1) >> u32:14,
    (y3 + y2) >> u32:14,
    (y0 + y4) >> u32:14,
    (y8 + y6) >> u32:14,
    (y8 - y6) >> u32:14,
    (y0 - y4) >> u32:14,
    (y3 - y2) >> u32:14,
    (y7 - y1) >> u32:14,
  ]
}

fn get_col(a: s32[COEFF_PER_MCU], colno: u8) -> s32[8] {
  s32[8]:[
    a[u8:8 * u8:0 + colno],
    a[u8:8 * u8:1 + colno],
    a[u8:8 * u8:2 + colno],
    a[u8:8 * u8:3 + colno],
    a[u8:8 * u8:4 + colno],
    a[u8:8 * u8:5 + colno],
    a[u8:8 * u8:6 + colno],
    a[u8:8 * u8:7 + colno],
  ]
}

fn idct_cols(f: s32[COEFF_PER_MCU]) -> s32[COEFF_PER_MCU] {
  let col0 = idct_col(get_col(f, u8:0));
  let col1 = idct_col(get_col(f, u8:1));
  let col2 = idct_col(get_col(f, u8:2));
  let col3 = idct_col(get_col(f, u8:3));
  let col4 = idct_col(get_col(f, u8:4));
  let col5 = idct_col(get_col(f, u8:5));
  let col6 = idct_col(get_col(f, u8:6));
  let col7 = idct_col(get_col(f, u8:7));
  for (i, accum): (u8, s32[COEFF_PER_MCU]) in u8:0..COEFF_PER_MCU_U8 {
    let val: s32 = match i & u8:7 {
      u8:0 => col0[i >> u8:3],
      u8:1 => col1[i >> u8:3],
      u8:2 => col2[i >> u8:3],
      u8:3 => col3[i >> u8:3],
      u8:4 => col4[i >> u8:3],
      u8:5 => col5[i >> u8:3],
      u8:6 => col6[i >> u8:3],
      u8:7 => col7[i >> u8:3],
      _ => fail!("invalid_column_index", s32:0)
    };
    update(accum, i, val)
  }(s32[COEFF_PER_MCU]:[0, ...])
}

// Full 2D 8×8 IDCT: row pass followed by column pass.
pub fn main(f: s32[COEFF_PER_MCU]) -> s32[COEFF_PER_MCU] {
  idct_cols(idct_rows(f))
}

#[test]
fn idct_row_test() {
  let input = s32[8]:[0x17, 0xffffffff, 0xfffffffe, 0, 0, 0, 0, 0];
  let got = idct_row(input);
  let want = s32[8]:[0x98, 0xa6, 0xba, 0xcb, 0xcf, 0xc7, 0xb9, 0xae];
  assert_eq(want, got)
}

#[test]
fn idct_empty_test() {
  let input = s32[COEFF_PER_MCU]:[23, -1, -2, 0, ...];
  let want = s32[COEFF_PER_MCU]:[
    2, 3, 3, 3, 3, 3, 3, 3,
    2, 3, 3, 3, 3, 3, 3, 3,
    2, 3, 3, 3, 3, 3, 3, 3,
    2, 3, 3, 3, 3, 3, 3, 3,
    2, 3, 3, 3, 3, 3, 3, 3,
    2, 3, 3, 3, 3, 3, 3, 3,
    2, 3, 3, 3, 3, 3, 3, 3,
    2, 3, 3, 3, 3, 3, 3, 3,
  ];
  assert_eq(want, main(input))
}