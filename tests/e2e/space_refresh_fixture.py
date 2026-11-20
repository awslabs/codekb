"""Fixture code base for the space refresh end-to-end test.

The fixture is a small repository whose structure the test fully controls. It
has the same modules in four languages (Python, Java, TypeScript, JavaScript),
so every change type the test makes runs in each of them.

The whole tree is a function of a ``State``. The seed is ``State()``; each chain
step copies the state, changes a field or two, and regenerates the tree. That
keeps every commit internally consistent (for example, the JavaScript export
list always matches the functions present) and lets ``expected_callables``
model, by hand, which functions and methods the parser should find.

Constraints that come from CodeKB's behaviour (design doc, section 5.3):

- The test names a component by its file and symbol (``component_key``), not
  by its ID, so it holds for any ID format whose first segment stands for the
  file. File stems are unique across all languages and contain no dots,
  except where scenario S20 collides them on purpose.
- CodeKB resolves a call by its bare name across every file and language, so
  every function name here is unique across languages too.
- Nothing lives under a directory that ``analysis.exclude_patterns`` excludes
  (``tests/``, ``build/`` and so on), except where S13 moves a file there.
- Each module uses words from its own unusual domain ("quokka ledger",
  "granite crate"), so a semantic search for those words finds that module.

This module only builds file contents. It never touches git or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath

CHAIN_LENGTH = 12  # hop 00 .. hop 11


@dataclass(frozen=True)
class Language:
    """Per-language naming, so stems and vocabulary never overlap."""

    key: str  # py | java | ts | js
    root: str
    ext: str
    chain: str
    accounts: str
    postings: str
    shipping: str
    rates: str
    fees: str
    tariffs: str
    weird: str
    comment: str  # line-comment prefix


LANGUAGES: tuple[Language, ...] = (
    Language("py", "py", ".py", "quokka", "heron", "ibis", "basalt", "tapir", "lemur", "marmot", "yak", "#"),
    Language("java", "java", ".java", "wombat", "egret", "kestrel", "granite", "okapi", "gecko", "ferret", "ibex", "//"),
    Language("ts", "ts", ".ts", "numbat", "plover", "osprey", "slate", "dugong", "axolotl", "quoll", "kudu", "//"),
    Language("js", "js", ".js", "bilby", "curlew", "merlin", "marble", "pangolin", "civet", "gerbil", "eland", "//"),
)
BY_KEY = {lang.key: lang for lang in LANGUAGES}


@dataclass(frozen=True)
class State:
    """Everything that varies between fixture commits. Defaults are the seed."""

    marker: str = "seed"
    header_rev: int = 0  # S23: first line of every code file
    readme_rev: int = 0  # S14
    # chain
    chain_leaf_v2: bool = False  # S5: body of the last hop
    chain_mid_v2: bool = False  # S21: body of hop 05
    # accounts
    accounts_stem: str = "accounts"  # S9: renamed file
    normalize_v2: bool = False  # S4: body of the shared callee
    # postings
    postings_area: str = "ledger"  # S10: moved directory
    postings_void: bool = False  # S3: added function
    postings_format: bool = True  # S7: deleted function (its caller is unchanged)
    validate_name: str = "validate"  # S11: renamed function and its callers
    # shipping
    shipping_area: str = "freight"  # S13: moved into an excluded directory and back
    shipping_reordered: bool = False  # S6: a method moved within the file
    carrier_v2: bool = False  # S15: a field and a class comment
    describe_present: bool = True  # S17: deleted function
    # rates
    base_rate_v2: bool = False  # S17: body of a callee
    # new modules
    fees: bool = False  # S2 adds, S8 deletes
    tariffs: bool = False  # S17 adds
    tariffs_padding: int = 0  # S18: bytes of padding, to cross the size limit
    surcharges: bool = False  # S19: defines the function rates already calls
    collisions: bool = False  # S20: files whose stem collides with an existing one
    weird: int = 0  # S12: 0 none, 1 added, 2 modified
    js_present: bool = True  # S16

    def with_(self, **changes) -> "State":
        return replace(self, **changes)


# ── Naming ───────────────────────────────────────────────────────────────────


def stem(lang: Language, module: str) -> str:
    """File stem for a module. Java uses CamelCase class-style stems."""
    if lang.key == "java":
        return "Java" + "".join(part.capitalize() for part in module.split("_"))
    return f"{lang.key}_{module}"


def path_of(lang: Language, area: str, module: str) -> str:
    return f"{lang.root}/{area}/{stem(lang, module)}{lang.ext}"


def fn(lang: Language, *words: str) -> str:
    """A function name in the language's convention: snake_case or camelCase."""
    if lang.key == "py":
        return "_".join(words)
    return words[0] + "".join(w.capitalize() for w in words[1:])


def hop_name(lang: Language, i: int) -> str:
    return f"{lang.chain}_hop_{i:02d}" if lang.key == "py" else f"{lang.chain}Hop{i:02d}"


def component_key(lang: Language, path: str, name: str, *, class_name: str | None = None) -> str:
    """How the test names a component: ``<repo-relative path>#<symbol>``.

    The symbol is the component ID after its first, file-naming segment. Java
    methods sit inside a class, by default named after the file, so their
    symbol is ``<Class>.<name>``. Methods of a named class use that class.
    """
    if lang.key == "java":
        return f"{path}#{class_name or PurePosixPath(path).stem}.{name}"
    if class_name:
        return f"{path}#{class_name}.{name}"
    return f"{path}#{name}"


