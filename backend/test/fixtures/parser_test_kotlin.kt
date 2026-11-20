package com.example.test

import kotlin.math.sqrt

/**
 * A sample Kotlin class for testing the parser.
 */
class Calculator(private val name: String) {
    
    companion object {
        const val PI = 3.14159
        
        fun createDefault(): Calculator {
            return Calculator("Default")
        }
    }
    
    private var lastResult: Double = 0.0
    
    /**
     * Adds two numbers together.
     */
    fun add(a: Int, b: Int): Int {
        val result = a + b
        lastResult = result.toDouble()
        return result
    }
    
    /**
     * Subtracts b from a.
     */
    fun subtract(a: Int, b: Int = 0): Int {
        return a - b
    }
    
    /**
     * Multiplies two numbers.
     */
    protected open fun multiply(a: Double, b: Double): Double {
        return a * b
    }
    
    /**
     * Calculates the square root.
     */
    internal fun squareRoot(value: Double): Double {
        return sqrt(value)
    }
}

/**
 * Interface for mathematical operations.
 */
interface MathOperations {
    fun calculate(x: Double, y: Double): Double
}

/**
 * Data class for storing results.
 */
data class Result(val value: Double, val operation: String)

/**
 * Singleton object for utility functions.
 */
object MathUtils {
    fun max(a: Int, b: Int): Int {
        return if (a > b) a else b
    }
    
    fun min(a: Int, b: Int): Int {
        return if (a < b) a else b
    }
}

/**
 * Extension function on Int.
 */
fun Int.isEven(): Boolean {
    return this % 2 == 0
}

/**
 * Top-level function.
 */
fun factorial(n: Int): Int {
    return if (n <= 1) 1 else n * factorial(n - 1)
}
