# Languages

CodeKB parses supported languages into components:

| Language | Additional notes |
|---|---|
| C | |
| C++ | See [C++ notes](#c) |
| C# | |
| Dart | See [Dart notes](#dart) |
| Go | |
| Java | |
| JavaScript | |
| Kotlin | |
| PHP | |
| Python | |
| Ruby | |
| Rust | See [Rust notes](#rust) |
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

## Rust

- An `impl` block for a type that the file does not define (a type from another
  file or crate, a type parameter, a primitive type) is stored as an
  `implementation` component named after that type, and its methods hang under
  it. A type with impl blocks in several files has one such component per file.
- An inline `mod name { ... }` is a `module` component. A function in it is a
  function, not a method, and a call to it by path (`name::f()`) gets an edge.

## C++

- A class head that tree-sitter does not read, such as a macro followed by an
  attribute (`class GTEST_API_ [[nodiscard]] Name {`), is not indexed. A member
  of that class defined outside it gets a parent that no component has.
- A namespace opened by a macro (`FMT_BEGIN_NAMESPACE`) is not read. A class
  declared in it and its members defined in another file stand at file scope,
  so those members get a parent that no component has.
