#!/usr/bin/env python3
"""intent: one shared layer that runs a mistaken or wishful CLI form as the form it meant, and says so.

Leo, 2026-11-23: "we can keep hill climbing cli, not just for humans (well, tbh bc they're now the minority these
days) but for agents... esp for those that expect different things for some reason, or fuck up commands, or do too
much wishful thinking. we need to have really powerful aliasing for intent." And 2026-11-20: "we need a bunch of
synonyms for code for you guys where it just goes 'eh close enough' and works".

A CLI takes it with one call before it parses (nothing else changes; a form that parses costs nothing extra):

    import intent; intent.install()          # an argparse CLI: every parser of this process
    argv = intent.words(argv, "lane-gh", subcommands=[...], flags={...})   # a hand-rolled one: the same table

What it does when argparse refuses the argv, in this order, each edit kept only if the whole argv then parses:
  the table      a form agents were measured to type (intent_aliases.json, generated from tools/cli_misses.py counts)
  synonyms       status, show, info; ls, list; --reason, --why (the table's groups), when one member is a real choice
  spelling       --dry_run, -json, a unique prefix, the one close match (difflib)
  glued, split   `land-status` for `land status`, `--status` for the subcommand `status`
  placement      a parent's flag typed after the subcommand (or the reverse) is moved to where its parser reads it
  order          positionals in the one order that parses; two swapped subcommand words
  missing verb   the one subcommand under which the rest parses (or the table's default for that CLI)
One meaning: it runs that form and prints `PROG: ran `...` for `...``. on stderr. Two or three: it prints them as
runnable commands and exits 2 as before. Destructive or outward forms (the table's "unsafe" words: delete, push,
send, merge, --force, ...) are never rewritten: the candidate is printed as "exact form needed" and nothing runs.

Every outcome is one line in ~/.local/state/intent/log.jsonl (ran, candidates, exact, none): the weekly re-mine
(tools/intent_table.py) reads it beside the transcripts. The Rust twin (cmux, acpmux) reads the same table and
passes the same vectors (tools/intent_vectors.json). Detector: tools/test_intent.py.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import itertools
import json
import os
import shlex
import sys
import time

CUTOFF = 0.75  # as close_flags: `--wiat`/`--wait` 0.75; `--days`/`--wait` 0.5 never matches
MAX_STATES = 40  # argvs tried for one refusal
MAX_EDITS = 4  # edits composed into one repair
MAX_SHOWN = 3
TABLE_FILE = "intent_aliases.json"
LOG = os.path.expanduser("~/.local/state/intent/log.jsonl")
LOG_MAX = 20 << 20

_table_cache: dict | None = None


def table() -> dict:
    """The alias table beside this file (or $INTENT_TABLE): synonyms, flag_synonyms, unsafe, cli."""
    global _table_cache
    if _table_cache is None:
        path = os.environ.get("INTENT_TABLE") or os.path.join(os.path.dirname(os.path.realpath(__file__)), TABLE_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                _table_cache = json.load(fh)
        except (OSError, ValueError):
            _table_cache = {}
    return _table_cache


def _group_mates(word: str, groups: list) -> list:
    return [m for g in groups if word in g for m in g if m != word]


def unsafe_word(tok: str, tbl: dict | None = None) -> bool:
    """Whether TOK (a subcommand or flag) is a destructive or outward word: any of its -/_ parts is in the table's
    unsafe list."""
    words = set((tbl if tbl is not None else table()).get("unsafe") or ())
    name = tok.split("=", 1)[0].lstrip("-").lower()
    return bool(name) and (name in words or any(p in words for p in name.replace("_", "-").split("-")))


def _close(word: str, known: list) -> list:
    return difflib.get_close_matches(word, known, n=3, cutoff=CUTOFF)


def _norm_flag(name: str) -> list:
    """Spellings NAME may have meant: --dry_run and --dryRun as --dry-run, -json as --json, --Json as --json."""
    body = name.lstrip("-")
    out = []
    for cand in (body.replace("_", "-"), "".join("-" + c.lower() if c.isupper() else c for c in body).lstrip("-"),
                 body.lower()):
        for dashes in ("--", "-"):
            if dashes + cand != name and dashes + cand not in out:
                out.append(dashes + cand)
    return out


# ---- argparse: reading a parser -------------------------------------------------------------------------------

def _sub_action(parser):
    for a in parser._actions:  # noqa: SLF001 (argparse keeps no public list)
        if isinstance(a, argparse._SubParsersAction):  # noqa: SLF001
            return a
    return None


def _options(parser) -> dict:
    return {s: a for a in parser._actions for s in a.option_strings}  # noqa: SLF001


def _takes_value(action) -> bool:
    return action.nargs != 0 and not isinstance(action, (
        argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._StoreConstAction,  # noqa: SLF001
        argparse._CountAction, argparse._HelpAction, argparse._VersionAction, argparse._AppendConstAction))  # noqa: SLF001


def _lookup(name: str, known: dict):
    """The action argparse would read NAME as: exact, or a long option's unique prefix."""
    if name in known:
        return known[name]
    if name.startswith("--"):
        hits = {id(a): a for k, a in known.items() if k.startswith(name)}
        if len(hits) == 1:
            return next(iter(hits.values()))
    elif len(name) > 2 and name[:2] in known:  # -n5, -abc
        return known[name[:2]]
    return None


