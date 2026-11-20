"""Node text must come from the node's bytes, not a byte-offset slice of decoded
text — otherwise a UTF-8 BOM or any earlier multi-byte character truncates names
(and therefore component ids), breaking lookup by name.
"""

import tempfile
from pathlib import Path

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import IdScheme

UTF8_BOM = b"\xef\xbb\xbf"


def _names(filename: str, content: bytes):
    d = Path(tempfile.mkdtemp())
    (d / filename).write_bytes(content)
    cat = ComponentCatalog()
    CodeAnalyzer(cat).analyze_directory(d, space_id="t", id_scheme=IdScheme.V2_PATH)
    comps = list(cat._components.values())
    return {c.get_simple_name() for c in comps}, comps


def test_csharp_bom_does_not_truncate_names():
    body = b"namespace Demo { public class Shape { public int Area() { return 1; } } }\n"
    with_bom, comps = _names("M.cs", UTF8_BOM + body)
    without_bom, _ = _names("N.cs", body)

    assert with_bom == without_bom  # BOM must not change anything
    assert {"Shape", "Area"} <= with_bom
    # No name is a truncation of a real one (e.g. "ape"/"hape" for "Shape").
    assert "ape" not in with_bom and "hape" not in with_bom
    # The namespace prefix in the ids is intact ("Demo", not the truncated "mo").
    assert any(c.id.split("::")[-1].startswith("Demo.") for c in comps)
    assert not any(c.id.split("::")[-1].startswith("mo.") for c in comps)


def test_non_ascii_earlier_on_line_does_not_shift_names():
    # 'é' (2 bytes) sits inside the type name, before the trailing part.
    code = "class CaféHelper { int x; }\n".encode("utf-8")
    names, _ = _names("J.java", code)
    assert any(n.startswith("Cafe") for n in names)  # name captured whole
    assert "Helper" not in names  # not shifted into a bare "Helper"
    assert "x" in names
