// Dart parser fixture for CodeKB.
//
// This file is both the grammar-acceptance fixture (W1) and the integration-test
// fixture (W5) for u1-dart-parser-core. It is deliberately readable: it is the
// clearest statement of what DartParser extracts.
//
// It covers, in order:
//   * the required language level — null safety, records, patterns, sealed and
//     final classes (BR1.1);
//   * every construct in this unit's scope (BR1.7);
//   * all five identity-collision cases (BR3.1-BR3.4, BR3.8).

import 'dart:math';

/// A top-level constant.
const int maxRetries = 3;

/// A top-level variable.
var requestCount = 0;

/// A private top-level variable, private by its leading underscore (BR3.5).
int _internalCounter = 0;

/// A late-initialised top-level variable (null-safety language level).
late String deferredLabel;

/// A top-level function with a required-positional, an optional-positional and
/// a named parameter (BR3.6).
int computeTotal(int base, [int bonus = 0, int? cap]) {
  return min(base + bonus, cap ?? base + bonus);
}

/// A top-level function with named parameters, one of them `required`.
String describe({required String name, int count = 1}) {
  return '$name x$count';
}

/// A private top-level function.
void _reset() {
  _internalCounter = 0;
  requestCount = 0;
}

/// A top-level getter. Collides with the setter below unless suffixed (BR3.2).
int get retryBudget => maxRetries - requestCount;

/// A top-level setter of the same name as the getter above.
set retryBudget(int value) {
  requestCount = maxRetries - value;
}

/// A record-returning function (required language level).
(int, String) splitLabel(String raw) {
  final parts = raw.split(':');
  return (int.parse(parts[0]), parts[1]);
}

/// Pattern matching over a record (required language level).
String classify(Object value) {
  return switch (value) {
    (int n, String s) when n > 0 => 'positive $s',
    (int _, String s) => 'other $s',
    int n => 'bare $n',
    _ => 'unknown',
  };
}

/// An abstract class.
abstract class Shape {
  /// An abstract method.
  double area();
}

/// A sealed class (required language level).
sealed class Result {}

/// A final class (required language level).
final class Success extends Result {
  final String payload;

  /// An unnamed constructor. Its member name is `new`, which keeps it apart
  /// from its own class and is a form a call site may write (BR3.4).
  const Success(this.payload);
}

/// A base class.
base class Failure extends Result {
  final String reason;
  Failure(this.reason);
}

/// Carries most of the member-level cases.
class Repository {
  /// A static field.
  static const String scheme = 'repo';

  final Map<String, int> _store = {};
  late int _version;

  /// An unnamed constructor — identity `Repository.new` (BR3.4).
  Repository();

  /// A named constructor. Its identity keeps the name written at a call site,
  /// so `of` below resolves to this constructor rather than to nothing (BR3.4).
  Repository.seeded(Map<String, int> initial) {
    _store.addAll(initial);
  }

  /// A factory constructor, called through `Repository()` below.
  factory Repository.empty() => Repository();

  /// A method. It cannot be called `seeded`: Dart rejects that clash (BR3.4).
  void merge(Map<String, int> more) {
    _store.addAll(more);
  }

  /// A getter. Collides with the setter below unless suffixed (BR3.2).
  int get version => _version;

  /// A setter of the same name as the getter above.
  set version(int value) {
    _version = value;
  }

  /// Index read — `operator[]:1` (BR3.8).
  int operator [](String key) => _store[key] ?? 0;

  /// Index write — `operator[]=:2`, distinct from the read above (BR3.8).
  void operator []=(String key, int value) {
    _store[key] = value;
  }

  /// Unary minus — `operator-:0` (BR3.8).
  Repository operator -() => Repository();

  /// Binary minus — `operator-:1`, distinct from the unary form by arity
  /// (BR3.8).
  Repository operator -(Repository other) => Repository();

  /// A static method.
  static Repository of(Map<String, int> seed) => Repository.seeded(seed);