def _number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _walk(parser, argv: list) -> dict:
    """ARGV read against PARSER's tree. levels: [{parser, start, sub}] (sub: the index of the subcommand word chosen
    at that level, None at the last); bad_sub: the index of a word in a subcommand's place that is none; unknown:
    [(index, level)] of flags no parser on the way knows there; positionals: indexes of plain words."""
    levels = [{"parser": parser, "start": 0, "sub": None}]
    unknown, positionals, bad_sub = [], [], None
    cur, known = parser, _options(parser)
    sub = _sub_action(cur)
    before = _positionals_before(cur, sub)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            positionals.extend(range(i + 1, len(argv)))
            break
        if tok.startswith("-") and len(tok) > 1 and not _number(tok):
            name, eq, _ = tok.partition("=")
            action = _lookup(name, known)
            if action is None and bad_sub is not None:
                pass  # after a word in a subcommand's place: whose flag it is cannot be read yet
            elif action is None:
                unknown.append((i, len(levels) - 1))
            elif not eq and _takes_value(action) and not (len(name) > 2 and not name.startswith("--")):
                i += 1
            i += 1
            continue
        if sub is not None and bad_sub is None:
            if before > 0:
                before -= 1
                positionals.append(i)
            elif tok in sub.choices:
                levels[-1]["sub"] = i
                cur = sub.choices[tok]
                known = _options(cur)
                levels.append({"parser": cur, "start": i + 1, "sub": None})
                sub = _sub_action(cur)
                before = _positionals_before(cur, sub)
            else:
                bad_sub = i
                positionals.append(i)
        else:
            positionals.append(i)
        i += 1
    return {"levels": levels, "bad_sub": bad_sub, "unknown": unknown, "positionals": positionals}


def _positionals_before(parser, sub) -> int:
    if sub is None:
        return 0
    n = 0
    for a in parser._actions:  # noqa: SLF001
        if a is sub:
            break
        if not a.option_strings and isinstance(a.nargs, (int, type(None))):
            n += a.nargs or 1
    return n


class _Fail(Exception):
    def __init__(self, message: str, parser=None):
        super().__init__(message)
        self.message, self.parser = message, parser


class _Stop(Exception):
    """A trial that reached parser.exit(): help or version (status 0) parses; anything else is a refusal."""

    def __init__(self, status):
        super().__init__(str(status))
        self.status = status


class _Rerun(Exception):
    def __init__(self, argv: list):
        super().__init__("rerun")
        self.argv = argv


_state = {"depth": 0, "trial": False, "top": None, "argv": None, "prog": None, "installed": False, "done": False}
_orig: dict = {}


def _writes_files(parser, seen=None) -> bool:
    """Whether a trial parse could write: an argparse.FileType opened for writing anywhere in the tree."""
    seen = seen if seen is not None else set()
    if id(parser) in seen:
        return False
    seen.add(id(parser))
    for a in parser._actions:  # noqa: SLF001
        mode = getattr(a.type, "_mode", "") if isinstance(a.type, argparse.FileType) else ""
        if any(c in mode for c in "wax+"):
            return True
        if isinstance(a, argparse._SubParsersAction) and any(  # noqa: SLF001
                _writes_files(p, seen) for p in a.choices.values()):
            return True
    return False


def _try(parser, argv: list) -> str | None:
    """None when PARSER takes ARGV, else argparse's refusal. Nothing is printed and nothing exits."""
    was = _state["trial"]
    _state["trial"] = True
    cls = argparse.ArgumentParser
    held = None if _state["installed"] else (cls.error, cls.exit)
    if held:  # not installed (fix(), repair() called directly): the trial's own error and exit, for this call only
        cls.error, cls.exit = _error, _exit
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            _orig.get("parse_args", argparse.ArgumentParser.parse_args)(parser, list(argv))
        return None
    except _Fail as fail:
        return fail.message
    except _Stop as stop:
        return None if not stop.status else f"exit {stop.status}"
    except SystemExit as stop:
        return None if not stop.code else f"exit {stop.code}"
    except Exception as error:  # noqa: BLE001 (a type= callable of the tool's own: its refusal is a refusal)
        return f"{type(error).__name__}: {error}"
    finally:
        _state["trial"] = was
        if held:
            cls.error, cls.exit = held


