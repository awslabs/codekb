// A Dart library file holding nothing but directives.
//
// Legitimate Dart that declares no component: a barrel file. Extraction must
// return an empty list and report a clean parse, not a failure (BR4.3).

library parser_test_dart_directives;

import 'dart:async';
import 'dart:convert' show jsonDecode;
import 'package:meta/meta.dart' as meta;

export 'parser_test_dart.dart';