def shipping_class(lang: Language) -> str:
    return f"{lang.shipping.capitalize()}FreightManifest"


def surcharge_name(lang: Language) -> str:
    """Called by the rates module; defined only once S19 adds the surcharges module."""
    return fn(lang, lang.rates, "surcharge")


def header(lang: Language, text: str, state: State) -> str:
    """First line of a code file. S23 bumps the revision on every file at once."""
    rev = f" (revision {state.header_rev})" if state.header_rev else ""
    if lang.key == "py":
        return f'"""{text}{rev}"""'
    if lang.key == "java":
        return f"/** {text}{rev} */"
    return f"// {text}{rev}"


def doc(lang: Language, text: str, indent: str = "") -> str:
    """A doc comment line placed before a definition (Java, TS, JS)."""
    if lang.key == "java":
        return f"{indent}/** {text} */"
    return f"{indent}// {text}"


def _exports(lang: Language, names: list[str]) -> list[str]:
    return ["", f"module.exports = {{ {', '.join(names)} }};"] if lang.key == "js" else []


def _sig(lang: Language, params: list[tuple[str, str]], ret: str) -> str:
    """Parameter list and return annotation for TypeScript; bare for JavaScript."""
    if lang.key == "ts":
        return "(" + ", ".join(f"{p}: {t}" for p, t in params) + f"): {ret}"
    return "(" + ", ".join(p for p, _ in params) + ")"


def _export(lang: Language) -> str:
    return "export " if lang.key == "ts" else ""


# ── Modules ──────────────────────────────────────────────────────────────────


def chain_module(lang: Language, state: State) -> str:
    w, W = lang.chain, lang.chain.capitalize()
    n = CHAIN_LENGTH
    lines = [header(lang, f"{W} ledger: a straight chain of hops over marsupial receipts.", state)]

    def body(i: int) -> tuple[str, str]:
        """Doc text and return expression for hop i."""
        if i == n - 1:
            if state.chain_leaf_v2:
                return (f"{W} ledger hop {i:02d}: splits the receipt total three ways into {w} shares.",
                        "amount // 3" if lang.key == "py" else "Math.floor(amount / 3)")
            return (f"{W} ledger hop {i:02d}: rounds the receipt total to whole {w} coins.",
                    "round(amount * 1.5)" if lang.key == "py" else
                    "(int) Math.round(amount * 1.5)" if lang.key == "java" else "Math.round(amount * 1.5)")
        nxt = hop_name(lang, i + 1)
        if i == 5 and state.chain_mid_v2:
            return (f"{W} ledger hop {i:02d}: doubles the receipt total before passing it on.", f"{nxt}(amount * 2)")
        return (f"{W} ledger hop {i:02d}: passes the receipt total one hop down.", f"{nxt}(amount + {i + 1})")

    if lang.key == "py":
        for i in range(n):
            text, expr = body(i)
            lines += ["", "", f"def {hop_name(lang, i)}(amount):", f'    """{text}"""', f"    return {expr}"]
        return "\n".join(lines) + "\n"
    if lang.key == "java":
        cls = stem(lang, "chain")
        lines.append(f"public class {cls} {{")
        for i in range(n):
            text, expr = body(i)
            lines += ["", doc(lang, text, "    "), f"    public static int {hop_name(lang, i)}(int amount) {{",
                      f"        return {expr};", "    }"]
        lines.append("}")
        return "\n".join(lines) + "\n"
    for i in range(n):
        text, expr = body(i)
        lines += ["", doc(lang, text), f"{_export(lang)}function {hop_name(lang, i)}{_sig(lang, [('amount', 'number')], 'number')} {{",
                  f"  return {expr};", "}"]
    lines += _exports(lang, [hop_name(lang, i) for i in range(n)])
    return "\n".join(lines) + "\n"


def accounts_names(lang: Language) -> tuple[str, str, str]:
    a = lang.accounts
    return fn(lang, "normalize", a, "amount"), fn(lang, "reconcile", a, "balance"), fn(lang, "audit", a, "balance")


