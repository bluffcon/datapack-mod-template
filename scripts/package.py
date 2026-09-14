#!/usr/bin/env python3
"""Stamp template metadata from mod.json and build zip + jar artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path


SKIP_NAMES = {
    ".git",
    ".github",
    ".gitignore",
    "mod.json",
    "dist",
    "working_dir",
}
SKIP_PREFIXES = (".git",)

# Coax version token for pack content (pack.mcmeta, .mcfunction, JSON, etc.).
# Discrete on purpose so normal words are never rewritten.
VER_PLACEHOLDER = "[×VER×]"

# Only rewrite these; never touch binary assets (.png, .nbt, .ogg, …).
TEXT_SUFFIXES = {
    ".mcmeta",
    ".mcfunction",
    ".json",
    ".txt",
    ".snbt",
    ".toml",
    ".properties",
    ".lang",
    ".md",
}


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"missing {path}")
    except json.JSONDecodeError as exc:
        die(f"invalid JSON in {path}: {exc}")
    return {}


COAX_RE = re.compile(r"^([abhrABHR])(\d+)$")


def parse_commit(message: str) -> tuple[str, str]:
    """'!r7 changes' -> ('r7', 'changes'). First line only."""
    first = (message or "").splitlines()[0].strip()
    match = re.match(r"^!\s*(\S+)(?:\s+(.*))?$", first)
    if not match:
        die(
            "commit title must look like '!r7 changes' "
            f"(got {first!r})"
        )
    version = match.group(1)
    title = (match.group(2) or version).strip()
    if not version:
        die("empty version in commit title")
    return version, title


def coax_to_semver(coax: str) -> tuple[str, int, str]:
    """r7 -> ('7.0.0', 7, 'r'). a7 -> ('7.0.0-alpha', 7, 'a')."""
    match = COAX_RE.match(coax.strip())
    if not match:
        die(
            f"version {coax!r} is not coax (expected a12 / b3 / r7 / h8). "
            "See https://github.com/bluffcon/coax-versioning"
        )
    tag = match.group(1).lower()
    number = int(match.group(2))
    suffix = { "a": "-alpha", "b": "-beta", "r": "", "h": "" }[tag]
    return f"{number}.0.0{suffix}", number, tag


def fabric_lib_range(min_version: int) -> str:
    return f">={min_version}.0.0"


def neoforge_lib_range(min_version: int) -> str:
    return f"[{min_version}.0.0,)"


def replace_all(text: str, mapping: dict[str, str]) -> str:
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = re.findall(r"\{\{[a-z0-9_]+\}\}", text)
    if leftover:
        die(f"unreplaced placeholders: {', '.join(sorted(set(leftover)))}")
    return text


def should_skip(name: str) -> bool:
    if name in SKIP_NAMES:
        return True
    return any(name.startswith(p) for p in SKIP_PREFIXES)


def zip_paths(out: Path, root: Path, include: list[str]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in include:
            src = root / rel
            if not src.exists():
                continue
            if src.is_file():
                zf.write(src, rel)
                continue
            for file in src.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(root).as_posix())


def write_github_output(**kwargs: str) -> None:
    dest = os.environ.get("GITHUB_OUTPUT")
    if not dest:
        return
    with open(dest, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            if "\n" in value:
                fh.write(f"{key}<<EOF\n{value}\nEOF\n")
            else:
                fh.write(f"{key}={value}\n")


def workspace_rel(path: Path) -> str:
    """Path relative to GITHUB_WORKSPACE so Docker-based actions can open it."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(workspace).as_posix()
    except ValueError:
        return resolved.as_posix()


def fabric_depends(
    mod: dict, library: dict | None, pin_mc: bool, lib_min: int | None
) -> dict:
    depends = {"fabricloader": "*"}
    if pin_mc:
        mc = (
            mod.get("loaders", {}).get("fabric", {}).get("minecraft")
            or None
        )
        if mc:
            depends["minecraft"] = mc
    if library:
        depends[library["id"]] = (
            fabric_lib_range(lib_min) if lib_min is not None else "*"
        )
    return depends


