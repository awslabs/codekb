# Parser test fixture - Python
# Comprehensive test code covering real-world patterns

# Constants
MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30

# Lambda assignment (intentional for parser testing)
validate_input = lambda x: x is not None and len(x) > 0  # noqa: E731


def helper_function(x):
    """Helper function with docstring."""
    return x * 2


def another_helper(y, multiplier=10):
    """Helper with default parameter."""
    return y + multiplier


def main_function(value, config=None):
    """Main function that calls helpers.

    Args:
        value: Input value
        config: Optional configuration
    """
    result = helper_function(value)
    final = another_helper(result)
    return final


def _private_helper(data):
    """Private helper function."""
    return data.strip()


class Calculator:
    """Calculator class with various methods."""

    def __init__(self, precision=2):
        self.precision = precision
        self._cache = {}

    def add(self, a, b):
        """Add two numbers."""
        return a + b

    def multiply(self, a, b):
        """Multiply two numbers."""
        return a * b

    def _validate(self, value):
        """Private validation method."""
        return isinstance(value, (int, float))

    def calculate(self, x, y):
        """Calculate using multiple operations."""
        if not self._validate(x) or not self._validate(y):
            return None

        sum_result = self.add(x, y)
        product = self.multiply(x, y)
        return sum_result + product

    def process_with_nested(self, items):
        """Method with nested function."""

        def filter_valid(item):
            return self._validate(item)

        valid_items = [i for i in items if filter_valid(i)]
        return sum(valid_items)


class DataProcessor:
    """Processor that uses other classes."""

    def __init__(self):
        self.calculator = Calculator()

    def process(self, data):
        """Process data using calculator."""
        cleaned = _private_helper(str(data))
        if validate_input(cleaned):
            return self.calculator.calculate(int(cleaned), 2)
        return None