def accounts_module(lang: Language, state: State) -> str:
    """Two callers share one callee (fan-in)."""
    w, W = lang.accounts, lang.accounts.capitalize()
    norm, rec, aud = accounts_names(lang)
    # Locals get names unique across the fixture: TS, JS and Python locals become
    # components, and CodeKB resolves a call by bare name across languages, so a
    # local called "sum" would capture Python's built-in sum() calls.
    cents, tenths, acc = fn(lang, w, "cents"), fn(lang, w, "tenths"), fn(lang, w, "running")
    if state.normalize_v2:
        norm_doc = f"Normalize a {w} account amount to tenths, capped at a ceiling of ten thousand."
        norm_py = [f"    {tenths} = int(amount * 10)", f"    return min({tenths}, 10000)"]
        norm_c = [f"long {tenths} = (long) (amount * 10);", f"return Math.min({tenths}, 10000);"]
        norm_js = [f"const {tenths} = Math.trunc(amount * 10);", f"return Math.min({tenths}, 10000);"]
    else:
        norm_doc = f"Normalize a {w} account amount to cents, dropping negative noise."
        norm_py = [f"    {cents} = int(round(amount * 100))", f"    return max({cents}, 0)"]
        norm_c = [f"long {cents} = Math.round(amount * 100);", f"return Math.max({cents}, 0);"]
        norm_js = [f"const {cents} = Math.round(amount * 100);", f"return Math.max({cents}, 0);"]
    rec_doc = f"Reconcile a {w} account by summing its normalized entries."
    aud_doc = f"Audit a {w} account: true when the normalized total matches the expected amount."
    head = header(lang, f"{W} accounts: balances reconciled and audited against one normalizer.", state)
    s = stem(lang, state.accounts_stem)
    if lang.key == "py":
        return "\n".join([
            head, "", "",
            f"def {norm}(amount):", f'    """{norm_doc}"""', *norm_py, "", "",
            f"def {rec}(entries):", f'    """{rec_doc}"""', f"    return sum({norm}(e) for e in entries)", "", "",
            f"def {aud}(entries, expected):", f'    """{aud_doc}"""', f"    {acc} = 0", "    for e in entries:",
            f"        {acc} += {norm}(e)", f"    return {acc} == expected", "",
        ])
    if lang.key == "java":
        return "\n".join([
            head, f"public class {s} {{", "",
            doc(lang, norm_doc, "    "), f"    public static long {norm}(double amount) {{",
            *("        " + x for x in norm_c), "    }", "",
            doc(lang, rec_doc, "    "), f"    public static long {rec}(double[] entries) {{", f"        long {acc} = 0;",
            "        for (double e : entries) {", f"            {acc} += {norm}(e);", "        }", f"        return {acc};", "    }", "",
            doc(lang, aud_doc, "    "), f"    public static boolean {aud}(double[] entries, long expected) {{",
            f"        long {acc} = 0;", "        for (double e : entries) {", f"            {acc} += {norm}(e);", "        }",
            f"        return {acc} == expected;", "    }", "}", "",
        ])
    e = _export(lang)
    return "\n".join([
        head, "",
        doc(lang, norm_doc), f"{e}function {norm}{_sig(lang, [('amount', 'number')], 'number')} {{", *("  " + x for x in norm_js), "}", "",
        doc(lang, rec_doc), f"{e}function {rec}{_sig(lang, [('entries', 'number[]')], 'number')} {{", f"  let {acc} = 0;",
        "  for (const e of entries) {", f"    {acc} += {norm}(e);", "  }", f"  return {acc};", "}", "",
        doc(lang, aud_doc), f"{e}function {aud}{_sig(lang, [('entries', 'number[]'), ('expected', 'number')], 'boolean')} {{",
        f"  let {fn(lang, w, 'audited')} = 0;", "  for (const e of entries) {", f"    {fn(lang, w, 'audited')} += {norm}(e);", "  }",
        f"  return {fn(lang, w, 'audited')} === expected;", "}",
        *_exports(lang, [norm, rec, aud]), "",
    ])


def postings_names(lang: Language, state: State) -> dict[str, str]:
    w = lang.postings
    return {
        "check": fn(lang, state.validate_name, w, "entry"),
        "format": fn(lang, "format", w, "memo"),
        "post": fn(lang, "post", w, "entry"),
        "void": fn(lang, "void", w, "entry"),
    }


