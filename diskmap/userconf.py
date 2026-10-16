# SPDX-License-Identifier: GPL-3.0-or-later
"""Machine-specific rules, deliberately kept out of the code.

Nothing about one person's disks belongs in a repository. The labels of your
volumes, the names of your own projects, the folder you dump old archives into -
all of that is yours, lives in `~/.diskmap/rules.json`, and is read from there.

What the shipped code carries instead are rules that are true for everyone: a
Steam install is a Steam install, `/build/` is derived output, `comfyui` writes
generated images. Anything that only makes sense on one machine goes in the
file.

The file is optional. Without it diskmap works, just with fewer opinions.
"""
import json, os, sys

PATH = os.path.expanduser("~/.diskmap/rules.json")

TEMPLATE = {
    "_help": {
        "libraries": "Extra image-library rules: {lib, match}. `match` is a "
                     "case-insensitive regex tested against 'relpath/name'. "
                     "Anchor folder names with (^|/) - relpath has no leading "
                     "slash. Libraries you can target: see diskmap/images.py "
                     "LIB_LABEL. These are tried after the built-in origin "
                     "rules and before everything else.",
        "prefer_drop": "Path fragments that mark a scratch or derived copy. When "
                       "a duplicate group has a copy here, this is the one given "
                       "up first.",
        "protected": "Path fragments that must never be offered for removal, "
                     "added to the built-in launcher list.",
        "mounts": "Volume label -> mount point, used by `diskmap import` to "
                  "attach a TSV index to real disks.",
    },
    "libraries": [],
    "prefer_drop": [],
    "protected": [],
    "mounts": {},
}

_cache = None


def load():
    """Read the rules file once. A broken file warns and is then ignored."""
    global _cache
    if _cache is not None:
        return _cache
    _cache = {k: v for k, v in TEMPLATE.items() if not k.startswith("_")}
    if os.path.exists(PATH):
        try:
            with open(PATH, encoding="utf-8") as fh:
                got = json.load(fh)
            for k in _cache:
                if k in got:
                    _cache[k] = got[k]
        except (OSError, ValueError) as e:
            print(f"aviso: {PATH} ignorado ({e})", file=sys.stderr)
    return _cache


def write_template():
    """Create the file with an empty template, so there is something to edit."""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    if not os.path.exists(PATH):
        with open(PATH, "w", encoding="utf-8") as fh:
            json.dump(TEMPLATE, fh, indent=2, ensure_ascii=False)
    return PATH


def libraries():
    """[(lib, regex, None)] in the shape images.ORIGIN/GENERIC use."""
    out = []
    for r in load().get("libraries") or ():
        lib, rx = r.get("lib"), r.get("match")
        if lib and rx:
            out.append((lib, rx, None))
    return out


def prefer_drop():
    return tuple(s.lower() for s in load().get("prefer_drop") or ())


def protected():
    return tuple(s.lower() for s in load().get("protected") or ())


def mounts():
    """label -> mount point. `~` is expanded, so a home-dir volume is portable."""
    return {k: os.path.expanduser(v)
            for k, v in (load().get("mounts") or {}).items() if v}
