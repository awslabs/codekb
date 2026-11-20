// Parser test fixture - JavaScript
// Comprehensive test code covering legacy through modern patterns

// ═══════════════════════════════════════════════════════════════════════
// CONSTANTS AND VARIABLES
// ═══════════════════════════════════════════════════════════════════════

const MAX_RETRIES = 3;
const DEFAULT_TIMEOUT = 30;
let globalCounter = 0;
var legacyVar = "hello";

// ═══════════════════════════════════════════════════════════════════════
// FUNCTION DECLARATIONS (ES3+)
// ═══════════════════════════════════════════════════════════════════════

function helperFunction(x) {
    return x * 2;
}

function anotherHelper(y, multiplier) {
    return y + multiplier;
}

function mainFunction(value, config) {
    const result = helperFunction(value);
    const final = anotherHelper(result, 10);
    return final;
}

// Private function (leading underscore convention)
function _privateHelper(data) {
    return data.trim();
}

// Function with default parameters (ES6+)
function withDefaults(x = 10, y = 20, options = {}) {
    return x + y;
}

// Function with rest parameters (ES6+)
function withRest(first, ...rest) {
    return [first, ...rest];
}

// Function with destructured parameters (ES6+)
function withDestructured({ name, age }, [first, second]) {
    return `${name} is ${age}`;
}

// Nested function declarations
function outerFunction(x) {
    function innerFunction(y) {
        return x + y;
    }
    
    function anotherInner(z) {
        return innerFunction(z) * 2;
    }
    
    return anotherInner(x);
}

// Higher-order function returning named function
function withLogging(fn) {
    return function wrapped(...args) {
        console.log('calling', fn.name);
        return fn(...args);
    };
}

// ═══════════════════════════════════════════════════════════════════════
// ARROW FUNCTIONS (ES6+)
// ═══════════════════════════════════════════════════════════════════════

const validateInput = (x) => {
    return x !== null && x.length > 0;
};

const double = x => x * 2;

const processItems = (items, callback) => {
    return items.map(callback);
};

// Async arrow function (ES2017+)
const fetchData = async (url, options = {}) => {
    const response = await fetch(url, options);
    return response.json();
};

// ═══════════════════════════════════════════════════════════════════════
// ASYNC FUNCTIONS (ES2017+)
// ═══════════════════════════════════════════════════════════════════════

async function loadUser(userId) {
    const response = await fetchData(`/api/users/${userId}`);
    return response;
}

async function saveUser(user) {
    const validated = validateInput(user.name);
    if (!validated) throw new Error('Invalid name');
    return await fetchData('/api/users', { method: 'POST', body: user });
}

// ═══════════════════════════════════════════════════════════════════════
// GENERATOR FUNCTIONS (ES6+)
// ═══════════════════════════════════════════════════════════════════════

function* idGenerator() {
    let id = 0;
    while (true) {
        yield ++id;
    }
}

function* fibonacci() {
    let a = 0, b = 1;
    while (true) {
        yield a;
        [a, b] = [b, a + b];
    }
}

// Async generator (ES2018+)
async function* streamEvents(url) {
    const source = new EventSource(url);
    while (true) {
        yield await new Promise(resolve => {
            source.onmessage = (e) => resolve(e.data);
        });
    }
}

// ═══════════════════════════════════════════════════════════════════════
// ES6+ CLASSES
// ═══════════════════════════════════════════════════════════════════════

class Calculator {
    // Class fields (ES2022+)
    precision = 2;
    static instanceCount = 0;
    #cache = new Map();

    constructor(precision = 2) {
        this.precision = precision;
        Calculator.instanceCount++;
    }

    add(a, b) {
        return a + b;
    }

    multiply(a, b) {
        return a * b;
    }

    // Private method (ES2022+)
    #validate(value) {
        return typeof value === 'number';
    }

    calculate(x, y) {
        if (!this.#validate(x) || !this.#validate(y)) {
            return null;
        }
        const sumResult = this.add(x, y);
        const product = this.multiply(x, y);
        return sumResult + product;
    }

    // Getter
    get cacheSize() {
        return this.#cache.size;
    }

    // Setter
    set maxPrecision(value) {
        this.precision = Math.min(value, 10);
    }

    // Static method
    static create(precision) {
        return new Calculator(precision);
    }

    // Async method
    async fetchRate(currency) {
        const data = await fetchData(`/api/rates/${currency}`);
        return data.rate;
    }

    // Generator method
    *iterateCache() {
        for (const [key, value] of this.#cache) {
            yield { key, value };
        }
    }

    // Computed property method
    [Symbol.iterator]() {
        return this.iterateCache();
    }
}

// Class with inheritance
class ScientificCalculator extends Calculator {
    constructor() {
        super(10);
    }