def postings_module(lang: Language, state: State) -> str:
    """A poster calls a validator and a formatter. S7 deletes the formatter but
    leaves the poster calling it; S3 appends a voider."""
    w, W = lang.postings, lang.postings.capitalize()
    n = postings_names(lang, state)
    head = header(lang, f"{W} postings: journal entries validated and given a memo before posting.", state)
    check_doc = f"Reject a {w} journal entry without an account or with a zero amount."
    fmt_doc = f"Build the {w} posting memo from the entry's account and amount."
    post_doc = f"Post a {w} journal entry after validating it, recording its memo."
    void_doc = f"Void a {w} journal entry, recording a reversal line for it."
    if lang.key == "py":
        out = [head, "", "", f"def {n['check']}(entry):", f'    """{check_doc}"""',
               '    return bool(entry.get("account")) and entry.get("amount", 0) != 0']
        if state.postings_format:
            out += ["", "", f"def {n['format']}(entry):", f'    """{fmt_doc}"""',
                    f"    return f\"{w}:{{entry['account']}}:{{entry['amount']}}\""]
        out += ["", "", f"def {n['post']}(journal, entry):", f'    """{post_doc}"""', f"    if not {n['check']}(entry):",
                f'        raise ValueError("invalid {w} entry")', f"    journal.append({n['format']}(entry))", "    return len(journal)"]
        if state.postings_void:
            out += ["", "", f"def {n['void']}(journal, entry):", f'    """{void_doc}"""', f"    if {n['check']}(entry):",
                    f'        journal.append("void:{w}:" + str(entry["account"]))', "    return len(journal)"]
        return "\n".join(out) + "\n"
    if lang.key == "java":
        cls = stem(lang, "postings")
        out = [head, f"public class {cls} {{", "", doc(lang, check_doc, "    "),
               f"    public static boolean {n['check']}(String account, long amount) {{",
               "        return account != null && !account.isEmpty() && amount != 0;", "    }"]
        if state.postings_format:
            out += ["", doc(lang, fmt_doc, "    "), f"    public static String {n['format']}(String account, long amount) {{",
                    f'        return "{w}:" + account + ":" + amount;', "    }"]
        out += ["", doc(lang, post_doc, "    "),
                f"    public static int {n['post']}(java.util.List<String> journal, String account, long amount) {{",
                f"        if (!{n['check']}(account, amount)) {{", f'            throw new IllegalArgumentException("invalid {w} entry");',
                "        }", f"        journal.add({n['format']}(account, amount));", "        return journal.size();", "    }"]
        if state.postings_void:
            out += ["", doc(lang, void_doc, "    "),
                    f"    public static int {n['void']}(java.util.List<String> journal, String account, long amount) {{",
                    f"        if ({n['check']}(account, amount)) {{", f'            journal.add("void:{w}:" + account);', "        }",
                    "        return journal.size();", "    }"]
        out += ["}"]
        return "\n".join(out) + "\n"
    e = _export(lang)
    entry_t = "{ account: string; amount: number }"
    out = [head, "", doc(lang, check_doc), f"{e}function {n['check']}{_sig(lang, [('entry', entry_t)], 'boolean')} {{",
           "  return Boolean(entry.account) && entry.amount !== 0;", "}"]
    names = [n["check"]]
    if state.postings_format:
        out += ["", doc(lang, fmt_doc), f"{e}function {n['format']}{_sig(lang, [('entry', entry_t)], 'string')} {{",
                f'  return "{w}:" + entry.account + ":" + entry.amount;', "}"]
        names.append(n["format"])
    out += ["", doc(lang, post_doc), f"{e}function {n['post']}{_sig(lang, [('journal', 'string[]'), ('entry', entry_t)], 'number')} {{",
            f"  if (!{n['check']}(entry)) {{", f'    throw new Error("invalid {w} entry");', "  }",
            f"  journal.push({n['format']}(entry));", "  return journal.length;", "}"]
    names.append(n["post"])
    if state.postings_void:
        out += ["", doc(lang, void_doc), f"{e}function {n['void']}{_sig(lang, [('journal', 'string[]'), ('entry', entry_t)], 'number')} {{",
                f"  if ({n['check']}(entry)) {{", f'    journal.push("void:{w}:" + entry.account);', "  }", "  return journal.length;", "}"]
        names.append(n["void"])
    out += _exports(lang, names)
    return "\n".join(out) + "\n"


def shipping_names(lang: Language) -> dict[str, str]:
    w = lang.shipping
    if lang.key == "py":
        return {"add": f"add_{w}_crate", "weigh": f"_weigh_{w}_crate", "total": f"total_{w}_weight",
                "describe": f"describe_{w}_manifest", "init": "__init__"}
    W = w.capitalize()
    return {"add": f"add{W}Crate", "weigh": f"weigh{W}Crate", "total": f"total{W}Weight", "describe": f"describe{W}Manifest"}


