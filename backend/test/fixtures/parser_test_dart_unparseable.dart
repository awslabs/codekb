// Dart the grammar recovers from by asserting a name nobody wrote, beside
// clean Dart it must keep.
//
// A tree-sitter grammar lags the language it reads, and when it cannot parse a
// declaration it still produces a tree — sometimes with a keyword where the
// name belongs and the real name stranded in a sibling ERROR node. Publishing
// that would lose the real declaration AND invent a searchable component named
// after the keyword; the name-trust check refuses it instead.
//
// The shape is what the guard is written against rather than any one syntax, so
// this fixture represents it directly. The real lagging-syntax case, Dart 3.13
// declaring constructors, lives in `parser_test_dart_grammar_upgrade.dart`.
//
// The two refused declarations below put a class modifier where the name
// belongs. That is the shape, exactly: `name:` reads a keyword and the real
// name is stranded in a sibling ERROR node. A modifier in the wrong order is a
// mistake a person makes rather than syntax the grammar lags, which is the one
// respect in which it is a weaker representative than `class const Foo` was —
// the lagging-syntax case stays covered by the `augment class` snippet in
// `test_the_guard_is_not_specific_to_declaring_constructors`.
//
// A SEPARATE fixture from parser_test_dart.dart on purpose: that file is
// append-only because the frozen u1/u2 expectations pin every line range in it,
// and this file holds syntax that puts ERROR nodes into the tree.

/// `name:` reads `sealed`; the real name `Thing` is stranded in an ERROR node.
class sealed Thing {
  void ok() {}
}

/// The same shape on a second modifier, so the guard is not pinned to one word.
class interface Facade {
  void run() {}
}

/// Clean Dart that must survive the refusals above: a refusal is scoped to the
/// declaration it could not name, never to the file.
class Waypoint {
  void advance() {}
}

enum Suit {
  hearts,
  spades,
}

const int constValue = 7;
