"""Call graph tests for Go method calls."""

import pytest

try:  # pragma: no cover
    import tree_sitter_go as tsgo

    GO_AVAILABLE = True
except ImportError:  # pragma: no cover
    tsgo = None
    GO_AVAILABLE = False

from tree_sitter import Language, Parser

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import ComponentType, IdScheme
from src.flow.flow_analyzer import FlowAnalyzer
from src.flow.go_flow_support import get_go_parameter_names
from src.parsers import GoParser

pytestmark = pytest.mark.skipif(not GO_AVAILABLE, reason="tree_sitter_go not installed")

REPO = """package store
type OrderRepo struct{}
func NewOrderRepo() *OrderRepo { return &OrderRepo{} }
func (r *OrderRepo) Save(id string) error { return nil }
func (r *OrderRepo) Find(id string) (string, error) { return id, nil }
"""


def parse(source):
    return Parser(Language(tsgo.language())).parse(source)


def analyze(tmp_path, files):
    components = []
    for name, body in files.items():
        path = tmp_path / name
        path.write_text(body)
        source = path.read_bytes()
        parser = GoParser()
        parser._scheme = IdScheme.V1_STEM
        components += parser.extract_components(parse(source).root_node, source, path)

    catalog = ComponentCatalog()
    for c in components:
        catalog.add_component(c)
    graph = FlowAnalyzer(catalog).build_call_graph(components)
    return components, graph


def calls(graph, caller):
    for cid, callees in graph.items():
        if cid.split("::", 1)[-1] == caller:
            return sorted(c.split("::", 1)[-1] for c in callees)
    return []


def test_method_has_receiver_as_parent(tmp_path):
    src = "package a\ntype User struct{}\nfunc (u *User) Activate() {}\n"
    components, _ = analyze(tmp_path, {"user.go": src})
    method = next(c for c in components if c.type == ComponentType.METHOD)
    assert method.id == "user.User.Activate"  # same id as before
    assert method.parent_component == "user.User"


def test_generic_receiver_methods_are_kept(tmp_path):
    src = (
        "package a\n"
        "type Stack[T any] struct{ items []T }\n"
        "func (s *Stack[T]) Push(v T) { s.items = append(s.items, v) }\n"
        "func (s *Stack[T]) PushAll(vs []T) { for _, v := range vs { s.Push(v) } }\n"
    )
    _, graph = analyze(tmp_path, {"stack.go": src})
    assert calls(graph, "stack.Stack.PushAll") == ["stack.Stack.Push"]


@pytest.mark.parametrize("recv", ["s S", "s *S"])
def test_call_on_receiver(tmp_path, recv):
    src = f"package a\ntype S struct{{}}\nfunc ({recv}) a() {{}}\nfunc ({recv}) B() {{ s.a() }}\n"
    _, graph = analyze(tmp_path, {"s.go": src})
    assert calls(graph, "s.S.B") == ["s.S.a"]


def test_call_on_pointer_param(tmp_path):
    svc = 'package store\nfunc Place(r *OrderRepo) (string, error) { r.Save("1"); return r.Find("1") }\n'
    _, graph = analyze(tmp_path, {"repo.go": REPO, "svc.go": svc})
    assert calls(graph, "svc.Place") == ["repo.OrderRepo.Find", "repo.OrderRepo.Save"]


@pytest.mark.parametrize(
    "decl",
    ["r := &OrderRepo{}", "r := OrderRepo{}", "var r OrderRepo", "var r = &OrderRepo{}"],
)
def test_call_on_local_var(tmp_path, decl):
    svc = f'package store\nfunc Place() {{\n\t{decl}\n\tr.Save("1")\n}}\n'
    _, graph = analyze(tmp_path, {"repo.go": REPO, "svc.go": svc})
    assert calls(graph, "svc.Place") == ["repo.OrderRepo.Save"]


def test_picks_method_by_receiver_type(tmp_path):
    types = (
        "package a\n"
        "type Cart struct{}\nfunc (c *Cart) Save() {}\n"
        "type Order struct{}\nfunc (o *Order) Save() {}\n"
    )
    svc = "package a\nfunc Checkout(o *Order) { o.Save() }\n"
    _, graph = analyze(tmp_path, {"types.go": types, "svc.go": svc})
    assert calls(graph, "svc.Checkout") == ["types.Order.Save"]


def test_unknown_receiver_type_is_not_guessed(tmp_path):
    svc = 'package store\nfunc Place() { x := load(); x.Save("1") }\n'
    _, graph = analyze(tmp_path, {"repo.go": REPO, "svc.go": svc})
    assert calls(graph, "svc.Place") == []


@pytest.mark.parametrize(
    "func, expected",
    [
        ('func F(a int) (string, error) { return "", nil }', ["a"]),
        ("func F(a int) (n int, err error) { return }", ["a"]),
        ("func (s *S) M(a, b int) error { return nil }", ["a", "b"]),
    ],
)
def test_param_names_ignore_results(func, expected):
    code = f"package a\ntype S struct{{}}\n{func}\n".encode()

    def text(node):
        return code[node.start_byte : node.end_byte].decode()

    assert get_go_parameter_names(parse(code).root_node, 2, text) == expected
