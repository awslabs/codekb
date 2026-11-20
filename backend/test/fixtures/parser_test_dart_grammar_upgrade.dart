// Dart the shipped grammar cannot read, pinned so the loss stays visible.
//
// A NEW fixture, not an append to parser_test_dart.dart: that file is
// append-only because the frozen u1/u2 expectations pin every line range in it,
// and `test_dart_u1_identities_are_unchanged` requires its 43 identities to be
// byte-identical. Nothing here may reach it.
//
// `tree-sitter-dart==0.1.0` is the only release on PyPI and predates Dart 3.13
// declaring constructors, so it cannot parse them. Two halves, both silent:
//
//  1. The five declaring-constructor forms, copied verbatim from
//     `flame-engine/flame` (file and line recorded against each). Each produces
//     ERROR or MISSING nodes and its class is absent from the index. Three of
//     them recover with a keyword where the class name belongs, which the
//     name-trust check refuses rather than publishing.
//  2. The generic-invocation pair. `Foo<T>(...)` with a SINGLE type argument
//     parses as a comparison chain with no ERROR node at all, so the call is
//     invisible to the call graph. `Foo<T>.named(...)` and the two-type-argument
//     form both read correctly, which scopes the defect.
//
// `test_dart_grammar_limits.py` asserts all of this as current behaviour. A
// grammar release that fixes any of it will fail that file, which is the signal
// to update both.
//
// Declarations are Dart's own shapes, reduced to the member set a component
// test needs; the headers are unchanged from flame.

// ── 1. The five declaring-constructor forms, from flame ──────────────────────

/// flame `examples/lib/commons/rounded_rect_component.dart:7`.
/// Plain declaring constructor on a class with a superclass and a mixin.
/// Shipped grammar: 1 ERROR node; the name `RoundedRectComponent` survives in
/// the text but the declaration does not parse.
class RoundedRectComponent() extends PositionComponent with HasPaint {
  void render(Canvas canvas) {}
}

/// flame `examples/lib/commons/example_app.dart:6`.
/// `const` declaring constructor with a required field parameter and
/// `super.key`. Shipped grammar: 2 ERROR nodes, and `name:` reads `const` — so
/// `ExampleApp` is absent and nothing is published under the keyword.
class const ExampleApp({required this.title}) extends StatelessWidget {
  final String title;
  Widget build(BuildContext context) => Container();
}

/// flame `packages/flame/lib/src/experimental/geometry/shapes/shape.dart:11`.
/// `const` declaring constructor plus a named constructor `._`.
/// Shipped grammar: `name:` reads `const`.
class const PathFinderData({required this.start}) {
  final int start;
  PathFinderData._(this.start);
  void recompute() {}
}

/// flame `examples/lib/stories/system/system_colors.dart:9`.
/// An enum with a declaring constructor — the one form this grammar reads.
enum GameColors {
  red,
  green,
  blue,
}

/// flame `packages/flame/lib/src/collisions/hitboxes/hitbox.dart:7`.
/// Declaring constructor after a type-parameter list whose bound is itself
/// generic. Shipped grammar: 1 MISSING node.
abstract class Hitbox<T extends Hitbox<T>>()
    implements GenericCollisionCallbacks<T> {
  void onCollision(Set<Vector2> points, T other) {}
}

// ── 2. The generic-invocation pair ───────────────────────────────────────────

/// THE DEFECT. `Foo<T>(...)` with a single type argument.
///
/// Read as a comparison chain — `ConstantTween < Color > (closedColor)`, a
/// nested `relational_expression` — which is not one of this parser's call
/// nodes, so the call is invisible. Zero ERROR nodes, so nothing reports it.
/// `flutter/packages` contains 3,580 occurrences of this shape in 517 files.
Animatable<Color?> buildTween(Color closedColor) {
  return ConstantTween<Color>(closedColor);
}

/// READS CORRECTLY. `Foo<T>.named(...)` with a single type argument.
///
/// Parses as an invocation rather than a comparison chain. Kept beside the
/// defect above because the two shapes differ by one `.name` and it would
/// otherwise be natural to assume both are affected.
Future<void> waitThenFill(Duration d) async {
  await Future<void>.delayed(d);
  final zeros = List<int>.filled(3, 0);
}

/// THE CONTROL. Two type arguments parse without ERROR nodes and are read as
/// invocations, so the defect is scoped to the single-type-argument case rather
/// than to generics generally.
Map<String, int> copyCounts(Map<String, int> m) {
  return Map<String, int>.from(m);
}
