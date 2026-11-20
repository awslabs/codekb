# Languages

CodeKB parses these languages into components: **Python, JavaScript,
TypeScript, Java, Kotlin, Go, Rust, C, C++, C#, PHP, Ruby, Terraform (HCL) and
Dart**.

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
- **Calls through function parameters aren't followed.** In
  `void helper(r) { r.find(); }`, the call to `find` gets no edge even when the
  argument's type is known at the call site.
- **A method without a return type is recorded as the class's unnamed
  constructor** (`Repo.new`), because the grammar parses it that way.
- **A call edge can point at the wrong component.** A static factory is assumed to
  return its own class, and a call can resolve to a same-named component in
  another language in the same space. Treat a Dart edge that lands in another
  language as suspect.

Classes, mixins, extensions, extension types and typedefs are all stored as
`class` components; the signature shows which one it is.