# ---- the edits ----------------------------------------------------------------------------------------------

def _flag_edits(parser, argv: list, walk: dict, tbl: dict) -> list:
    """Edits for the first flag no parser on the way knows: [(class, new_argv, note)], best class first."""
    i, level = walk["unknown"][0]
    levels = walk["levels"]
    name, eq, value = argv[i].partition("=")
    here = _options(levels[level]["parser"])
    out = []

    def put(cls, new_name, where=level):
        action = _options(levels[where]["parser"])[new_name]
        take = 2 if (not eq and _takes_value(action) and i + 1 < len(argv)) else 1
        chunk = [new_name + eq + value] + argv[i + 1:i + take]
        rest = argv[:i] + argv[i + take:]
        if where == level:
            new = argv[:i] + chunk + argv[i + take:]
            note = f"{new_name} for {name}"
        elif where < level:  # an ancestor's flag: before that level's subcommand word
            at = levels[where]["sub"]
            new = rest[:at] + chunk + rest[at:]
            note = f"{new_name} moved before `{argv[at]}`" + ("" if new_name == name else f" (for {name})")
        else:  # the chosen subcommand's flag typed before it: after the subcommand word
            at = levels[where]["start"] - take
            new = rest[:at] + chunk + rest[at:]
            note = f"{new_name} moved after `{argv[levels[where]['start'] - 1]}`" + (
                "" if new_name == name else f" (for {name})")
        out.append((cls, new, note))

    def spellings(known: dict) -> list:
        """(class, option) NAME may mean among KNOWN, best class first, one class only."""
        norm = [c for c in _norm_flag(name) if _lookup(c, known) is not None and c in known]
        if norm:
            return [("spelling", norm[0])]
        syn = [m for m in _group_mates(name, tbl.get("flag_synonyms") or []) if m in known]
        if syn:
            return [("synonym", m) for m in dict.fromkeys(syn)]
        return [("close", m) for m in _close(name, [k for k in known if k.startswith("--") == name.startswith("--")])]

    for cls, opt in spellings(here):
        put(cls, opt)
    if not out:  # a flag another parser on the way owns, as typed or respelled
        for where in list(range(level - 1, -1, -1)) + list(range(level + 1, len(levels))):
            known = _options(levels[where]["parser"])
            if name in known:
                put("placement", name, where)
            else:
                for cls, opt in spellings(known)[:1]:
                    if cls != "close":
                        put("placement", opt, where)
            if out:
                break
    sub = _sub_action(levels[level]["parser"])
    if not out and sub is not None and not eq and name.lstrip("-") in sub.choices and level == len(levels) - 1:
        word = name.lstrip("-")  # `land --status X`: the flag names the subcommand
        out.append(("glued", argv[:i] + [word] + argv[i + 1:], f"subcommand `{word}` for {name}"))
    return out