def shipping_module(lang: Language, state: State) -> str:
    """A class with a field and methods, plus a function that uses it."""
    w, W = lang.shipping, lang.shipping.capitalize()
    n = shipping_names(lang)
    cls = shipping_class(lang)
    crates = fn(lang, w, "crates")  # unique per language: CodeKB resolves names across languages
    code = (w[:4] if not state.carrier_v2 else w[0] + w[2] + w[3] + w[-1]).upper()
    cls_doc = f"A manifest of {w} crates for one {'ocean ' if state.carrier_v2 else ''}freight carrier."
    head = header(lang, f"{W} freight: manifests of {w} crates and their shipped weight.", state)
    add_doc = f"Add a {w} crate of the given weight to the manifest."
    weigh_doc = f"Weight of one {w} crate including its pallet allowance."
    total_doc = f"Total shipped weight of every {w} crate on the manifest."
    desc_doc = f"One-line summary of a {w} freight manifest and its weight."
    if lang.key == "py":
        add = ["", f"    def {n['add']}(self, weight):", f'        """{add_doc}"""', f"        self.{crates}.append(weight)"]
        weigh = ["", f"    def {n['weigh']}(self, weight):", f'        """{weigh_doc}"""', "        return weight * 1.1"]
        total = ["", f"    def {n['total']}(self):", f'        """{total_doc}"""', f"        return sum(self.{n['weigh']}(c) for c in self.{crates})"]
        methods = (total + weigh + add) if state.shipping_reordered else (add + weigh + total)
        out = [head, "", "", f"class {cls}:", f'    """{cls_doc}"""', "", f'    carrier_code = "{code}"', "",
               "    def __init__(self):", f"        self.{crates} = []", *methods]
        if state.describe_present:
            out += ["", "", f"def {n['describe']}(manifest):", f'    """{desc_doc}"""',
                    f'    return f"{{manifest.carrier_code}} {{len(manifest.{crates})}} crates {{manifest.{n["total"]}():.1f}}"']
        return "\n".join(out) + "\n"
    if lang.key == "java":
        file_cls = stem(lang, "shipping")
        add = ["", doc(lang, add_doc, "    "), f"    public void {n['add']}(double weight) {{", f"        {crates}.add(weight);", "    }"]
        weigh = ["", doc(lang, weigh_doc, "    "), f"    private double {n['weigh']}(double weight) {{", "        return weight * 1.1;", "    }"]
        tot = fn(lang, w, "tally")
        total = ["", doc(lang, total_doc, "    "), f"    public double {n['total']}() {{", f"        double {tot} = 0;",
                 f"        for (double c : {crates}) {{", f"            {tot} += {n['weigh']}(c);", "        }", f"        return {tot};", "    }"]
        methods = (total + weigh + add) if state.shipping_reordered else (add + weigh + total)
        out = [head, doc(lang, cls_doc), f"public class {file_cls} {{", "", doc(lang, f"Carrier code for {w} freight.", "    "),
               f'    public static final String CARRIER_CODE = "{code}";', "",
               f"    private final java.util.List<Double> {crates} = new java.util.ArrayList<>();", *methods]
        if state.describe_present:
            out += ["", doc(lang, desc_doc, "    "), f"    public String {n['describe']}() {{",
                    f'        return CARRIER_CODE + " " + {crates}.size() + " crates " + {n["total"]}();', "    }"]
        out += ["}"]
        return "\n".join(out) + "\n"
    num = ": number" if lang.key == "ts" else ""
    void = ": void" if lang.key == "ts" else ""
    add = ["", doc(lang, add_doc, "  "), f"  {n['add']}(weight{num}){void} {{", f"    this.{crates}.push(weight);", "  }"]
    weigh = ["", doc(lang, weigh_doc, "  "), f"  {n['weigh']}(weight{num}){num} {{", "    return weight * 1.1;", "  }"]
    tot = fn(lang, w, "tally")
    total = ["", doc(lang, total_doc, "  "), f"  {n['total']}(){num} {{", f"    let {tot} = 0;", f"    for (const c of this.{crates}) {{",
             f"      {tot} += this.{n['weigh']}(c);", "    }", f"    return {tot};", "  }"]
    methods = (total + weigh + add) if state.shipping_reordered else (add + weigh + total)
    field_t = ": string" if lang.key == "ts" else ""
    crates_t = ": number[]" if lang.key == "ts" else ""
    out = [head, "", doc(lang, cls_doc), f"{_export(lang)}class {cls} {{", f'  carrierCode{field_t} = "{code}";',
           f"  {crates}{crates_t} = [];", *methods, "}"]
    names = [cls]
    if state.describe_present:
        param = f"manifest: {cls}" if lang.key == "ts" else "manifest"
        rs = ": string" if lang.key == "ts" else ""
        out += ["", doc(lang, desc_doc), f"{_export(lang)}function {n['describe']}({param}){rs} {{",
                f'  return manifest.carrierCode + " " + manifest.{crates}.length + " crates " + manifest.{n["total"]}();', "}"]
        names.append(n["describe"])
    out += _exports(lang, names)
    return "\n".join(out) + "\n"


def rates_names(lang: Language) -> tuple[str, str]:
    return fn(lang, "base", lang.rates, "rate"), fn(lang, "quote", lang.rates, "rate")


def rates_module(lang: Language, state: State, *, base_body_only: bool = False) -> str:
    """``quote`` calls the surcharge function, which no file defines until S19."""
    w, W = lang.rates, lang.rates.capitalize()
    base, quote = rates_names(lang)
    sur = surcharge_name(lang)
    if state.base_rate_v2:
        base_doc, expr = f"Base {w} freight rate: a higher flat fee and a lower per-kilogram charge.", "15 + weight * 0.65"
    else:
        base_doc, expr = f"Base {w} freight rate: a flat fee plus a per-kilogram charge.", "12 + weight * 0.8"
    quote_doc = f"Quote a {w} freight rate: the base rate plus any surcharge."
    head = header(lang, f"{W} rates: freight rate quotes per kilogram.", state)
    if lang.key == "py":
        return "\n".join([head, "", "", f"def {base}(weight):", f'    """{base_doc}"""', f"    return {expr}", "", "",
                          f"def {quote}(weight):", f'    """{quote_doc}"""', f"    return {base}(weight) + {sur}(weight)", ""])
    if lang.key == "java":
        cls = stem(lang, "rates")
        return "\n".join([head, f"public class {cls} {{", "", doc(lang, base_doc, "    "),
                          f"    public static double {base}(double weight) {{", f"        return {expr};", "    }", "",
                          doc(lang, quote_doc, "    "), f"    public static double {quote}(double weight) {{",
                          f"        return {base}(weight) + {sur}(weight);", "    }", "}", ""])
    decl = [f"declare function {sur}(weight: number): number;", ""] if lang.key == "ts" else []
    e = _export(lang)
    sig = _sig(lang, [("weight", "number")], "number")
    return "\n".join([head, "", *decl, doc(lang, base_doc), f"{e}function {base}{sig} {{", f"  return {expr};", "}", "",
                      doc(lang, quote_doc), f"{e}function {quote}{sig} {{", f"  return {base}(weight) + {sur}(weight);", "}",
                      *_exports(lang, [base, quote]), ""])


def fees_names(lang: Language) -> tuple[str, str, str]:
    f = lang.fees
    return fn(lang, f, "fee", "base"), fn(lang, f, "fee", "late"), fn(lang, f, "fee", "total")


