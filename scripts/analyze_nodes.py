import csv, glob, collections, numpy as np, os

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
files = sorted(glob.glob(os.path.join(REPO_ROOT, "data/csi/csi_*.csv")))

print("시각    | 행수  | 노드 구성          | 상태")
print("-" * 58)
phase_map = {}
for fp in files:
    label = fp.split("_")[-1].replace(".csv", "")
    node_counts = collections.Counter()
    with open(fp, newline="") as f:
        for row in csv.DictReader(f):
            node_counts[row["node_id"]] += 1
    total = sum(node_counts.values())
    nodes = sorted(node_counts.keys())
    n_nodes = len(nodes)
    counts_str = "  ".join(f"n{n}={node_counts[n]}" for n in nodes)
    phase = f"{n_nodes}노드"
    phase_map[label] = {"nodes": nodes, "counts": dict(node_counts), "total": total}
    print(f"{label}  | {total:5d} | {counts_str:30s} | {phase}")

# 전환 시점
print()
prev_nodes = None
for label in sorted(phase_map.keys()):
    cur = set(phase_map[label]["nodes"])
    if cur != prev_nodes:
        print(f"  >> 전환: {label} 부터 노드={sorted(cur)}")
        prev_nodes = cur
