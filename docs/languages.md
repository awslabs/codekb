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
| Python | |
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