  /// A private method.
  void _bump() {
    _version += 1;
  }

  /// A `build` method. Collides with `Widget.build` below unless the enclosing
  /// class qualifies it (BR3.3).
  String build(String prefix) {
    _bump();
    return '$prefix:${_store.length}';
  }
}

/// The second class carrying a `build` method, for the collision case.
class Widget {
  /// A `build` method on a different class — must stay distinct from
  /// `Repository.build`.
  String build(String prefix) {
    return describe(name: prefix);
  }
}

/// A plain enum.
enum Status {
  pending,
  active,
  _retired,
}

/// An enhanced enum with a field, a constructor and a method.
enum Priority {
  low(1),
  high(10);

  final int weight;

  const Priority(this.weight);

  bool get isUrgent => weight >= 10;
}

// ── u2-dart-extended-constructs ─────────────────────────────────────────────
//
// Appended, never inserted (BR4.3): every line range the expectation table
// already records stays correct, so the addition is additive rather than a
// rewrite of the whole table.

/// A mixin with a method and a getter/setter pair. A mixin maps to `class`
/// (BR1.1), and its members are attributed to the mixin by name (BR2.1).
mixin Auditable {
  final List<String> _entries = [];

  /// A mixin method — identity `parser_test_dart.Auditable.record`.
  void record(String entry) {
    _entries.add(entry);
  }

  /// A mixin getter. Collides with the setter below unless suffixed (BR2.2).
  int get entryCount => _entries.length;

  /// A mixin setter of the same name as the getter above.
  set entryCount(int value) {
    while (_entries.length > value) {
      _entries.removeLast();
    }
  }
}

/// `mixin class` is a class declaration with a modifier, extracted once by
/// u1's existing class path; BR1.1 does not fire (BR3.1).
mixin class Loggable {
  /// A method on the mixin class.
  void log(String message) {
    requestCount += 1;
  }
}

/// A named extension on a type this fixture declares, so the attribution
/// assertion is about a real alternative rather than a vacuous one (BR4.4).
extension RepositoryReport on Repository {
  /// Identity `parser_test_dart.RepositoryReport.summarise`, never
  /// `parser_test_dart.Repository.summarise`: the member is reachable only
  /// where this extension is in scope (BR1.2, BR2.1).
  String summarise() {
    return build('report');
  }

  /// An extension getter, suffixed exactly as a class getter is (BR2.2).
  int get tally => version;

  /// An extension setter of the same name as the getter above.
  set tally(int value) {
    version = value;
  }
}

/// A top-level function whose name the unnamed extension below also declares.
/// The design review proved this pair loses a component when an unnamed
/// extension's members carry an empty enclosing name.
String shout(String raw) {
  return raw.toUpperCase();
}

/// An unnamed extension on `String`. No component identifies the extension
/// itself; its member is qualified by the extended type, which keeps it apart
/// from the top-level `shout` above.
extension on String {
  String shout() {
    return toUpperCase();
  }
}

/// A second unnamed extension, on a different type, declaring the same member
/// name: these two must not collide with each other either.
extension on int {
  String shout() {
    return 'n$this';
  }
}

/// An extension type — a distinct construct, not an extension (BR3.2). Its
/// members are attributed to its own declared name, as a class's are. The
/// representation field `(int value)` yields no component, because this parser
/// extracts no fields.
extension type Meters(int value) {
  /// Identity `parser_test_dart.Meters.raw.get`.
  int get raw => value;

  /// Identity `parser_test_dart.Meters.scaled`.
  int scaled(int factor) {
    return value * factor;
  }
}

/// A typedef in the modern form, aliasing a function type (BR1.3).
typedef Transform = String Function(String input);

/// A typedef in the legacy function-type form, still valid Dart.
typedef void Notify(String message);

/// A typedef aliasing a type that is NOT a function type. The design's claim
/// that both typedef forms are function types is false, and the signature
/// records whatever the declaration aliases rather than asserting one.
typedef LabelList = List<String>;
