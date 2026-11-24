// Parser test fixture - TypeScript abstract classes
// Regression fixture for abstract class indexing: the class, its abstract
// method signatures, and its concrete methods must all become components.

abstract class Shape {
    protected name: string;

    abstract area(): number;

    abstract scale(factor: number): void;

    describe(): string {
        return `${this.name} covers ${this.area()}`;
    }
}

class Circle extends Shape {
    protected name: string;
    private radius: number;

    constructor(radius: number) {
        super();
        this.name = "circle";
        this.radius = radius;
    }

    area(): number {
        return Math.PI * this.radius * this.radius;
    }

    scale(factor: number): void {
        this.radius *= factor;
    }
}
