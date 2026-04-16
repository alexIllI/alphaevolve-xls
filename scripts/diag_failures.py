import sqlite3

conn = sqlite3.connect('results/matmul_exp_001/candidates_db.sqlite')
pragma = conn.execute('PRAGMA table_info(candidates)').fetchall()
col_names = [p[1] for p in pragma]

rows = conn.execute('SELECT * FROM candidates ORDER BY id').fetchall()
for row in rows:
    d = dict(zip(col_names, row))
    print(f"=== iter={d['iteration']} island={d['island_id']} status={d['build_status']} ===")
    print(f"notes: {d.get('notes', '')}")
    code = d.get('generated_code', '') or ''
    print(f"generated_code ({len(code)} chars):")
    print(code[:1000])
    print()

conn.close()
