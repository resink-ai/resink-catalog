"""List the licences of everything the Local Catalog ships or needs at runtime (for the L4 compatibility check).

uv run python scripts/licenses.py            # Python runtime dependencies
node scripts/licenses.mjs                    # what ends up in the web bundle (needs web/node_modules)
"""

import re
from importlib import metadata

ROOT = "resink-catalog"
# pulled in by the pyiceberg extras `sql-sqlite` and `pyarrow` (the extras are not followed automatically)
EXTRAS = ["pyarrow", "sqlalchemy", "greenlet"]


def licence(dist: metadata.Distribution) -> str:
    md = dist.metadata
    if md.get("License-Expression"):
        return md["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    text = (md.get("License") or "").strip().splitlines()
    return text[0][:60] if text else "UNKNOWN"


def closure(root: str) -> dict[str, metadata.Distribution]:
    seen: dict[str, metadata.Distribution] = {}
    todo = [root, *EXTRAS]
    while todo:
        name = re.split(r"[ ;<>=!~\[(]", todo.pop().lower(), maxsplit=1)[0].replace("_", "-")
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue  # an optional extra that is not installed
        seen[name] = dist
        for req in dist.requires or []:
            if "extra ==" not in req:
                todo.append(req)
    return seen


if __name__ == "__main__":
    deps = closure(ROOT)
    for name, dist in sorted(deps.items()):
        print(f"{name:32} {dist.version:12} {licence(dist)}")