def _sub_edits(parser, argv: list, walk: dict, tbl: dict, prog: str) -> list:
    """Edits for a word in a subcommand's place that is no subcommand."""
    i = walk["bad_sub"]
    tok = argv[i]
    level = walk["levels"][-1]
    sub = _sub_action(level["parser"])
    choices = list(sub.choices)
    out = []

    def swap(cls, words: list, note: str, take: int = 1):
        out.append((cls, argv[:i] + words + argv[i + take:], note))

    path = [argv[lv["sub"]] for lv in walk["levels"][:-1]]
    for rule in (tbl.get("cli") or {}).get(prog, {}).get("aliases") or []:
        frm, to = rule.get("from") or [], rule.get("to") or []
        if frm and (rule.get("under") or []) == path and argv[i:i + len(frm)] == frm:
            swap("table", list(to), f"`{' '.join(to)}` for `{' '.join(frm)}`", len(frm))
    if out:
        return out
    syn = [m for m in _group_mates(tok, tbl.get("synonyms") or []) if m in choices]
    for m in dict.fromkeys(syn):
        swap("synonym", [m], f"`{m}` for `{tok}`")
    if out:
        return out
    for sep in ("-", "_", ":", "/", "."):  # `land-status` for `land status`
        head, _, tail = tok.partition(sep)
        if tail and head in choices:
            inner = _sub_action(sub.choices[head])
            if inner is not None and tail in inner.choices:
                swap("glued", [head, tail], f"`{head} {tail}` for `{tok}`")
        if tail and tail in choices:
            inner = _sub_action(sub.choices[tail])
            if inner is not None and head in inner.choices:
                swap("glued", [tail, head], f"`{tail} {head}` for `{tok}`")
    if out:
        return out
    prefixed = [c for c in choices if c.startswith(tok)]
    if len(prefixed) == 1:
        swap("spelling", prefixed, f"`{prefixed[0]}` for `{tok}`")
        return out
    for m in _close(tok, choices):
        swap("close", [m], f"`{m}` for `{tok}`")
    if out:
        return out
    for j in walk["positionals"]:  # a subcommand word typed later: `REPO status`, `pr status` for `status pr`
        if j > i and argv[j] in choices:
            moved = argv[:i] + [argv[j]] + argv[i:j] + argv[j + 1:]
            out.append(("order", moved, f"`{argv[j]}` moved before `{tok}`"))
            break
    default = (tbl.get("cli") or {}).get(prog, {}).get("default")
    if isinstance(default, dict):
        default = default.get(" ".join(path))
    if default and default in choices:
        out.append(("missing verb", argv[:i] + [default] + argv[i:], f"`{default}` (the default verb)"))
    elif not out:
        part = (tbl.get("cli") or {}).get(prog, {})
        for c in choices:  # the one reading subcommand under which the rest parses: a write's verb is never supplied
            if c in (tbl.get("reads") or []) + (part.get("safe") or []) and not _exact_path(prog, path + [c], tbl):
                out.append(("missing verb", argv[:i] + [c] + argv[i:], f"`{c}` (the one verb that takes the rest)"))
    return out


def _message_edits(parser, argv: list, walk: dict, message: str, tbl: dict) -> list:
    """Edits for a refusal the argv's shape does not show: a value outside its choices, positionals out of order."""
    out = []
    if "invalid choice: " in message:
        bad = message.split("invalid choice: ", 1)[1]
        tok = bad.split(" (choose from", 1)[0].strip().strip("'\"")
        listed = bad.split("(choose from", 1)[1].rsplit(")", 1)[0] if "(choose from" in bad else ""
        choices = [c.strip().strip("'\"") for c in listed.split(",") if c.strip()]
        if tok in argv and choices:
            i = argv.index(tok)
            cands = [m for m in _group_mates(tok, tbl.get("synonyms") or []) if m in choices]
            cls = "synonym"
            if not cands:
                cands, cls = [c for c in choices if c.startswith(tok)], "spelling"
                cands = cands if len(cands) == 1 else []
            if not cands:
                cands, cls = _close(tok, choices), "close"
            for m in dict.fromkeys(cands):
                out.append((cls, argv[:i] + [m] + argv[i + 1:], f"`{m}` for `{tok}`"))
    if not out:
        pos = [i for i in walk["positionals"] if i >= walk["levels"][-1]["start"]]
        if 2 <= len(pos) <= 4:
            for perm in itertools.permutations(pos):
                if list(perm) != pos:
                    new = list(argv)
                    for slot, src in zip(pos, perm):
                        new[slot] = argv[src]
                    if new != argv:
                        out.append(("order", new, "positionals reordered"))
    return out


def _unsafe(parser, argv: list, tbl: dict, prog: str) -> str | None:
    """The word that makes ARGV destructive or outward (its form must be exact), else None."""
    walk = _walk(parser, argv)
    path = [argv[lv["sub"]] for lv in walk["levels"][:-1]]
    exact = (tbl.get("cli") or {}).get(prog, {}).get("exact") or []
    for n in range(len(path), 0, -1):
        if " ".join(path[:n]) in exact:
            return " ".join(path[:n])
    for lv in walk["levels"]:
        if lv["parser"].get_default("intent_exact"):
            return " ".join(path) or prog
    safe = set((tbl.get("cli") or {}).get(prog, {}).get("safe") or [])
    words = [w for w in path if w not in safe]
    if len(path) > 1 and path[-1] in (tbl.get("reads") or ()):  # `land status`: a read verb at the leaf is a read
        words = []
    end = argv.index("--") if "--" in argv else len(argv)
    words += [t for t in argv[:end] if t.startswith("-") and not _number(t)]
    return next((w for w in words if unsafe_word(w, tbl)), None)