def neoforge_deps(
    mod: dict, library: dict | None, pin_mc: bool, lib_min: int | None
) -> str:
    blocks: list[str] = []
    if pin_mc:
        rng = (
            mod.get("loaders", {}).get("neoforge", {}).get("minecraft")
            or "[,)"
        )
        blocks.append(
            "\n[[dependencies.{id}]]\n"
            'modId = "minecraft"\n'
            'type = "required"\n'
            f'versionRange = "{rng}"\n'
            'ordering = "NONE"\n'
            'side = "BOTH"\n'.format(id=mod["id"])
        )
    if library:
        rng = neoforge_lib_range(lib_min) if lib_min is not None else "[,)"
        blocks.append(
            "\n[[dependencies.{id}]]\n"
            'modId = "{lib}"\n'
            'type = "required"\n'
            f'versionRange = "{rng}"\n'
            'ordering = "NONE"\n'
            'side = "BOTH"\n'.format(id=mod["id"], lib=library["id"])
        )
    return "".join(blocks)


def main() -> None:
    content = Path(os.environ.get("CONTENT_DIR", ".")).resolve()
    template = Path(os.environ.get("TEMPLATE_DIR", "template")).resolve()
    dist = Path(os.environ.get("DIST_DIR", "dist")).resolve()

    mod_path = content / "mod.json"
    mod = load_json(mod_path)

    for required in ("id", "name"):
        if not mod.get(required):
            die(f"mod.json missing {required}")

    commit_msg = os.environ.get("COMMIT_MESSAGE", "")
    version_override = os.environ.get("VERSION_OVERRIDE", "").strip()
    if version_override:
        pack_version = version_override.lstrip("v")
        title = os.environ.get("RELEASE_TITLE", pack_version).strip() or pack_version
    else:
        pack_version, title = parse_commit(commit_msg)

    mod_version, coax_number, coax_tag = coax_to_semver(pack_version)

    main_mod = bool(mod.get("main_mod", False))
    library = None
    lib_min: int | None = None
    if not main_mod:
        library = mod.get("library") or {}
        if not library.get("id") or not library.get("modrinth_project_id"):
            die("non-main packs need library.id and library.modrinth_project_id")
        if library.get("min_version") is not None:
            lib_min = int(library["min_version"])
        else:
            lib_min = coax_number

    pin_mc = bool(mod.get("pin_minecraft_in_mod", False))
    pack = mod.get("pack") or {}
    rp = mod.get("resource_pack") or {}
    mr = mod.get("modrinth") or {}
    if not mr.get("project_id"):
        die("mod.json.modrinth.project_id is required")

    authors = mod.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]

    icon = mod.get("icon") or "pack.png"
    # Stamped into fabric.mod.json / neoforge.mods.toml only.
    # pack.mcmeta is the content-repo file with XXX → pack_version (coax).
    mapping = {
        "id": mod["id"],
        "name": mod["name"],
        "version": mod_version,
        "description": mod.get("description") or mod["name"],
        "license": mod.get("license") or "All Rights Reserved",
        "homepage": mod.get("homepage") or "",
        "sources": mod.get("sources") or "",
        "issues": mod.get("issues") or "",
        "icon": icon,
        "authors_json": json.dumps(authors),
        "authors_csv": ", ".join(authors),
        "fabric_depends": json.dumps(
            fabric_depends(mod, library, pin_mc, lib_min), indent=2
        ),
        "neoforge_dependencies": neoforge_deps(
            mod, library, pin_mc, lib_min
        ),
    }

    stage = Path(os.environ.get("STAGE_DIR", "stage")).resolve()
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Content files
    for item in content.iterdir():
        if should_skip(item.name):
            continue
        dest = stage / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    # Stamped loader metadata (overwrite any content copies)
    fabric_src = (template / "fabric.mod.json").read_text(encoding="utf-8")
    (stage / "fabric.mod.json").write_text(
        replace_all(fabric_src, mapping), encoding="utf-8"
    )

    toml_src = (template / "META-INF" / "neoforge.mods.toml").read_text(
        encoding="utf-8"
    )
    meta_inf = stage / "META-INF"
    meta_inf.mkdir(exist_ok=True)
    (meta_inf / "neoforge.mods.toml").write_text(
        replace_all(toml_src, mapping), encoding="utf-8"
    )

    # Content-repo pack.mcmeta is kept as-is (fancy description, formats).
    # Inject coax version into every text file that uses the placeholder so
    # datapack code can read its own version (tellraw, storage, scoreboard, …).
    mcmeta_path = stage / "pack.mcmeta"
    if not mcmeta_path.is_file():
        die("content repo is missing pack.mcmeta")

    replaced_files = 0
    for path in stage.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        # Loader metadata is stamped separately; don't rewrite those templates.
        rel = path.relative_to(stage).as_posix()
        if rel in ("fabric.mod.json",) or rel.startswith("META-INF/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if VER_PLACEHOLDER not in text:
            continue
        path.write_text(
            text.replace(VER_PLACEHOLDER, pack_version), encoding="utf-8"
        )
        replaced_files += 1
        print(f"version inject: {rel}")
    if replaced_files == 0:
        print(
            f"warning: no {VER_PLACEHOLDER} found under content text files; "
            "pack version was not injected anywhere"
        )

    dist.mkdir(parents=True, exist_ok=True)
    base = f"{mod['id']}_{pack_version}"
    datapack_zip = dist / f"{base}.zip"
    rp_zip = dist / f"{base}-resources.zip"
    jar = dist / f"{base}.jar"

    zip_paths(
        datapack_zip,
        stage,
        ["pack.mcmeta", "pack.png", icon, "data"],
    )

    has_assets = (stage / "assets").exists()
    rp_present = bool(rp.get("present", has_assets))
    if rp_present and has_assets:
        zip_paths(rp_zip, stage, ["pack.mcmeta", "pack.png", icon, "assets"])
    else:
        rp_zip = None

    # jar = full pack + loader metadata
    jar_tmp = dist / f"{base}.jar.zip"
    include = [
        "pack.mcmeta",
        "pack.png",
        icon,
        "data",
        "assets",
        "fabric.mod.json",
        "META-INF",
    ]
    zip_paths(jar_tmp, stage, include)
    jar_tmp.replace(jar)

    game_versions = mr.get("game_versions") or ["26.1.x"]
    if isinstance(game_versions, str):
        game_versions = [game_versions]

    deps = []
    if library:
        deps.append(
            {
                "project_id": library["modrinth_project_id"],
                "dependency_type": "required",
            }
        )
    extra = mod.get("dependencies") or []
    for dep in extra:
        pid = dep.get("modrinth_project_id") or dep.get("project_id")
        if pid:
            deps.append(
                {
                    "project_id": pid,
                    "dependency_type": dep.get("type") or "required",
                }
            )

    rp_required = bool(rp.get("required", True))
    file_types = ""
    # Relative paths only — modrinth-publish runs in Docker and cannot see
    # host absolute paths like /home/runner/work/.../dist/...
    files_datapack = workspace_rel(datapack_zip)
    if rp_zip:
        files_datapack += f"\n{workspace_rel(rp_zip)}"
        kind = (
            "required-resource-pack"
            if rp_required
            else "optional-resource-pack"
        )
        file_types = f"{rp_zip.name}={kind}"

    loaders_pack = ["datapack"]
    if main_mod:
        loaders_pack = ["datapack", "minecraft"]

    tag_to_channel = {"a": "alpha", "b": "beta", "r": "release", "h": "release"}
    channel = mr.get("channel") or tag_to_channel.get(coax_tag, "release")

    write_github_output(
        version=pack_version,
        mod_version=mod_version,
        title=title,
        id=mod["id"],
        name=mod["name"],
        project_id=mr["project_id"],
        channel=channel,
        environment=mr.get("environment") or "",
        game_versions="\n".join(game_versions),
        datapack_zip=workspace_rel(datapack_zip),
        resource_zip=workspace_rel(rp_zip) if rp_zip else "",
        jar=workspace_rel(jar),
        files_datapack=files_datapack,
        file_types=file_types,
        primary_datapack=datapack_zip.name,
        loaders_datapack=json.dumps(loaders_pack),
        loaders_mod=json.dumps(["fabric", "neoforge"]),
        dependencies=json.dumps(deps),
        main_mod="true" if main_mod else "false",
        jar_version=f"{mod_version}+mod",
    )
    print(f"version={pack_version}")
    print(f"mod_version={mod_version}")
    print(f"title={title}")
    print(f"main_mod={main_mod}")
    print(f"wrote {datapack_zip}")
    if rp_zip:
        print(f"wrote {rp_zip}")
    print(f"wrote {jar}")


if __name__ == "__main__":
    main()
