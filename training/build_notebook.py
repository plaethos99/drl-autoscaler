"""Convert drl_autoscaler_training.py (# %% cells) into a Jupyter/Kaggle notebook."""
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "drl_autoscaler_training.py")
dst = os.path.join(here, "drl_autoscaler_training.ipynb")

cells, cur, kind = [], [], None


def flush():
    if kind is None:
        return
    lines = cur[:]
    while lines and not lines[-1].strip():
        lines.pop()
    if kind == "markdown":
        lines = [l[2:] if l.startswith("# ") else l.lstrip("#") for l in lines]
    text = [l + "\n" for l in lines]
    if text:
        text[-1] = text[-1].rstrip("\n")
    cell = {"cell_type": kind, "metadata": {}, "source": text}
    if kind == "code":
        cell.update(execution_count=None, outputs=[])
    cells.append(cell)


for line in open(src, encoding="utf-8").read().splitlines():
    if line.startswith("# %%"):
        flush()
        kind, cur = ("markdown" if "[markdown]" in line else "code"), []
    else:
        cur.append(line)
flush()

nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                                   "language_info": {"name": "python"}},
      "nbformat": 4, "nbformat_minor": 5}
json.dump(nb, open(dst, "w", encoding="utf-8"), indent=1)
print(f"wrote {dst} ({len(cells)} cells)")