    power(base, exponent) {
        return Math.pow(base, exponent);
    }

    sqrt(value) {
        return Math.sqrt(value);
    }
}

// Class expression
const EventEmitter = class {
    #listeners = {};

    on(event, callback) {
        if (!this.#listeners[event]) {
            this.#listeners[event] = [];
        }
        this.#listeners[event].push(callback);
    }

    emit(event, ...args) {
        const handlers = this.#listeners[event] || [];
        handlers.forEach(handler => handler(...args));
    }
};

// ═══════════════════════════════════════════════════════════════════════
// LEGACY PROTOTYPE-BASED CLASSES (ES3/ES5)
// ═══════════════════════════════════════════════════════════════════════

function Animal(name, sound) {
    this.name = name;
    this.sound = sound;
}

Animal.prototype.speak = function() {
    return `${this.name} says ${this.sound}`;
};

Animal.prototype.toString = function() {
    return `[Animal: ${this.name}]`;
};

// Static method on constructor function
Animal.create = function(name, sound) {
    return new Animal(name, sound);
};

// Object.assign to prototype (bulk method assignment)
function Vehicle(make, model) {
    this.make = make;
    this.model = model;
}

Object.assign(Vehicle.prototype, {
    start() {
        return `${this.make} ${this.model} starting...`;
    },

    stop() {
        return `${this.make} ${this.model} stopped.`;
    },

    getInfo: function() {
        return `${this.make} ${this.model}`;
    }
});

// Foo.prototype = { ... } direct assignment pattern
function Shape(type) {
    this.type = type;
}

Shape.prototype = {
    draw() {
        return `Drawing ${this.type}`;
    },

    resize(factor) {
        return `Resizing ${this.type} by ${factor}`;
    }
};

// ═══════════════════════════════════════════════════════════════════════
// OBJECT LITERAL METHODS
// ═══════════════════════════════════════════════════════════════════════

const utils = {
    // Shorthand method (ES6+)
    formatDate(date) {
        return date.toISOString();
    },

    // Property function (legacy)
    parseDate: function(str) {
        return new Date(str);
    },

    // Async shorthand method
    async fetchConfig() {
        return await fetchData('/api/config');
    },

    // Generator shorthand method
    *generateIds() {
        let id = 0;
        while (true) yield ++id;
    },

    // Arrow function property
    capitalize: (str) => str.charAt(0).toUpperCase() + str.slice(1),
};

// ═══════════════════════════════════════════════════════════════════════
// MODULE PATTERNS
// ═══════════════════════════════════════════════════════════════════════

// CommonJS exports
exports.helperFunction = function exportedHelper(x) {
    return x * 2;
};

// ═══════════════════════════════════════════════════════════════════════
// EXPORT DECLARATIONS (ES6 modules)
// ═══════════════════════════════════════════════════════════════════════

export function exportedFunction(a, b) {
    return a + b;
}

export class ExportedClass {
    constructor(value) {
        this.value = value;
    }

    getValue() {
        return this.value;
    }
}

export const exportedArrow = (x) => x * 2;

export default function defaultExport() {
    return 'default';
}

// ═══════════════════════════════════════════════════════════════════════
// REACT PATTERNS (JSX)
// ═══════════════════════════════════════════════════════════════════════

function UserProfile({ user, onUpdate }) {
    const handleSubmit = (event) => {
        event.preventDefault();
        onUpdate(user);
    };

    return null; // JSX would go here
}

const MemoizedList = React.memo(function ListComponent({ items }) {
    return null;
});

const ForwardedInput = React.forwardRef(function Input(props, ref) {
    return null;
});

// ═══════════════════════════════════════════════════════════════════════
// IIFE (Immediately Invoked Function Expression)
// ═══════════════════════════════════════════════════════════════════════

const singleton = (function createSingleton() {
    let instance = null;
    return {
        getInstance() {
            if (!instance) instance = {};
            return instance;
        }
    };
})();

// ═══════════════════════════════════════════════════════════════════════
// CROSS-FUNCTION CALLS (for call graph testing)
// ═══════════════════════════════════════════════════════════════════════

function processData(rawData) {
    const cleaned = _privateHelper(rawData);
    if (validateInput(cleaned)) {
        const calc = new Calculator();
        return calc.calculate(parseInt(cleaned), 2);
    }
    return null;
}

class DataProcessor {
    constructor() {
        this.calculator = new Calculator();
    }

    process(data) {
        const cleaned = _privateHelper(data);
        if (validateInput(cleaned)) {
            return this.calculator.calculate(parseInt(cleaned), 2);
        }
        return null;
    }

    async processAsync(url) {
        const data = await fetchData(url);
        return this.process(data);
    }
}
