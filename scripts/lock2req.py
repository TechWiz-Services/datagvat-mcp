"""Convert uv.lock into a hash-pinned requirements.txt (runtime deps only).

Usage: python scripts/lock2req.py app/uv.lock > requirements.txt

Walks the dependency graph from the root project (without the 'dev' extra),
carries environment markers along each path, and emits every reachable
package as name==version with all sha256 hashes from the lock, so that
`pip install --require-hashes` installs exactly what the lockfile pins.
"""

import sys
import tomllib
from collections import defaultdict

ROOT = "datagvat-mcp"


def main(lock_path: str) -> None:
    with open(lock_path, "rb") as f:
        lock = tomllib.load(f)

    pkgs = {p["name"]: p for p in lock["package"]}
    # node -> set of marker strings ("" = unconditional) under which it is needed
    needed: dict[str, set[str]] = defaultdict(set)

    def combine(a: str, b: str) -> str:
        if not a:
            return b
        if not b:
            return a
        return f"({a}) and ({b})"

    def visit(name: str, marker: str, extras: tuple[str, ...]) -> None:
        key = (name, marker, extras)
        if key in seen:
            return
        seen.add(key)
        pkg = pkgs[name]
        if name != ROOT:
            needed[name].add(marker)
        edges = list(pkg.get("dependencies", []))
        for extra in extras:
            edges += pkg.get("optional-dependencies", {}).get(extra, [])
        for dep in edges:
            visit(dep["name"], combine(marker, dep.get("marker", "")), tuple(sorted(dep.get("extra", []))))

    seen: set = set()
    visit(ROOT, "", ())

    out = []
    for name in sorted(needed):
        pkg = pkgs[name]
        markers = needed[name]
        marker = "" if "" in markers else " or ".join(f"({m})" for m in sorted(markers))
        hashes = [w["hash"] for w in pkg.get("wheels", [])]
        if "sdist" in pkg:
            hashes.append(pkg["sdist"]["hash"])
        line = f"{name}=={pkg['version']}"
        if marker:
            line += f" ; {marker}"
        line += "".join(f" \\\n    --hash={h}" for h in hashes)
        out.append(line)
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
