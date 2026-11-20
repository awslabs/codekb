// Parser test fixture - TypeScript
// Comprehensive test code covering real-world patterns

// Constants
const MAX_RETRIES = 3;
const DEFAULT_TIMEOUT = 30;

// Arrow function assignment
const validateInput = (x: string | null): boolean => {
    return x !== null && x.length > 0;
};

function helperFunction(x: number): number {
    return x * 2;
}

function anotherHelper(y: number, multiplier: number = 10): number {
    return y + multiplier;
}

function mainFunction(value: number, config?: object): number {
    const result = helperFunction(value);
    const final = anotherHelper(result);
    return final;
}

function _privateHelper(data: string): string {
    return data.trim();
}

interface CalculatorConfig {
    precision: number;
}

class Calculator {
    private precision: number;
    private _cache: Map<string, number>;
    
    constructor(precision: number = 2) {
        this.precision = precision;
        this._cache = new Map();
    }
    
    add(a: number, b: number): number {
        return a + b;
    }
    
    multiply(a: number, b: number): number {
        return a * b;
    }
    
    private _validate(value: any): boolean {
        return typeof value === 'number';
    }
    
    calculate(x: number, y: number): number | null {
        if (!this._validate(x) || !this._validate(y)) {
            return null;
        }
        
        const sumResult = this.add(x, y);
        const product = this.multiply(x, y);
        return sumResult + product;
    }
    
    processWithNested(items: number[]): number {
        const filterValid = (item: number): boolean => {
            return this._validate(item);
        };
        
        const validItems = items.filter(filterValid);
        return validItems.reduce((sum, item) => sum + item, 0);
    }
}

class DataProcessor {
    private calculator: Calculator;
    
    constructor() {
        this.calculator = new Calculator();
    }
    
    process(data: string): number | null {
        const cleaned = _privateHelper(data);
        if (validateInput(cleaned)) {
            return this.calculator.calculate(parseInt(cleaned), 2);
        }
        return null;
    }
}