def _read_only(parser, argv: list, tbl: dict, prog: str) -> bool:
    """Whether ARGV runs a subcommand that only reads: its last subcommand word is one of the table's `reads` (or
    the CLI's `safe`), is not on the CLI's `exact` list, and the form holds no unsafe word. A CLI with no
    subcommand is never read-only here: nothing names what it does."""
    walk = _walk(parser, argv)
    path = [argv[lv["sub"]] for lv in walk["levels"] if lv["sub"] is not None]
    if not path or _exact_path(prog, path, tbl) or _unsafe(parser, argv, tbl, prog):
        return False
    part = (tbl.get("cli") or {}).get(prog, {})
    return path[-1] in (tbl.get("reads") or []) or path[-1] in (part.get("safe") or [])


def repair(parser, argv: list, prog: str | None = None, tbl: dict | None = None) -> dict:
    """What ARGV, which PARSER refuses, meant. {"outcome": "ran" | "candidates" | "exact" | "none" | "ok",
    "argv": the one form to run (ran), "candidates": [{argv, notes}] (at most MAX_SHOWN), "error": the refusal}."""
    tbl = table() if tbl is None else tbl
    prog = prog or parser.prog
    argv = list(argv)
    first = _try(parser, argv)
    if first is None:
        return {"outcome": "ok", "argv": argv, "candidates": [], "error": ""}
    if _writes_files(parser):
        return {"outcome": "none", "argv": argv, "candidates": [], "error": first}
    seen = {tuple(argv)}
    queue = [(argv, [], first)]
    parsed, tried = [], 0
    while queue and tried < MAX_STATES:
        cur, notes, message = queue.pop(0)
        if len(notes) >= MAX_EDITS:
            continue
        walk = _walk(parser, cur)
        if walk["unknown"]:
            edits = _flag_edits(parser, cur, walk, tbl)
        elif walk["bad_sub"] is not None and ("invalid choice" in message or "required" in message):
            edits = _sub_edits(parser, cur, walk, tbl, prog)
        else:
            edits = _message_edits(parser, cur, walk, message, tbl)
        if not edits:
            continue
        best = edits[0][0]
        for cls, new, note in edits:
            if cls != best or tuple(new) in seen:
                continue
            seen.add(tuple(new))
            tried += 1
            again = _try(parser, new)
            if again is None:
                parsed.append({"argv": new, "notes": notes + [note], "classes": [cls]})
            else:
                queue.append((new, notes + [note], again))
    if not parsed:
        return {"outcome": "none", "argv": argv, "candidates": [], "error": first}
    fewest = min(len(p["notes"]) for p in parsed)
    parsed = [p for p in parsed if len(p["notes"]) == fewest][:MAX_SHOWN]
    if len(parsed) == 1:
        word = _unsafe(parser, parsed[0]["argv"], tbl, prog) or next(
            (w for w in argv if w.startswith("-") and unsafe_word(w, tbl)), None)
        if word:
            return {"outcome": "exact", "argv": argv, "candidates": parsed, "error": first, "word": word}
        return {"outcome": "ran", "argv": parsed[0]["argv"], "candidates": parsed, "error": first}
    if all(_read_only(parser, p["argv"], tbl, prog) for p in parsed) and not any(
            w.startswith("-") and unsafe_word(w, tbl) for w in argv):
        also = ", ".join(f"`{_show(prog, p['argv'])}`" for p in parsed[1:])
        best = dict(parsed[0], notes=parsed[0]["notes"] + [f"the best of {len(parsed)} read-only forms; also {also}"])
        return {"outcome": "ran", "argv": best["argv"], "candidates": [best], "error": first, "others": parsed[1:]}
    return {"outcome": "candidates", "argv": argv, "candidates": parsed, "error": first}


def _show(prog: str, argv: list) -> str:
    return shlex.join([prog] + list(argv))


def _log(prog: str, argv: list, result: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            return
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "cli": prog, "from": argv,
               "outcome": result["outcome"], "to": [c["argv"] for c in result["candidates"]],
               "notes": [n for c in result["candidates"] for n in c["notes"]], "error": result["error"][:300],
               "session": os.environ.get("ACPMUX_SESSION_NAME", "")}
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def say(prog: str, argv: list, result: dict, err=None) -> None:
    """The one line (ran), or the candidates as runnable commands, on ERR (default stderr)."""
    err = sys.stderr if err is None else err
    if result["outcome"] == "ran":
        notes = "; ".join(result["candidates"][0]["notes"])
        print(f"{prog}: ran `{_show(prog, result['argv'])}` for `{_show(prog, argv)}` ({notes})", file=err)
    elif result["outcome"] == "exact":
        print(f"{prog}: `{_show(prog, argv)}` is not a form of {prog}, and `{result['word']}` is destructive or "
              f"outward, so nothing ran: the exact form is needed. Did you mean:\n  "
              f"{_show(prog, result['candidates'][0]['argv'])}", file=err)
    elif result["outcome"] == "candidates":
        print(f"{prog}: `{_show(prog, argv)}` could mean (nothing ran):", file=err)
        for c in result["candidates"]:
            print(f"  {_show(prog, c['argv'])}", file=err)


