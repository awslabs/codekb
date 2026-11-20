// Dart that the pinned tree-sitter-dart 0.1.0 grammar cannot parse, beside Dart
// that it parses perfectly well and must keep.
//
// A SEPARATE fixture from parser_test_dart.dart on purpose: that file is
// append-only because the frozen u1/u2 expectations pin every line range in it,
// and this file holds syntax that puts ERROR and MISSING nodes into the tree.
//
// The first three declarations use Dart's declaring-constructor syntax, copied
// in shape from flame's `main` (`class const InventoryEvent({...})`,
// `class Foo()`, `enum Foo()`). The rest are ordinary Dart whose names contain,
// resemble or exactly equal a keyword that is legal in that position — the
// components an over-broad keyword guard would delete.

/// Declaring constructor with a `const` modifier: the grammar takes `const`
/// for the class name and strands `InventoryEvent` in an ERROR node.
class const InventoryEvent({
  required this.id,
  this.quantity = 0,
}) {
  final String id;
  int quantity;

  void describe() {}
}

/// The same defect on a named constructor, as flame writes it.
class const PathFinderData._({
  required this.terrain,
}) {
  void recompute() {}
}

/// Declaring constructor with no modifier: the name survives, and only a
/// MISSING `implements` marks the damage.
class Waypoint() {
  void advance() {}
}

/// An enum with a declaring constructor.
enum Suit() {
  hearts,
  spades,
}

/// `get`, `set` and `export` are built-in identifiers: barred from a declared
/// type name, and perfectly legal as member names. AppFlowy declares all three.
abstract class KeyValueStore {
  Future<String?> get(String key);

  Future<void> set(String key, String value);

  Future<String> export(String target);
}

/// Names that merely contain or resemble a keyword.
const int constValue = 7;

final String finalise = 'done';

int _const = 3;

class Constants {
  void describe() {}
}

class _Const {
  void run() {}
}

/// A top-level function whose name is a built-in identifier.
Future<void> export(String target) async {}

typedef Mapper = int Function(String input);
