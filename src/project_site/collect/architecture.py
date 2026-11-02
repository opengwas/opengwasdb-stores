"""The architecture of both repositories, drawn from their claims (#418, #421, epic #416).

Each repository keeps a hand-written `docs/architecture/components.yaml` whose
components claim code paths (`architecture_model` holds the one ownership rule).
This collector reads both through git, without a checkout of either:

- this repository at a ref (default `HEAD`), tracked files only;
- the engine at a ref (default `main`) of a git directory, normally the bare
  clone the site build already keeps. The site's *docs* mirror stays at the
  revision `pixi.lock` pins; the map shows current work, so this reads `main`.

It reports the components of both repositories with their measured size, the
files nobody claims and the claims that match nothing (gaps, reported never
raised), the commit it read in each repository, and the baseline of this
repository's imports of engine modules (#421): how many entries the committed
`docs/architecture/engine-imports-baseline.yaml` holds, and how many engine
imports the code makes. Whether a name is the engine's is decided by the name
alone, so that figure needs no engine model. An engine ref that has no
model yet is not an error: the output says so, with the ref and commit, and
this repository's half is still there.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from project_site.collect import architecture_adrs, architecture_graph, architecture_model as model_rules
from project_site.collect.common import CollectError

ENGINE_REPO = "opengwas/opengwasdb"
REGISTRY_REPO = "opengwas/opengwasdb-stores"
MODEL_PATH = "docs/architecture/components.yaml"
DEFAULT_ENGINE_REF = "main"
DEFAULT_REGISTRY_REF = "HEAD"


class Tree:
    """The files of one repository at one ref, read through git and nothing else."""

    def __init__(self, git_dir: Path, ref: str) -> None:
        self.git = ["git", "-C", str(git_dir)]
        self.ref = ref
        try:
            self.commit = self._run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]).decode().strip()
        except CollectError as error:
            raise CollectError(f"{git_dir} has no ref {ref}: {error}") from error
        # `<mode> <type> <oid>\t<path>` per NUL-terminated record: the object id is how a file is read
        # back, because a path can hold a newline and an id cannot.
        self.oids: dict[str, str] = {}
        for record in model_rules.split_nul(self._run(["ls-tree", "-r", "-z", self.commit]).decode()):
            meta, path = record.split("\t", 1)
            mode, kind, oid = meta.split()
            if kind == "blob":
                self.oids[path] = oid
        self.files = list(self.oids)
        self.committed = self._run(["show", "-s", "--format=%cI", self.commit]).decode().strip()

    def _run(self, args: list[str], stdin: bytes | None = None) -> bytes:
        done = subprocess.run([*self.git, *args], input=stdin, capture_output=True, check=False)
        if done.returncode != 0:
            raise CollectError(f"git {args[0]} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}")
        return done.stdout

    def read(self, paths: list[str]) -> dict[str, str]:
        """Many files at the ref through one `git cat-file --batch`; a path not in the tree is absent.

        Blobs are requested by object id, one `<oid>` per line: a path is never
        put on a line, because a tracked path may contain a newline.
        """
        wanted = [(path, self.oids[path]) for path in paths if path in self.oids]
        if not wanted:
            return {}
        out = self._run(["cat-file", "--batch"], "".join(f"{oid}\n" for _, oid in wanted).encode())
        found: dict[str, str] = {}
        position = 0
        for path, oid in wanted:
            newline = out.index(b"\n", position)
            header = out[position:newline].split()
            if header[0].decode() != oid or header[1] != b"blob":
                raise CollectError(f"git cat-file answered {header!r} for {oid} ({path!r})")
            size = int(header[2])
            found[path] = out[newline + 1 : newline + 1 + size].decode("utf-8", "replace")
            position = newline + 1 + size + 1
        return found


def collect(
    registry_root: Path | None = None,
    *,
    engine_git: Path | None = None,
    engine_ref: str = DEFAULT_ENGINE_REF,
    registry_ref: str = DEFAULT_REGISTRY_REF,
    refresh_engine: bool = False,
) -> dict[str, Any]:
    """Both repositories' components, gaps, import graph and ADRs, as the site's `architecture` source."""
    if registry_root is None or engine_git is None:
        from ogstores import paths

        registry_root = registry_root or paths.REPO_ROOT
        engine_git = engine_git or _default_engine_clone()
    registry = Tree(registry_root, registry_ref)
    refresh = _refresh(engine_git, engine_ref) if refresh_engine else None
    engine = Tree(engine_git, engine_ref)

    registry_text = registry.read([MODEL_PATH]).get(MODEL_PATH)
    if registry_text is None:
        raise CollectError(f"{MODEL_PATH} is not in {REGISTRY_REPO} at {registry.commit[:12]}")
    registry_model = model_rules.load_model(registry_text)
    baseline_path = architecture_graph.BASELINE_PATH
    baseline_text = registry.read([baseline_path]).get(baseline_path)
    baseline = architecture_graph.load_baseline(baseline_text) if baseline_text is not None else None
    engine_text = engine.read([MODEL_PATH]).get(MODEL_PATH)
    engine_model = model_rules.load_model(engine_text) if engine_text is not None else None

    trees = {"registry": registry, "engine": engine}
    components = _components(registry_model, engine_model)
    claims = {repo: model_rules.claims_of(c for c in components if c["repo"] == repo) for repo in trees}
    is_code = {"registry": model_rules.is_registry_code, "engine": model_rules.is_engine_code}
    # Without a model there are no claims to judge the engine's files against:
    # the output says the model is missing, and does not list every file as a gap.
    gaps = {
        repo: model_rules.check_claims(claims[repo], trees[repo].files, is_code[repo])
        if repo == "registry" or engine_model is not None
        else {"unclaimed": [], "stale": []}
        for repo in trees
    }
    _measure(components, trees, claims, is_code)

    # One measurement of the imports, for both the graph's counts and the
    # baseline this repository is held to (#421): they can never disagree.
    imports = architecture_graph.imports_of(trees, claims)
    edges = architecture_graph.import_edges(trees, claims, components, imports)
    found = architecture_graph.engine_imports(imports)
    # The seam's import count is the name-based measurement, so it is the same
    # with or without the engine's model. Every other figure on the engine's half
    # of the graph is resolved through the engine's claims -- what the layers
    # make upward, which component pairs the seam has, how many edges there are
    # in all -- so without the model those are unknown, never 0 (#421 review).
    summary = architecture_graph.summarise(edges) | {"registry_to_engine_imports": len(found)}
    if engine_model is None:
        summary |= {
            key: None
            for key in ("edges", "upward_edges", "upward_imports", "registry_to_engine_edges")
        }
    adrs = architecture_adrs.place(trees, claims, components, architecture_graph.modules_of(trees))
    return {
        "registry": _repository(REGISTRY_REPO, registry, True, None),
        "engine": _repository(
            ENGINE_REPO, engine, engine_model is not None,
            None if engine_model is not None
            else f"{MODEL_PATH} is not in {ENGINE_REPO} at {engine.ref} ({engine.commit[:12]})",
            refresh,
        ),
        "baseline": {
            "path": baseline_path,
            "entries": len(baseline) if baseline is not None else None,
            "measured": len(found),
            "note": None if baseline is not None else (
                f"{baseline_path} is not in {REGISTRY_REPO} at {registry.commit[:12]}"
            ),
        },
        "model": {
            key: registry_model.get(key) or [] for key in ("people", "external", "containers", "container_relations", "stages")
        } | {
            "system": registry_model.get("system") or {},
            "layers": (engine_model or {}).get("layers") or [],
        },
        "components": components,
        "gaps": gaps,
        "edges": edges,
        # Unknown without the engine's model, like `summary.upward_imports`: never an empty list.
        "upward_imports": None if engine_model is None else architecture_graph.upward_imports(imports, components),
        "adrs": adrs,
        "summary": summary
        | {"adrs_total": len(adrs), "adrs_placed": sum(1 for a in adrs if a["components"])},
    }


def _default_engine_clone() -> Path:
    from project_site import paths

    return paths.engine_clone()


def _repository(
    repo: str, tree: Tree, model: bool, note: str | None, refresh: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "repo": repo, "ref": tree.ref, "commit": tree.commit, "committed": tree.committed,
        "refresh": refresh, "model": model, "note": note,
    }


def _refresh(git_dir: Path, ref: str) -> dict[str, Any]:
    """Fetch the engine's `ref` into the clone, and say whether that worked.

    The clone is bare and has no fetch refspec, so the branch is named. A failure
    (offline, no access) is recorded with git's own message and the ref the clone
    already has is read: the page shows both, so a stale `main` is never silent.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(git_dir), "fetch", "--quiet", "origin", f"+refs/heads/{ref}:refs/heads/{ref}"],
            capture_output=True, text=True, check=False, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    if done.returncode != 0:
        return {"ok": False, "error": done.stderr.strip() or f"git fetch exited {done.returncode}"}
    return {"ok": True, "error": None}


