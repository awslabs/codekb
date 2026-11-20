package com.example.test;

import java.util.*;
import java.util.function.*;

/**
 * Comprehensive Java test file covering various function syntaxes
 */
public class JavaFunctionSyntaxTest {
    
    // 1. Regular instance method
    public String regularMethod(int param1, String param2) {
        return param2 + param1;
    }
    
    // 2. Static method
    public static void staticMethod() {
        System.out.println("Static method");
    }
    
    // 3. Private method
    private int privateMethod(int x) {
        return x * 2;
    }
    
    // 4. Protected method
    protected void protectedMethod() {
        // Implementation
    }
    
    // 5. Method with varargs
    public void varArgsMethod(String... args) {
        for (String arg : args) {
            System.out.println(arg);
        }
    }
    
    // 6. Generic method
    public <T> T genericMethod(T input) {
        return input;
    }
    
    // 7. Method with multiple generic types
    public <K, V> Map<K, V> multiGenericMethod(K key, V value) {
        Map<K, V> map = new HashMap<>();
        map.put(key, value);
        return map;
    }
    
    // 8. Method with bounded type parameters
    public <T extends Comparable<T>> T boundedGenericMethod(T a, T b) {
        return a.compareTo(b) > 0 ? a : b;
    }
    
    // 9. Method with throws clause
    public void methodWithException() throws Exception {
        throw new Exception("Test exception");
    }
    
    // 10. Synchronized method
    public synchronized void synchronizedMethod() {
        // Thread-safe operation
    }
    
    // 11. Final method
    public final void finalMethod() {
        // Cannot be overridden
    }
    
    // 12. Abstract method (in abstract class context)
    public abstract static class AbstractInner {
        public abstract void abstractMethod();
    }
    
    // 13. Default method (in interface context)
    public interface TestInterface {
        default void defaultMethod() {
            System.out.println("Default implementation");
        }
        
        // 14. Static method in interface
        static void staticInterfaceMethod() {
            System.out.println("Static interface method");
        }
    }
    
    // 15. Constructor
    public JavaFunctionSyntaxTest() {
        // Default constructor
    }
    
    // 16. Parameterized constructor
    public JavaFunctionSyntaxTest(String name, int value) {
        // Parameterized constructor
    }
    
    // 17. Lambda expressions - various forms
    public void lambdaExamples() {
        // Simple lambda
        Runnable r1 = () -> System.out.println("Hello");
        
        // Lambda with single parameter
        Consumer<String> c1 = s -> System.out.println(s);
        
        // Lambda with multiple parameters
        BiFunction<Integer, Integer, Integer> add = (a, b) -> a + b;
        
        // Lambda with block body
        Function<String, String> upper = (str) -> {
            String result = str.toUpperCase();
            return result;
        };
        
        // Lambda with explicit types
        BiConsumer<String, Integer> bc = (String s, Integer i) -> {
            System.out.println(s + i);
        };
    }
    
    // 18. Method references
    public void methodReferenceExamples() {
        // Static method reference
        Function<String, Integer> parseInt = Integer::parseInt;
        
        // Instance method reference
        String str = "hello";
        Supplier<String> upper = str::toUpperCase;
        
        // Constructor reference
        Supplier<List<String>> listSupplier = ArrayList::new;
        
        // Arbitrary object method reference
        Function<String, String> trim = String::trim;
    }
    
    // 19. Inner class with methods
    public class InnerClass {
        public void innerMethod() {
            System.out.println("Inner class method");
        }
        
        private void privateInnerMethod() {
            // Private method in inner class
        }
    }
    
    // 20. Static nested class with methods
    public static class StaticNestedClass {
        public static void staticNestedMethod() {
            System.out.println("Static nested class method");
        }
        
        public void instanceNestedMethod() {
            System.out.println("Instance method in static nested class");
        }
    }
    
    // 21. Local class (method-local inner class)
    public void methodWithLocalClass() {
        class LocalClass {
            public void localMethod() {
                System.out.println("Local class method");
            }
        }
        
        LocalClass local = new LocalClass();
        local.localMethod();
    }
    
    // 22. Anonymous class
    public void anonymousClassExample() {
        Runnable r = new Runnable() {
            @Override
            public void run() {
                System.out.println("Anonymous class method");
            }
            
            // Additional method in anonymous class
            public void extraMethod() {
                System.out.println("Extra method");
            }
        };
    }
    
    // 23. Method with annotations
    @Override
    @Deprecated
    @SuppressWarnings("unchecked")
    public String toString() {
        return "JavaFunctionSyntaxTest";
    }
    
    // 24. Native method
    public native void nativeMethod();
    
    // 25. Strictfp method
    public strictfp double strictfpMethod(double a, double b) {
        return a * b;
    }
    
    // 26. Method with array parameters and return type
    public int[] arrayMethod(int[] input, String[] strings) {
        return new int[]{1, 2, 3};
    }
    
    // 27. Recursive method
    public int factorial(int n) {
        if (n <= 1) return 1;
        return n * factorial(n - 1);
    }
    
    // 28. Method with functional interface parameter
    public void higherOrderMethod(Function<Integer, Integer> func) {
        int result = func.apply(10);
        System.out.println(result);
    }
    
    // 29. Method returning functional interface
    public Function<String, Integer> methodReturningFunction() {
        return String::length;
    }
    
    // 30. Enum with methods
    public enum Status {
        ACTIVE {
            @Override
            public void action() {
                System.out.println("Active action");
            }
        },
        INACTIVE {
            @Override
            public void action() {
                System.out.println("Inactive action");
            }
        };
        
        public abstract void action();
        
        public void commonMethod() {
            System.out.println("Common enum method");
        }
    }
    
    // 31. Record with methods (Java 14+)
    public record Person(String name, int age) {
        // Compact constructor
        public Person {
            if (age < 0) {
                throw new IllegalArgumentException("Age cannot be negative");
            }
        }
        
        // Additional method in record
        public boolean isAdult() {
            return age >= 18;
        }
        
        // Static method in record
        public static Person createDefault() {
            return new Person("Unknown", 0);
        }
    }
    
    // 32. Sealed class with methods (Java 17+)
    public sealed class Shape permits Circle, Rectangle {
        public abstract double area();
        
        public void commonMethod() {
            System.out.println("Common shape method");
        }
    }
    
    public final class Circle extends Shape {
        private double radius;
        
        @Override
        public double area() {
            return Math.PI * radius * radius;
        }
    }
    
    public final class Rectangle extends Shape {
        private double width, height;
        
        @Override
        public double area() {
            return width * height;
        }
    }
    
    // 33. Method with try-with-resources
    public void tryWithResourcesMethod() {
        try (Scanner scanner = new Scanner(System.in)) {
            String line = scanner.nextLine();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }
    
    // 34. Method with switch expression (Java 14+)
    public String switchExpressionMethod(int value) {
        return switch (value) {
            case 1 -> "One";
            case 2 -> "Two";
            case 3 -> {
                String result = "Three";
                yield result;
            }
            default -> "Other";
        };
    }
    
    // 35. Main method
    public static void main(String[] args) {
        JavaFunctionSyntaxTest test = new JavaFunctionSyntaxTest();
        
        // Lambda usage
        test.lambdaExamples();
        
        // Method reference usage
        test.methodReferenceExamples();
        
        // Higher-order function usage
        test.higherOrderMethod(x -> x * 2);
    }
}