def fees_module(lang: Language, state: State) -> str:
    """Added by S2, deleted by S8: three functions, two of them callers."""
    w, W = lang.fees, lang.fees.capitalize()
    base, late, total = fees_names(lang)
    head = header(lang, f"{W} fees: membership fees with late charges.", state)
    d_base = f"Base {w} membership fee: a tenth of the amount, at least one coin."
    d_late = f"Late {w} fee: the base fee plus two coins for every overdue day."
    d_total = f"Total {w} charge: the amount plus its late fee."
    if lang.key == "py":
        return "\n".join([head, "", "", f"def {base}(amount):", f'    """{d_base}"""', "    return max(amount // 10, 1)", "", "",
                          f"def {late}(amount, days):", f'    """{d_late}"""', f"    return {base}(amount) + 2 * days", "", "",
                          f"def {total}(amount, days):", f'    """{d_total}"""', f"    return amount + {late}(amount, days)", ""])
    if lang.key == "java":
        cls = stem(lang, "fees")
        return "\n".join([head, f"public class {cls} {{", "", doc(lang, d_base, "    "), f"    public static long {base}(long amount) {{",
                          "        return Math.max(amount / 10, 1);", "    }", "", doc(lang, d_late, "    "),
                          f"    public static long {late}(long amount, int days) {{", f"        return {base}(amount) + 2L * days;", "    }", "",
                          doc(lang, d_total, "    "), f"    public static long {total}(long amount, int days) {{",
                          f"        return amount + {late}(amount, days);", "    }", "}", ""])
    e = _export(lang)
    return "\n".join([head, "", doc(lang, d_base), f"{e}function {base}{_sig(lang, [('amount', 'number')], 'number')} {{",
                      "  return Math.max(Math.floor(amount / 10), 1);", "}", "",
                      doc(lang, d_late), f"{e}function {late}{_sig(lang, [('amount', 'number'), ('days', 'number')], 'number')} {{",
                      f"  return {base}(amount) + 2 * days;", "}", "",
                      doc(lang, d_total), f"{e}function {total}{_sig(lang, [('amount', 'number'), ('days', 'number')], 'number')} {{",
                      f"  return amount + {late}(amount, days);", "}", *_exports(lang, [base, late, total]), ""])


def tariffs_names(lang: Language) -> tuple[str, str]:
    t = lang.tariffs
    return fn(lang, t, "tariff", "rate"), fn(lang, t, "tariff", "quote")