def fix(argv: list, parser, prog: str | None = None, err=None) -> list:
    """ARGV as PARSER should read it: unchanged when it parses or nothing clear was meant, else the one form meant
    (said on ERR). Candidates and exact-form refusals are printed; the argv is returned as given for argparse to
    refuse. For a CLI that calls its parser itself; install() does this for every parser of a process."""
    prog = prog or parser.prog
    result = repair(parser, argv, prog)
    if result["outcome"] != "ok":
        _log(prog, list(argv), result)
        say(prog, list(argv), result, err)
    return result["argv"]


# ---- a parser as data: what the PreToolUse rewrite reads instead of starting the tool ----------------------------

def spec_of(parser) -> dict:
    """PARSER's tree in the vectors' spec form (tools/intent_vectors.json `about`), plus nargs for words that take
    several. What the spec cannot say (a custom type, an exclusive group) it leaves out, so the parser built from it
    refuses no more than PARSER does."""
    flags, required, args = {}, [], []
    for a in parser._actions:  # noqa: SLF001
        if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)):  # noqa: SLF001
            continue
        if a.option_strings:
            if not _takes_value(a):
                kind = "switch"
            elif a.nargs in ("*", "+", "?", argparse.REMAINDER) or isinstance(a.nargs, int):
                kind = "many"
            elif a.choices is not None and all(isinstance(c, str) for c in a.choices):
                kind = list(a.choices)
            else:
                kind = "int" if a.type is int else "value"
            flags[",".join(a.option_strings)] = kind
            if a.required:
                required.append(a.option_strings[0])
            continue
        arg = {"name": a.dest}
        if a.choices is not None and all(isinstance(c, str) for c in a.choices):
            arg["choices"] = list(a.choices)
        if a.type is int:
            arg["int"] = True
        if a.nargs == "?":
            arg["optional"] = True
        elif a.nargs is not None:
            arg["nargs"] = a.nargs
        args.append(arg["name"] if len(arg) == 1 else arg)
    spec: dict = {}
    if flags:
        spec["flags"] = flags
    if required:
        spec["required"] = required
    if args:
        spec["args"] = args
    sub = _sub_action(parser)
    if sub is not None:
        spec["subs"] = {name: spec_of(child) for name, child in sub.choices.items()}
        if not sub.required:
            spec["subs_optional"] = True
    if not parser.allow_abbrev:
        spec["abbrev"] = False
    return spec


def parser_of(spec: dict, prog: str = "t"):
    """The argparse parser a spec describes (spec_of's inverse, and the vectors' parsers)."""
    parser = argparse.ArgumentParser(prog=prog, allow_abbrev=spec.get("abbrev", True))
    _fill(parser, spec)
    return parser


def _fill(parser, spec: dict) -> None:
    for names, kind in (spec.get("flags") or {}).items():
        names = names.split(",")
        required = names[0] in (spec.get("required") or [])
        if kind == "switch":
            parser.add_argument(*names, action="store_true")
        elif kind == "many":
            parser.add_argument(*names, nargs="*", required=required)
        elif isinstance(kind, list):
            parser.add_argument(*names, choices=kind, required=required)
        else:
            parser.add_argument(*names, type=int if kind == "int" else str, required=required)
    for n, arg in enumerate(spec.get("args") or []):
        arg = {"name": arg} if isinstance(arg, str) else arg
        kw: dict = {}
        if arg.get("choices"):
            kw["choices"] = arg["choices"]
        if arg.get("int"):
            kw["type"] = int
        if arg.get("optional"):
            kw["nargs"] = "?"
        elif "nargs" in arg:
            kw["nargs"] = arg["nargs"]
        parser.add_argument(f"arg{n}_{arg['name']}".replace("-", "_"), metavar=arg["name"], **kw)
    if spec.get("subs"):
        subs = parser.add_subparsers(dest="cmd", required=not spec.get("subs_optional"))
        for name, inner in spec["subs"].items():
            _fill(subs.add_parser(name, allow_abbrev=inner.get("abbrev", True)), inner)


