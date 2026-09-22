"""保存済みthroughput比較CSVから1KB LTM Get Hitのみ描画する。"""
import csv
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/vmemkv-matplotlib")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
plt.rcParams.update({
    "font.family": "serif", "font.serif": ["DejaVu Serif"], "font.size": 9,
    "axes.labelsize": 9, "legend.fontsize": 9,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.linewidth": .6, "xtick.major.width": .6, "ytick.major.width": .6,
    "pdf.fonttype": 42, "ps.fonttype": 42,
})
with (ROOT / "20260922_get_throughput_regression.csv").open() as f:
    rows = {(int(d["threads"]), d["distribution"]): d for d in csv.DictReader(f)
            if d["phase"] == "initial" and d["scenario"] == "ltm"
            and d["value_bytes"] == "1024" and d["operation"] == "Get-Hit"}

fig, axes = plt.subplots(1, 2, figsize=(7, 2.8))
fig.subplots_adjust(left=.09, right=.985, bottom=.23, top=.83, wspace=.32)

for ax, threads, ymax, panel in zip(axes, (1, 16), (50, 900), ("a", "b")):
    ax.set_ylim(0, ymax)
    ax.set_ylabel("Throughput (kops/s)")
    ax.set_xticks([0, 1], ["Uniform", "Zipf"])
    ax.set_xlabel(f"({panel}) {threads} thread{'s' if threads > 1 else ''}", labelpad=7)
    ax.set_xlim(-.55, 1.55)
    ax.tick_params(direction="out", length=3)
    for i, (key, label, color, hatch, offset) in enumerate((
        ("baseline_ops_s", "Baseline", "white", "///", -.18),
        ("proposal1_ops_s", "Cross-page pread", "0.55", None, .18),
    )):
        values = [float(rows[(threads, d)][key]) / 1000 for d in ("Uniform", "Zipf")]
        ax.bar([x + offset for x in (0, 1)], values, width=.32,
               label=label, color=color, edgecolor="black", linewidth=.6, hatch=hatch)

handles, labels = axes[0].get_legend_handles_labels()
fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(.53, 1),
           frameon=False, ncol=2, handlelength=1.8, columnspacing=2)
for ext in ("png", "svg", "pdf"):
    fig.savefig(ROOT / f"20260922_1kb_ltm_get_hit_throughput.{ext}", dpi=300,
                facecolor="white", bbox_inches="tight", pad_inches=.04)