def tariffs_module(lang: Language, state: State) -> str:
    """Added by S17. S18 pads it past the file size limit and back."""
    w, W = lang.tariffs, lang.tariffs.capitalize()
    rate, quote = tariffs_names(lang)
    head = header(lang, f"{W} tariffs: customs tariffs on imported freight.", state)
    d_rate = f"{W} customs tariff rate for a declared value: five percent."
    d_quote = f"Quote the {w} customs tariff owed on a declared value, in whole coins."
    if lang.key == "py":
        text = "\n".join([head, "", "", f"def {rate}(value):", f'    """{d_rate}"""', "    return value * 0.05", "", "",
                          f"def {quote}(value):", f'    """{d_quote}"""', f"    return round({rate}(value))", ""])
    elif lang.key == "java":
        cls = stem(lang, "tariffs")
        text = "\n".join([head, f"public class {cls} {{", "", doc(lang, d_rate, "    "), f"    public static double {rate}(double value) {{",
                          "        return value * 0.05;", "    }", "", doc(lang, d_quote, "    "),
                          f"    public static long {quote}(double value) {{", f"        return Math.round({rate}(value));", "    }", "}", ""])
    else:
        e = _export(lang)
        sig = _sig(lang, [("value", "number")], "number")
        text = "\n".join([head, "", doc(lang, d_rate), f"{e}function {rate}{sig} {{", "  return value * 0.05;", "}", "",
                          doc(lang, d_quote), f"{e}function {quote}{sig} {{", f"  return Math.round({rate}(value));", "}",
                          *_exports(lang, [rate, quote]), ""])
    if state.tariffs_padding:
        line = f"{lang.comment} padding to cross the analysis file size limit: {w} tariff schedule filler line.\n"
        text += line * (state.tariffs_padding // len(line) + 1)
    return text


def surcharges_module(lang: Language, state: State) -> str:
    """Added by S19: defines the function the rates module already calls."""
    w = lang.rates
    sur = surcharge_name(lang)
    d = f"Fuel surcharge on a {w} freight rate: three coins plus two percent of the weight."
    head = header(lang, f"{w.capitalize()} surcharges: fuel surcharges on freight rates.", state)
    if lang.key == "py":
        return "\n".join([head, "", "", f"def {sur}(weight):", f'    """{d}"""', "    return 3 + weight * 0.02", ""])
    if lang.key == "java":
        cls = stem(lang, "surcharges")
        return "\n".join([head, f"public class {cls} {{", "", doc(lang, d, "    "), f"    public static double {sur}(double weight) {{",
                          "        return 3 + weight * 0.02;", "    }", "}", ""])
    return "\n".join([head, "", doc(lang, d), f"{_export(lang)}function {sur}{_sig(lang, [('weight', 'number')], 'number')} {{",
                      "  return 3 + weight * 0.02;", "}", *_exports(lang, [sur]), ""])


def collision_files(lang: Language, state: State) -> dict[str, str]:
    """S20: a second file with the same stem as the rates module, defining the
    same function name with a different body. Python also gets a TypeScript
    file with the Python rates stem, so the collision crosses languages."""
    base, _ = rates_names(lang)
    w = lang.rates
    rel_stem = stem(lang, "rates")
    d = f"Discounted base {w} freight rate for contract customers."
    head = header(lang, f"{w.capitalize()} contract rates.", state)
    files: dict[str, str] = {}
    if lang.key == "py":
        files[f"{lang.root}/freight/contract/{rel_stem}.py"] = "\n".join(
            [head, "", "", f"def {base}(weight):", f'    """{d}"""', "    return 10 + weight * 0.7", ""])
        ts = BY_KEY["ts"]
        files[f"{ts.root}/freight/contract/{rel_stem}.ts"] = "\n".join(
            [header(ts, f"{w.capitalize()} contract rates, ported.", state), "", doc(ts, d + " Ported."),
             f"export function {base}(weight: number): number {{", "  return 9 + weight * 0.7;", "}", ""])
    elif lang.key == "java":
        files[f"{lang.root}/freight/contract/{rel_stem}.java"] = "\n".join(
            [head, f"public class {rel_stem} {{", "", doc(lang, d, "    "), f"    public static double {base}(double weight) {{",
             "        return 10 + weight * 0.7;", "    }", "}", ""])
    else:
        files[f"{lang.root}/freight/contract/{rel_stem}{lang.ext}"] = "\n".join(
            [head, "", doc(lang, d), f"{_export(lang)}function {base}{_sig(lang, [('weight', 'number')], 'number')} {{",
             "  return 10 + weight * 0.7;", "}", *_exports(lang, [base]), ""])
    return files


WEIRD_TAGS = ("space", "unicode", "quote", "backslash")


def weird_stem(lang: Language, tag: str) -> str:
    """File stems with a space, non-ASCII characters, a quote and a backslash (C2)."""
    return {
        "space": f"{lang.key} spaced name",
        "unicode": f"{lang.key} ünïcödé ñame",
        "quote": f"{lang.key} quote'name",
        "backslash": f"{lang.key} back\\slash",
    }[tag]


def weird_function(lang: Language, tag: str) -> str:
    return fn(lang, lang.weird, tag, "tally")


def weird_class(tag: str) -> str:
    return f"Weird{tag.capitalize()}"


def weird_files(lang: Language, state: State) -> dict[str, str]:
    files = {}
    for tag in WEIRD_TAGS:
        name = weird_function(lang, tag)
        if state.weird == 2:
            d, expr = f"{lang.weird.capitalize()} {tag} tally, doubled for the second count.", "count * 2"
        else:
            d, expr = f"{lang.weird.capitalize()} {tag} tally: the count plus one.", "count + 1"
        head = header(lang, f"{lang.weird.capitalize()} tallies kept in a file with an unusual name.", state)
        if lang.key == "py":
            text = "\n".join([head, "", "", f"def {name}(count):", f'    """{d}"""', f"    return {expr}", ""])
        elif lang.key == "java":
            text = "\n".join([head, f"class {weird_class(tag)} {{", "", doc(lang, d, "    "), f"    static int {name}(int count) {{",
                              f"        return {expr};", "    }", "}", ""])
        else:
            text = "\n".join([head, "", doc(lang, d), f"{_export(lang)}function {name}{_sig(lang, [('count', 'number')], 'number')} {{",
                              f"  return {expr};", "}", *_exports(lang, [name]), ""])
        files[f"{lang.root}/misc/{weird_stem(lang, tag)}{lang.ext}"] = text
    return files


# ── Tree ─────────────────────────────────────────────────────────────────────

MARKER_PATH = "marker/fixture_marker.py"

MARKER_KEY = f"{MARKER_PATH}#FIXTURE_STEP"


def marker_file(state: State) -> str:
    """Changed by every commit, so a reader can tell which commit a response comes from."""
    return f'"""Fixture step marker for the space refresh end-to-end test."""\n\nFIXTURE_STEP = "{state.marker}"\n'


def readme(state: State) -> str:
    rev = f"\nRevision {state.readme_rev}.\n" if state.readme_rev else ""
    return ("# CodeKB space refresh fixture\n\nGenerated by `tests/e2e/space_refresh_e2e.py` in the AwsCodeKB package. "
            "Every branch under `codekb-e2e/` is created and deleted by that test. Do not edit by hand.\n" + rev)


def build_tree(state: State) -> dict[str, str]:
    """The repository contents for a state: path -> file content."""
    tree: dict[str, str] = {"docs/README.md": readme(state), MARKER_PATH: marker_file(state)}
    for lang in LANGUAGES:
        if lang.key == "js" and not state.js_present:
            continue
        tree[path_of(lang, "ledger", "chain")] = chain_module(lang, state)
        tree[path_of(lang, "ledger", state.accounts_stem)] = accounts_module(lang, state)
        tree[path_of(lang, state.postings_area, "postings")] = postings_module(lang, state)
        tree[path_of(lang, state.shipping_area, "shipping")] = shipping_module(lang, state)
        tree[path_of(lang, "freight", "rates")] = rates_module(lang, state)
        if state.fees:
            tree[path_of(lang, "ledger", "fees")] = fees_module(lang, state)
        if state.tariffs:
            tree[path_of(lang, "freight", "tariffs")] = tariffs_module(lang, state)
        if state.surcharges:
            tree[path_of(lang, "freight", "surcharges")] = surcharges_module(lang, state)
        if state.collisions:
            tree.update(collision_files(lang, state))
        if state.weird:
            tree.update(weird_files(lang, state))
    return tree


def seed_tree() -> dict[str, str]:
    return build_tree(State())


# ── Hand-written model of what the parser should find ───────────────────────

EXCLUDED_AREAS = ("build",)  # areas the fixture uses that analysis.exclude_patterns excludes


def expected_callables(state: State, *, max_file_bytes: int | None = None) -> set[str]:
    """Keys (``component_key``) of the functions and methods the parser should produce.

    Written by hand from the templates above, independently of CodeKB, so a
    disagreement with a fresh index points at the fixture or at the parser.
    ``max_file_bytes`` models the file size limit for S18.
    """
    ids: set[str] = set()
    for lang in LANGUAGES:
        if lang.key == "js" and not state.js_present:
            continue

        def cid(path: str, name: str, class_name: str | None = None) -> str:
            return component_key(lang, path, name, class_name=class_name)

        chain = path_of(lang, "ledger", "chain")
        ids |= {cid(chain, hop_name(lang, i)) for i in range(CHAIN_LENGTH)}
        acc = path_of(lang, "ledger", state.accounts_stem)
        ids |= {cid(acc, name) for name in accounts_names(lang)}
        post = path_of(lang, state.postings_area, "postings")
        n = postings_names(lang, state)
        wanted = ["check", "post"] + (["format"] if state.postings_format else []) + (["void"] if state.postings_void else [])
        ids |= {cid(post, n[k]) for k in wanted}
        if state.shipping_area not in EXCLUDED_AREAS:
            ship = path_of(lang, state.shipping_area, "shipping")
            sn = shipping_names(lang)
            cls = shipping_class(lang)
            methods = ["add", "weigh", "total"] + (["init"] if lang.key == "py" else [])
            if lang.key == "java":
                ids |= {cid(ship, sn[k]) for k in methods}
                if state.describe_present:
                    ids.add(cid(ship, sn["describe"]))
            else:
                ids |= {cid(ship, sn[k], cls) for k in methods}
                if state.describe_present:
                    ids.add(cid(ship, sn["describe"]))
        rates = path_of(lang, "freight", "rates")
        ids |= {cid(rates, name) for name in rates_names(lang)}
        if state.fees:
            ids |= {cid(path_of(lang, "ledger", "fees"), name) for name in fees_names(lang)}
        if state.tariffs:
            too_big = max_file_bytes is not None and len(tariffs_module(lang, state).encode()) > max_file_bytes
            if not too_big:
                ids |= {cid(path_of(lang, "freight", "tariffs"), name) for name in tariffs_names(lang)}
        if state.surcharges:
            ids.add(cid(path_of(lang, "freight", "surcharges"), surcharge_name(lang)))
        # Each colliding file keeps its own function. Stem-only IDs (legacy Spaces) merge them, and fail S20.
        if state.collisions:
            base, rel_stem = rates_names(lang)[0], stem(lang, "rates")
            ids.add(cid(f"{lang.root}/freight/contract/{rel_stem}{lang.ext}", base))
            if lang.key == "py":
                ts = BY_KEY["ts"]
                ids.add(component_key(ts, f"{ts.root}/freight/contract/{rel_stem}{ts.ext}", base))
        if state.weird:
            for tag in WEIRD_TAGS:
                ids.add(cid(f"{lang.root}/misc/{weird_stem(lang, tag)}{lang.ext}", weird_function(lang, tag),
                            weird_class(tag) if lang.key == "java" else None))
    return ids


def search_queries(state: State | None = None) -> list[str]:
    """One query per module domain. Together they reach every vector of a Space."""
    queries: list[str] = []
    for lang in LANGUAGES:
        queries += [
            f"{lang.chain} ledger hop passes receipt total",
            f"{lang.accounts} account balance normalize reconcile audit",
            f"{lang.postings} journal entry posting memo validate void",
            f"{lang.shipping} freight crate manifest weight",
            f"{lang.rates} freight rate quote surcharge contract",
            f"{lang.fees} membership fee late charge",
            f"{lang.tariffs} customs tariff declared value",
            f"{lang.weird} tally unusual file name",
        ]
    return queries


def expected_seed_edges() -> list[tuple[str, str]]:
    """Call edges the seed should produce, as (caller, callee) component keys.

    A fixture sanity check: if the parser doesn't resolve these, expectations
    that depend on them (stale callers in S4 and S5) are unreliable.
    """
    edges: list[tuple[str, str]] = []
    for lang in LANGUAGES:
        chain = path_of(lang, "ledger", "chain")
        hops = [component_key(lang, chain, hop_name(lang, i)) for i in range(CHAIN_LENGTH)]
        edges += list(zip(hops, hops[1:]))
        acc = path_of(lang, "ledger", "accounts")
        norm, rec, aud = (component_key(lang, acc, name) for name in accounts_names(lang))
        edges += [(rec, norm), (aud, norm)]
    return edges