def _write_words(typed: list, path: list, tbl: dict) -> bool:
    """Whether an alias from TYPED to the subcommand words PATH touches a write: an unsafe word typed, or one in
    PATH, unless PATH ends in a reading verb under it (`land status` reads; _unsafe says the same of a parser)."""
    if len(path) > 1 and path[-1] in (tbl.get("reads") or ()):
        return False  # what runs is a read, whatever word was typed for it (`land-status`)
    return any(unsafe_word(t, tbl) for t in list(typed) + list(path))


def alias(argv: list, prog: str, tbl: dict | None = None):
    """(ARGV with the table's first matching alias of PROG applied, the rule), or None. For a CLI with no spec: the
    words agents typed and the words that then worked (tools/intent_table.py). Never with an unsafe word on a side."""
    tbl = table() if tbl is None else tbl
    for rule in (tbl.get("cli") or {}).get(prog, {}).get("aliases") or []:
        under, frm, to = rule.get("under") or [], rule.get("from") or [], rule.get("to") or []
        at = len(under)
        if frm and to and argv[:at] == under and argv[at:at + len(frm)] == frm and not _write_words(
                frm, under + to, tbl) and not _exact_path(prog, under + to, tbl):
            return argv[:at] + list(to) + argv[at + len(frm):], rule
    return None


# ---- argparse: install ------------------------------------------------------------------------------------------

def _wrap(name: str):
    original = _orig[name]

    def parse(self, args=None, namespace=None):
        if _state["trial"] or _state["depth"]:
            return original(self, args, namespace)
        if os.environ.get("INTENT_SPEC_OUT"):  # tools/intent_table.py specs: the parser as data, nothing runs
            with open(os.environ["INTENT_SPEC_OUT"], "w", encoding="utf-8") as fh:
                json.dump(dict(spec_of(self), prog=self.prog, loose=name == "parse_known_args"), fh)
            raise SystemExit(0)
        given = list(sys.argv[1:] if args is None else args)
        _state.update(depth=1, top=self, argv=given)
        try:
            return original(self, given, namespace)
        except _Rerun as rerun:
            _state["done"] = True  # one repair per parse: the repaired form's own refusal is argparse's to print
            return original(self, rerun.argv, namespace)
        finally:
            _state.update(depth=0, top=None, argv=None)

    parse.__name__ = name
    parse.__doc__ = original.__doc__
    return parse


def _error(self, message):
    if _state["trial"]:
        raise _Fail(message, self)
    top, argv = _state["top"], _state["argv"]
    if top is None or _state["done"] or os.environ.get("INTENT_OFF"):
        return _orig["error"](self, message)
    _state["done"] = True
    prog = _state["prog"] or top.prog
    try:
        result = repair(top, argv, prog)
    except Exception:  # noqa: BLE001 (the layer never costs a CLI its own refusal)
        return _orig["error"](self, message)
    if result["outcome"] == "ok":
        return _orig["error"](self, message)
    _log(prog, argv, result)
    say(prog, argv, result)
    if result["outcome"] == "ran":
        raise _Rerun(result["argv"])
    return _orig["error"](self, message)


def _exit(self, status=0, message=None):
    if _state["trial"]:
        raise _Stop(status)
    return _orig["exit"](self, status, message)


_orig.update(error=argparse.ArgumentParser.error, exit=argparse.ArgumentParser.exit)


def install(prog: str | None = None) -> None:
    """Every argparse parser of this process repairs a refused argv as above. Call it once, from the program's own
    entry (never at import of a module tests load). PROG names the CLI in the table (default: the parser's prog)."""
    _state["prog"] = prog or _state["prog"]
    if _state["installed"]:
        return
    cls = argparse.ArgumentParser
    _orig.update(parse_args=cls.parse_args, parse_known_args=cls.parse_known_args, error=cls.error, exit=cls.exit)
    cls.parse_args = _wrap("parse_args")
    cls.parse_known_args = _wrap("parse_known_args")
    cls.error = _error
    cls.exit = _exit
    _state["installed"] = True


def uninstall() -> None:
    if _state["installed"]:
        for name, fn in _orig.items():
            setattr(argparse.ArgumentParser, name, fn)
        _state.update(installed=False, done=False, prog=None)


def _exact_path(prog: str, path: list, tbl: dict) -> bool:
    """Whether the table's per-CLI `exact` list names PATH (its words, or its last word): a subcommand that is
    destructive or outward by what it does, not by an unsafe word in its name."""
    exact = (tbl.get("cli") or {}).get(prog, {}).get("exact") or []
    return " ".join(path) in exact or (bool(path) and path[-1] in exact)


# ---- hand-rolled CLIs ---------------------------------------------------------------------------------------