def _components(registry_model: Mapping[str, Any], engine_model: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The registry's components, then the engine's, each tagged with its repository."""
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for repo, source in (("registry", registry_model), ("engine", engine_model)):
        for raw in (source or {}).get("components") or []:
            if raw["id"] in seen:
                raise CollectError(f"component id {raw['id']!r} is in both repositories' models")
            seen.add(raw["id"])
            components.append(
                {
                    "id": raw["id"],
                    "name": raw["name"],
                    "repo": repo,
                    "container": raw.get("container"),
                    "layer": raw.get("layer"),
                    "stages": list(raw.get("stages") or []),
                    "summary": raw.get("summary") or "",
                    "paths": list(raw.get("paths") or []),
                    "files": 0,
                    "lines": 0,
                    "tracked": 0,
                }
            )
    return components


def _measure(components: list[dict[str, Any]], trees: Mapping[str, Tree], claims: Mapping[str, Any], is_code: Mapping[str, Any]) -> None:
    """Code files, their lines, and every tracked file each component owns."""
    by_id = {c["id"]: c for c in components}
    for repo, tree in trees.items():
        code = [f for f in tree.files if is_code[repo](f)]
        text = tree.read(code)
        for path in tree.files:
            cid = model_rules.owner(claims[repo], path)
            if cid:
                by_id[cid]["tracked"] += 1
        for path in code:
            cid = model_rules.owner(claims[repo], path)
            if cid:
                by_id[cid]["files"] += 1
                if path not in text:
                    raise CollectError(f"{path} is a code file of {repo} but git could not read it")
                by_id[cid]["lines"] += len(text[path].splitlines())
