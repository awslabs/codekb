# Languages

CodeKB parses supported languages into components:

| Language | Additional notes |
|---|---|
| C | |
| C++ | |
| C# | |
| Dart | See [Dart notes](#dart) |
| Go | |
| Java | |
| JavaScript | |
| Kotlin | |
| PHP | |
| Python | See [Python notes](#python) |
| Ruby | |
| Rust | |
| Terraform | HCL files |
| TypeScript | |

Files in other languages are skipped, and the space shows how many. Tests, build
output and AI-agent folders are excluded by default; see
[How do exclusion patterns work?](faq.md#how-do-exclusion-patterns-work).

## Dart

Know these limits before relying on a Dart space, especially its call edges:

- **Classes with declaring constructors are missing.** The Dart grammar CodeKB
  uses (`tree-sitter-dart` 0.1.0) predates Dart 3.13's declaring constructors, so a
  class written as `class Foo() extends Bar {}` is not indexed. Its methods may
  appear as top-level functions instead. Enums with declaring constructors are
  parsed correctly.
- **Generated files are skipped:** `*.g.dart`, `*.freezed.dart`, `*.gr.dart` and
  `*.mocks.dart`.
- **Generic named-constructor calls aren't followed.** A call such as
  `Box<int>.named()` gets no call edge, even when the constructor declaration is
  indexed.
- **Calls through static factory results can resolve to the wrong class.** Type
  inference assumes a class-like factory returns its own class, so a later call
  through that value can target the factory's class when the factory actually
  returns another type.

Classes, mixins, extensions, extension types and typedefs are all stored as
`class` components; the signature shows which one it is.

## Python

Call edges from Python code follow Python's own name rules:

- `self.x()`, `cls.x()` and `super().x()` resolve through the caller's class and
  its bases in method resolution order; base classes are found through the
  module's imports. When a base class outside the repository (the standard
  library, an installed package) comes before the method in that order, the call
  gets no edge rather than a guess at a same-named method of an unrelated class.
- A bare call such as `helper()` resolves through the enclosing functions, then
  the module and its imports. A call to a parameter or to a builtin gets no edge.
- A call through another object, such as `client.get()`, still needs a known type
  for `client`: a parameter annotation, or an assignment from a class name in the
  same function. A variable annotation (`client: Client = make()`) and an alias of
  the class name (`client = C()` after `import Client as C`) do not give a type.