def word(tok: str, known: list, prog: str, kind: str = "synonyms", tbl: dict | None = None, under: list | None = None,
         err=None, log: bool = True) -> str:
    """For a CLI that reads its own argv: TOK when it is one of KNOWN, else the one word of KNOWN it meant (a table
    alias of one word, a synonym, a spelling, a unique prefix, the one close match), said on ERR. A destructive or
    outward word is never guessed: TOK is returned for the CLI to refuse, with the candidate printed. KIND:
    "synonyms" for a subcommand or value, "flag_synonyms" for a flag."""
    if tok in known:
        return tok
    tbl = table() if tbl is None else tbl
    err = sys.stderr if err is None else err
    cands: list = []
    if kind == "synonyms":
        for rule in (tbl.get("cli") or {}).get(prog, {}).get("aliases") or []:
            if rule.get("from") == [tok] and len(rule.get("to") or []) == 1 and (rule.get("under") or []) == (
                    under or []) and rule["to"][0] in known:
                cands.append(rule["to"][0])
    if not cands:
        cands = [m for m in _group_mates(tok.split("=", 1)[0], tbl.get(kind) or []) if m in known]
    if not cands and kind == "flag_synonyms":
        cands = [c for c in _norm_flag(tok.split("=", 1)[0]) if c in known][:1]
    if not cands:
        prefixed = [k for k in known if k.startswith(tok)]
        cands = prefixed if len(prefixed) == 1 and len(tok) >= 2 else _close(tok.split("=", 1)[0], list(known))
    cands = list(dict.fromkeys(cands))
    if not cands:
        return tok
    name, eq, value = tok.partition("=")
    shown = [c + (eq + value if kind == "flag_synonyms" else "") for c in cands[:MAX_SHOWN]]
    result = {"candidates": [{"argv": [s], "notes": [f"`{s}` for `{tok}`"]} for s in shown], "error": ""}
    if len(cands) > 1:
        result["outcome"] = "candidates"
        print(f"{prog}: `{tok}` could mean (nothing ran): " + ", ".join(f"`{s}`" for s in shown), file=err)
    elif unsafe_word(cands[0], tbl) or unsafe_word(tok, tbl) or _exact_path(prog, (under or []) + [cands[0]], tbl):
        result["outcome"] = "exact"
        print(f"{prog}: `{tok}` is not a form of {prog}, and `{shown[0]}` is destructive or outward, so nothing ran: "
              f"the exact form is needed. Did you mean `{shown[0]}`?", file=err)
    else:
        result["outcome"] = "ran"
        print(f"{prog}: ran `{shown[0]}` for `{tok}`", file=err)
    if log:
        _log(prog, [tok], result)
    return shown[0] if result["outcome"] == "ran" else tok


def words(argv: list, prog: str, subcommands: list | None = None, flags: dict | None = None,
          tbl: dict | None = None, err=None) -> list:
    """ARGV for a hand-rolled CLI: its first plain word read as one of SUBCOMMANDS, and each `--flag` as one of
    FLAGS[that subcommand] plus FLAGS[""] (the CLI's own), by word() above; and the table's multi-word aliases for
    PROG applied at the head. Everything else is kept as given."""
    tbl = table() if tbl is None else tbl
    out = list(argv)
    for rule in (tbl.get("cli") or {}).get(prog, {}).get("aliases") or []:
        frm, to = rule.get("from") or [], rule.get("to") or []
        if frm and not rule.get("under") and out[:len(frm)] == frm and (len(frm) > 1 or len(to) > 1) and not (
                _write_words(frm, to, tbl)):
            print(f"{prog}: ran `{_show(prog, to + out[len(frm):])}` for `{_show(prog, out)}`",
                  file=sys.stderr if err is None else err)
            _log(prog, out, {"outcome": "ran", "candidates": [{"argv": to + out[len(frm):], "notes": ["table"]}],
                             "error": ""})
            out = list(to) + out[len(frm):]
            break
    sub = ""
    if subcommands:
        for i, tok in enumerate(out):
            if not tok.startswith("-"):
                out[i] = sub = word(tok, list(subcommands), prog, tbl=tbl, err=err)
                break
    if flags:
        known = list(flags.get("") or []) + list(flags.get(sub) or [])
        for i, tok in enumerate(out):
            if tok == "--":
                break
            if tok.startswith("--") and len(tok) > 2 and tok.split("=", 1)[0] not in known:
                out[i] = word(tok, known, prog, kind="flag_synonyms", tbl=tbl, err=err)
    return out


def main(argv: list) -> int:
    if argv[:1] == ["log"]:
        try:
            sys.stdout.write(open(LOG, encoding="utf-8").read())
        except OSError:
            pass
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
